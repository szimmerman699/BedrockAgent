const chat = document.getElementById("chat");
const form = document.getElementById("form");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");
const clearBtn = document.getElementById("clear");
const welcome = document.getElementById("welcome");

const historyBtn = document.getElementById("historyBtn");
const historyPanel = document.getElementById("historyPanel");
const historyList = document.getElementById("historyList");

// Any API call that says we are signed out sends us to the login page.
const _fetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const res = await _fetch(...args);
  if (res.status === 401) location.href = "/login";
  return res;
};

const quotaEl = document.getElementById("quota");
const paywallEl = document.getElementById("paywall");
const upgradeBtn = document.getElementById("upgradeBtn");
const setLimit = document.getElementById("setLimit");
let blocked = false;
let paymentUrl = "";

// The license button does nothing until a payment link is configured (PAYMENT_URL).
function onUpgrade() {
  if (paymentUrl) window.open(paymentUrl, "_blank", "noopener");
}
upgradeBtn.addEventListener("click", onUpgrade);
document.getElementById("paywallBtn").addEventListener("click", onUpgrade);

function showUser(u) {
  if (!u) return;
  paymentUrl = u.payment_url || "";
  quotaEl.hidden = false;
  quotaEl.textContent = u.limit == null
    ? `${u.email} · Unlimited`
    : `${u.email} · ${Math.max(u.limit - u.used, 0)} of ${u.limit} free messages left`;
  upgradeBtn.hidden = u.limit == null;
  if (u.limit != null && u.used >= u.limit) showPaywall();
}

function showPaywall() {
  blocked = true;
  paywallEl.hidden = false;
  document.getElementById("form").hidden = true;
}

fetch("/api/me").then(r => r.json()).then(showUser).catch(() => {});

document.getElementById("logout").addEventListener("click", async () => {
  await _fetch("/api/logout", { method: "POST" });
  location.href = "/login";
});

const settingsBtn = document.getElementById("settingsBtn");
const settingsPanel = document.getElementById("settingsPanel");
const setPrompt = document.getElementById("setPrompt");
const setMax = document.getElementById("setMax");
const setTemp = document.getElementById("setTemp");
const setStatus = document.getElementById("setStatus");
let settingDefaults = null;

const ctxEl = document.getElementById("ctx");
const setWindow = document.getElementById("setWindow");
let ctxLimited = false, ctxSummarized = false, ctxWindow = 3;

function updateCtx(limited, summarized, windowSize) {
  if (limited !== undefined) ctxLimited = limited;
  if (summarized !== undefined) ctxSummarized = summarized;
  if (windowSize !== undefined) ctxWindow = windowSize;
  ctxEl.hidden = !ctxLimited;
  ctxEl.textContent = ctxSummarized ? `Context: last ${ctxWindow} + summary` : `Context: last ${ctxWindow} messages`;
  ctxEl.title = ctxSummarized
    ? `Older messages in this chat were compacted into a summary; only your last ${ctxWindow} messages are sent in full.`
    : `Only your last ${ctxWindow} messages are sent in full; older ones will be summarized.`;
}

function fillSettings(s) {
  setPrompt.value = s.system_prompt;
  setMax.value = s.max_tokens;
  setTemp.value = s.temperature;
  setLimit.checked = !!s.limit_context;
  setWindow.value = s.context_window;
  updateCtx(!!s.limit_context, undefined, s.context_window);
}

async function loadSettings() {
  const res = await fetch("/api/settings");
  const data = await res.json();
  settingDefaults = data.defaults;
  fillSettings(data.settings);
}

function flashStatus(msg, isError) {
  setStatus.textContent = msg;
  setStatus.className = "status" + (isError ? " bad" : "");
  setTimeout(() => { setStatus.textContent = ""; }, 3000);
}

settingsBtn.addEventListener("click", async () => {
  const open = settingsPanel.hidden;
  settingsPanel.hidden = !open;
  settingsBtn.setAttribute("aria-expanded", String(open));
  if (open) await loadSettings();
});

settingsPanel.addEventListener("submit", async e => {
  e.preventDefault();
  const res = await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      system_prompt: setPrompt.value,
      max_tokens: setMax.value,
      temperature: setTemp.value,
      limit_context: setLimit.checked,
      context_window: setWindow.value,
    }),
  });
  const data = await res.json();
  if (!res.ok) return flashStatus(data.error || "Save failed", true);
  fillSettings(data.settings);
  flashStatus("Saved — applies to your next message");
});

// The context option takes effect immediately - no separate Save click needed.
setLimit.addEventListener("change", () => settingsPanel.requestSubmit());
setWindow.addEventListener("change", () => settingsPanel.requestSubmit());
loadSettings().catch(() => {});

document.getElementById("setReset").addEventListener("click", () => {
  if (settingDefaults) fillSettings(settingDefaults);
  flashStatus("Defaults loaded — click Save to apply");
});

let conversationId = new URLSearchParams(location.search).get("c");
let busy = false;
const totals = { input: 0, output: 0 };
const tokensEl = document.getElementById("tokens");

