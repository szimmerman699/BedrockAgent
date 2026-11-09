import os
import re
import secrets
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from functools import wraps

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, NoCredentialsError
from flask import Flask, g, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-opus-4-6-v1")
MAX_TOKENS = 1000
TEMPERATURE = 0.5
MAX_HISTORY = 40  # messages kept per request
SYSTEM_PROMPT = "You are a helpful, concise assistant."
FREE_MESSAGE_LIMIT = int(os.environ.get("FREE_MESSAGE_LIMIT", "10"))  # messages per free account
PAYMENT_URL = os.environ.get("PAYMENT_URL", "")  # e.g. a Stripe Payment Link
PRICE_LABEL = "$10/month"  # shown on the upgrade button; unlimited messages
CONTEXT_WINDOW = 3  # default: your last N messages (with replies) kept verbatim when the limit is on
DB_PATH = os.environ.get("CHAT_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat.db"))

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # always revalidate JS/CSS so UI updates show up immediately

def _clean(s):
    # Strip stray control chars (e.g. 0x16 from a bad paste) that break SigV4 signing.
    return "".join(ch for ch in s if ch.isalnum() or ch in "/+=")


_creds = boto3.Session().get_credentials()
# Only freeze/clean static keys (env vars or ~/.aws/credentials). Rotating credentials
# (EC2 instance role, container role, SSO, assume-role) must stay on boto3's refreshing provider.
if _creds and getattr(_creds, "method", None) in ("env", "shared-credentials-file"):
    _f = _creds.get_frozen_credentials()
    session_aws = boto3.Session(
        aws_access_key_id=_clean(_f.access_key),
        aws_secret_access_key=_clean(_f.secret_key),
        aws_session_token=_clean(_f.token) if _f.token else None,
        region_name=AWS_REGION,
    )
else:
    session_aws = boto3.Session(region_name=AWS_REGION)
VERIFY_SSL = os.environ.get("AWS_VERIFY_SSL", "true")  # set "false" only for local dev behind a proxy
bedrock = session_aws.client(
    "bedrock-runtime",
    verify=VERIFY_SSL.strip().lower() not in ("false", "0", "no"),
    config=Config(retries={"max_attempts": 6, "mode": "adaptive"}),  # ride out ThrottlingException
)


# ---------- SQLite persistence ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    plan          TEXT NOT NULL DEFAULT 'free' CHECK (plan IN ('free', 'paid')),
    message_count INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT 'New chat',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text            TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