// Header shows the CURRENT context (the last request), so it drops when old messages
// are compacted. Session totals, including summarizing calls, go in the tooltip.
function addUsage(usage, summaryUsage) {
  if (!usage) return;
  const inp = usage.inputTokens || 0, out = usage.outputTokens || 0;
  const sInp = (summaryUsage && summaryUsage.inputTokens) || 0;
  const sOut = (summaryUsage && summaryUsage.outputTokens) || 0;
  totals.input += inp + sInp;
  totals.output += out + sOut;
  tokensEl.textContent = `In ${inp.toLocaleString()} · Out ${out.toLocaleString()}`;
  tokensEl.title = `Tokens in the current request. Session total: ${totals.input.toLocaleString()} in · ${totals.output.toLocaleString()} out`;
  let label = `${inp.toLocaleString()} in · ${out.toLocaleString()} out`;
  if (sInp || sOut) label += ` (+${(sInp + sOut).toLocaleString()} to compact older messages)`;
  return label;
}

function setUrl(id) {
  const url = id ? `/?c=${id}` : "/";
  history.replaceState(null, "", url);
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Minimal safe formatting: ```code blocks```, `inline code`, **bold**
function format(text) {
  let html = escapeHtml(text);
  html = html.replace(/```(?:\w+)?\n?([\s\S]*?)```/g, (_, code) => `<pre><code>${code.replace(/\n$/, "")}</code></pre>`);
  html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  return html;
}

function addMessage(kind, content, { html = false } = {}) {
  welcome?.remove();
  const row = document.createElement("div");
  row.className = `msg ${kind === "error" ? "bot error" : kind}`;
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = kind === "user" ? "You" : "AI";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (html) bubble.innerHTML = content; else bubble.textContent = content;
  row.append(avatar, bubble);
  chat.append(row);
  chat.scrollTop = chat.scrollHeight;
  return row;
}

function setBusy(state) {
  busy = state;
  sendBtn.disabled = state;
  input.disabled = state;
  if (!state) input.focus();
}

function autosize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
}

async function send(text) {
  text = text.trim();
  if (!text || busy || blocked) return;

  addMessage("user", text);
  input.value = "";
  autosize();
  setBusy(true);

  const typing = addMessage("bot", '<span class="typing"><span></span><span></span><span></span></span>', { html: true });

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, conversation_id: conversationId }),
    });
    const data = await res.json();
    typing.remove();
    if (res.status === 402) { paymentUrl = data.payment_url || ""; showPaywall(); throw new Error(data.error); }
    if (!res.ok) throw new Error(data.error || "Request failed");
    showUser(data.user);
    if (data.context) updateCtx(data.context.limited, data.context.summarized, data.context.window);
    if (data.conversation_id && data.conversation_id !== conversationId) {
      conversationId = data.conversation_id;
      setUrl(conversationId);
    }
    const row = addMessage("bot", format(data.reply), { html: true });
    const label = addUsage(data.usage, data.summary_usage);
    if (label) {
      const meta = document.createElement("div");
      meta.className = "usage";
      meta.textContent = label;
      row.querySelector(".bubble").append(meta);
    }
  } catch (err) {
    typing.remove();
    addMessage("error", err.message || "Something went wrong.");
  } finally {
    setBusy(false);
  }
}

form.addEventListener("submit", e => { e.preventDefault(); send(input.value); });

input.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
});
input.addEventListener("input", autosize);

document.querySelectorAll(".chip").forEach(chip =>
  chip.addEventListener("click", () => send(chip.textContent)));

clearBtn.addEventListener("click", () => { location.href = "/"; });

async function loadConversation(id) {
  try {
    const res = await fetch(`/api/conversations/${encodeURIComponent(id)}`);
    if (!res.ok) throw new Error("not found");
    const data = await res.json();
    updateCtx(undefined, !!data.summarized);
    data.messages.forEach(m =>
      m.role === "user" ? addMessage("user", m.text) : addMessage("bot", format(m.text), { html: true }));
  } catch {
    conversationId = null;
    setUrl(null);
  }
}

async function refreshHistory() {
  const res = await fetch("/api/conversations");
  const { conversations } = await res.json();
  historyList.innerHTML = "";
  if (!conversations.length) {
    historyList.innerHTML = '<li class="empty">No saved chats yet.</li>';
    return;
  }
  conversations.forEach(c => {
    const li = document.createElement("li");
    if (c.id === conversationId) li.className = "active";
    const open = document.createElement("a");
    open.href = `/?c=${c.id}`;
    open.textContent = c.title;
    const del = document.createElement("button");
    del.type = "button";
    del.className = "del";
    del.setAttribute("aria-label", "Delete chat");
    del.textContent = "×";
    del.addEventListener("click", async () => {
      await fetch(`/api/conversations/${c.id}`, { method: "DELETE" });
      if (c.id === conversationId) location.href = "/";
      else refreshHistory();
    });
    li.append(open, del);
    historyList.append(li);
  });
}

historyBtn.addEventListener("click", async () => {
  const open = historyPanel.hidden;
  historyPanel.hidden = !open;
  historyBtn.setAttribute("aria-expanded", String(open));
  if (open) await refreshHistory();
});

if (conversationId) loadConversation(conversationId);
input.focus();