"""

# Columns added after the first release (applied to existing databases).
MIGRATIONS = [
    ("users", "count_date", "TEXT NOT NULL DEFAULT ''"),  # UTC day that message_count belongs to
    ("conversations", "user_id", "INTEGER REFERENCES users(id) ON DELETE CASCADE"),
    ("conversations", "summary", "TEXT NOT NULL DEFAULT ''"),
    ("conversations", "summarized_upto", "INTEGER NOT NULL DEFAULT 0"),
]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


with sqlite3.connect(DB_PATH) as _init:
    _init.executescript(SCHEMA)
    # Accounts were briefly keyed by username; they are keyed by email now.
    if "username" in {r[1] for r in _init.execute("PRAGMA table_info(users)")}:
        _init.execute("ALTER TABLE users RENAME COLUMN username TO email")
    for _table, _col, _decl in MIGRATIONS:
        if _col not in {r[1] for r in _init.execute(f"PRAGMA table_info({_table})")}:
            _init.execute(f"ALTER TABLE {_table} ADD COLUMN {_col} {_decl}")
    # Session-signing key: from the environment, else generated once and kept in the DB.
    _key = os.environ.get("SECRET_KEY")
    if not _key:
        _row = _init.execute("SELECT value FROM meta WHERE key = 'secret_key'").fetchone()
        if _row:
            _key = _row[0]
        else:
            _key = secrets.token_hex(32)
            _init.execute("INSERT INTO meta (key, value) VALUES ('secret_key', ?)", (_key,))

app.secret_key = _key
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
)


# ---------- authentication ----------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_DUMMY_HASH = generate_password_hash("not-a-real-password")
_failures = {}  # (ip, email) -> [timestamps]; single gunicorn worker, so in-memory is fine
MAX_FAILURES, FAIL_WINDOW = 5, 300


def current_user():
    if "user" not in g:
        g.user = None
        uid = session.get("uid")
        if uid:
            g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return g.user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            return jsonify(error="Please sign in."), 401
        return view(*args, **kwargs)
    return wrapped


def throttled(key):
    recent = [t for t in _failures.get(key, []) if time.time() - t < FAIL_WINDOW]
    _failures[key] = recent
    return len(recent) >= MAX_FAILURES


def user_payload(user):
    paid = user["plan"] == "paid"
    return {
        "email": user["email"],
        "plan": user["plan"],
        "used": user["message_count"],
        "price": PRICE_LABEL,
        "limit": None if paid else FREE_MESSAGE_LIMIT,
        "payment_url": PAYMENT_URL,
    }


@app.get("/login")
def login_page():
    if current_user():
        return redirect("/")
    return render_template("login.html")


@app.post("/api/register")
def register():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    if len(email) > 254 or not EMAIL_RE.match(email):
        return jsonify(error="Please enter a valid email address."), 400
    if len(password) < 8:
        return jsonify(error="Password must be at least 8 characters."), 400
    if password != str(data.get("confirm_password") or ""):
        return jsonify(error="Passwords do not match."), 400
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            (email, generate_password_hash(password), now()),
        )
    except sqlite3.IntegrityError:
        return jsonify(error="An account with that email already exists."), 409
    # Conversations from before accounts existed go to the first user to sign up.
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1:
        db.execute("UPDATE conversations SET user_id = ? WHERE user_id IS NULL", (cur.lastrowid,))
    db.commit()
    session.clear()
    session.permanent = True
    session["uid"] = cur.lastrowid
    return jsonify(ok=True)


@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    key = (request.remote_addr, email)
    if throttled(key):
        return jsonify(error="Too many failed attempts. Try again in a few minutes."), 429
    user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    ok = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)
    if not (user and ok):
        _failures.setdefault(key, []).append(time.time())
        return jsonify(error="Incorrect email or password."), 401
    _failures.pop(key, None)
    session.clear()
    session.permanent = True
    session["uid"] = user["id"]
    return jsonify(ok=True)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
@login_required
def me():
    return jsonify(user_payload(current_user()))


# ---------- per-user settings ----------
DEFAULTS = {
    "system_prompt": SYSTEM_PROMPT,
    "max_tokens": MAX_TOKENS,
    "temperature": TEMPERATURE,
    "limit_context": False,
    "context_window": CONTEXT_WINDOW,
}


def get_settings(uid):
    rows = get_db().execute("SELECT key, value FROM user_settings WHERE user_id = ?", (uid,)).fetchall()
    stored = {r["key"]: r["value"] for r in rows}
    out = dict(DEFAULTS)
    try:
        if "system_prompt" in stored:
            out["system_prompt"] = stored["system_prompt"]
        if "max_tokens" in stored:
            out["max_tokens"] = int(stored["max_tokens"])
        if "temperature" in stored:
            out["temperature"] = float(stored["temperature"])
        if "limit_context" in stored:
            out["limit_context"] = stored["limit_context"] == "1"
        if "context_window" in stored:
            out["context_window"] = int(stored["context_window"])
    except ValueError:
        pass
    return out


@app.get("/api/settings")
@login_required
def read_settings():
    return jsonify(settings=get_settings(current_user()["id"]), defaults=DEFAULTS)


@app.put("/api/settings")
@login_required
def update_settings():
    data = request.get_json(silent=True) or {}
    try:
        prompt = str(data.get("system_prompt", "")).strip()
        max_tokens = int(data.get("max_tokens"))
        temperature = float(data.get("temperature"))
        limit_context = bool(data.get("limit_context"))
        raw_window = data.get("context_window")  # older cached pages don't send this
        context_window = CONTEXT_WINDOW if raw_window in (None, "") else int(raw_window)
    except (TypeError, ValueError):
        return jsonify(error="Invalid settings values."), 400
    if not 1 <= context_window <= 20:
        return jsonify(error="Context window must be between 1 and 20 messages."), 400
    if not 1 <= max_tokens <= 64000:
        return jsonify(error="Max tokens must be between 1 and 64000."), 400
    if not 0 <= temperature <= 1:
        return jsonify(error="Temperature must be between 0 and 1."), 400
    uid = current_user()["id"]
    db = get_db()
    db.executemany(
        "INSERT INTO user_settings (user_id, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
        [
            (uid, "system_prompt", prompt),
            (uid, "max_tokens", str(max_tokens)),
            (uid, "temperature", str(temperature)),
            (uid, "limit_context", "1" if limit_context else "0"),
            (uid, "context_window", str(context_window)),
        ],
    )
    db.commit()
    return jsonify(settings=get_settings(uid))


# ---------- conversations ----------
def load_messages(conv_id):
    rows = get_db().execute(
        "SELECT id, role, text FROM messages WHERE conversation_id = ? ORDER BY id", (conv_id,)
    ).fetchall()
    return [{"id": r["id"], "role": r["role"], "text": r["text"]} for r in rows]


def clean_history(raw, limit=MAX_HISTORY):
    """Validate history into Bedrock Converse messages."""
    messages = []
    for item in raw[-limit:]:
        role = item.get("role")
        text = (item.get("text") or "").strip()
        if role not in ("user", "assistant") or not text:
            continue
        messages.append({"role": role, "content": [{"text": text}]})
    # Converse requires the conversation to start with a user turn.
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    return messages


CONTEXT_MSG_CHARS = 350   # older messages are trimmed to this many characters when limiting context
SUMMARY_MAX_CHARS = 450   # hard cap on the running summary


def clamp_context(messages):
    """Trim every message except the newest so the size of the context stays steady."""
    out = []
    for i, m in enumerate(messages):
        text = m["content"][0]["text"]
        if i < len(messages) - 1 and len(text) > CONTEXT_MSG_CHARS:
            text = text[:CONTEXT_MSG_CHARS].rstrip() + "…"
        out.append({"role": m["role"], "content": [{"text": text}]})
    return out


def add_usage(total, usage):
    for k, v in (usage or {}).items():
        total[k] = total.get(k, 0) + v


def reply_text(response):
    return "".join(b["text"] for b in response["output"]["message"]["content"] if "text" in b)


SUMMARY_PROMPT = (
    "You maintain a running summary of a conversation between a user and an assistant. "
    "Merge the earlier summary (if any) with the new messages into one concise summary of at most 60 words, "
    "so it never grows over time (drop the least important details first). "
    "Keep facts, decisions, names, numbers, code snippets worth remembering and open questions. "
    "Output only the summary."
)


def compact_history(conv_id, history, usage, window):
    """Fold messages older than the context window into the conversation summary.

    `history` is every stored message plus the new user turn. `window` is how many of your
    messages (each with its reply) stay verbatim, so N-1 earlier exchanges + the new message.
    Returns (summary, recent_messages).
    """
    keep = 2 * window - 1
    db = get_db()
    conv = db.execute("SELECT summary, summarized_upto FROM conversations WHERE id = ?", (conv_id,)).fetchone()
    summary, upto = conv["summary"], conv["summarized_upto"]
    recent, older = history[-keep:], history[:-keep]
    fresh = [m for m in older if m["id"] > upto]
    if fresh:
        transcript = "\n".join(f"{m['role'].upper()}: {m['text']}" for m in fresh)
        prompt = f"Earlier summary:\n{summary or '(none)'}\n\nNew messages:\n{transcript}"
        resp = bedrock.converse(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=[{"text": SUMMARY_PROMPT}],
            inferenceConfig={"maxTokens": 150, "temperature": 0.2},
        )
        add_usage(usage, resp.get("usage"))
        summary = reply_text(resp).strip()
        db.execute(
            "UPDATE conversations SET summary = ?, summarized_upto = ? WHERE id = ?",
            (summary, fresh[-1]["id"], conv_id),
        )
        db.commit()
    return summary, recent


@app.get("/")
def index():
    if current_user() is None:
        return redirect("/login")
    return render_template("index.html", model_id=MODEL_ID)


@app.get("/api/conversations")
@login_required
def list_conversations():
    rows = get_db().execute(
        "SELECT id, title, updated_at FROM conversations WHERE user_id = ? "
        "ORDER BY updated_at DESC LIMIT 100",
        (current_user()["id"],),
    ).fetchall()
    return jsonify(conversations=[dict(r) for r in rows])


@app.get("/api/conversations/<conv_id>")
@login_required
def get_conversation(conv_id):
    row = get_db().execute(
        "SELECT id, title, summary FROM conversations WHERE id = ? AND user_id = ?",
        (conv_id, current_user()["id"]),
    ).fetchone()
    if not row:
        return jsonify(error="Conversation not found."), 404
    return jsonify(id=row["id"], title=row["title"], messages=load_messages(conv_id),
                   summarized=bool(row["summary"]))


@app.delete("/api/conversations/<conv_id>")
@login_required
def delete_conversation(conv_id):
    db = get_db()
    db.execute("DELETE FROM conversations WHERE id = ? AND user_id = ?", (conv_id, current_user()["id"]))
    db.commit()
    return jsonify(ok=True)


@app.post("/api/chat")
@login_required
def chat():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify(error="Please send a message."), 400

    user = current_user()
    uid = user["id"]
    db = get_db()

    # Reserve one free message up front (atomic, so parallel requests can't slip past the limit).
    paid = user["plan"] == "paid"
    if not paid:
        reserved = db.execute(
            "UPDATE users SET message_count = message_count + 1 WHERE id = ? AND message_count < ?",
            (uid, FREE_MESSAGE_LIMIT),
        ).rowcount
        db.commit()
        if not reserved:
            return jsonify(
                error=f"You've used all {FREE_MESSAGE_LIMIT} free messages. "
                      f"Upgrade for unlimited messages ({PRICE_LABEL}).",
                paywall=True,
                payment_url=PAYMENT_URL,
            ), 402

    def refund():
        db.rollback()
        if not paid:
            db.execute("UPDATE users SET message_count = MAX(message_count - 1, 0) WHERE id = ?", (uid,))
            db.commit()

    conv_id = data.get("conversation_id")
    if conv_id and not db.execute(
        "SELECT 1 FROM conversations WHERE id = ? AND user_id = ?", (conv_id, uid)
    ).fetchone():
        conv_id = None
    if not conv_id:
        conv_id = uuid.uuid4().hex
        db.execute(
            "INSERT INTO conversations (id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (conv_id, uid, text[:60], now(), now()),
        )

    cfg = get_settings(uid)
    history = load_messages(conv_id) + [{"id": 0, "role": "user", "text": text}]
    summary_usage = {}  # tokens spent summarizing old messages (separate from the reply itself)
    system_text = cfg["system_prompt"]
    summarized = False
    try:
        if cfg["limit_context"]:
            summary, recent = compact_history(conv_id, history, summary_usage, cfg["context_window"])
            summarized = bool(summary)
            messages = clamp_context(clean_history(recent, 2 * cfg["context_window"] - 1))
            summary = summary[:SUMMARY_MAX_CHARS]
            if summary:
                system_text = f"{system_text}\n\nSummary of the earlier conversation:\n{summary}".strip()
        else:
            messages = clean_history(history)
        kwargs = {"system": [{"text": system_text}]} if system_text else {}
        response = bedrock.converse(
            modelId=MODEL_ID,
            messages=messages,
            inferenceConfig={"maxTokens": cfg["max_tokens"], "temperature": cfg["temperature"]},
            **kwargs,
        )
    except NoCredentialsError:
        refund()
        return jsonify(error="AWS credentials not found. Run: aws configure"), 500
    except ClientError as err:
        refund()
        e = err.response["Error"]
        return jsonify(error=f"{e['Code']}: {e['Message']}"), 502
    except Exception as err:  # network/SSL errors etc. - always answer with JSON
        refund()
        app.logger.exception("chat failed")
        return jsonify(error=f"Request failed: {type(err).__name__}"), 502

    reply = reply_text(response)
    usage = response.get("usage", {})
    ts = now()
    db.executemany(
        "INSERT INTO messages (conversation_id, role, text, created_at) VALUES (?, ?, ?, ?)",
        [(conv_id, "user", text, ts), (conv_id, "assistant", reply, ts)],
    )
    db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (ts, conv_id))
    db.commit()
    fresh_user = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return jsonify(reply=reply, conversation_id=conv_id, usage=usage, summary_usage=summary_usage, user=user_payload(fresh_user),
                   context={"limited": cfg["limit_context"], "summarized": summarized,
                            "window": cfg["context_window"]})


def _cli(argv):
    """python app.py upgrade <email> | downgrade <email> | reset <email> | users"""
    cmd = argv[0] if argv else ""
    with sqlite3.connect(DB_PATH) as db:
        if cmd == "users":
            for r in db.execute("SELECT email, plan, message_count FROM users ORDER BY id"):
                print(f"{r[0]:<24} {r[1]:<5} {r[2]} messages")
            return
        if cmd in ("upgrade", "downgrade", "reset") and len(argv) == 2:
            sql = {
                "upgrade": "UPDATE users SET plan = 'paid' WHERE email = ?",
                "downgrade": "UPDATE users SET plan = 'free' WHERE email = ?",
                "reset": "UPDATE users SET message_count = 0 WHERE email = ?",
            }[cmd]
            n = db.execute(sql, (argv[1],)).rowcount
            print(f"{cmd}: {argv[1]}" if n else f"No such user: {argv[1]}")
            return
    print(_cli.__doc__)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _cli(sys.argv[1:])
    else:
        app.run(debug=True)
