"use strict";

const state = {
  sessionId: null,
  busy: false,
  mode: "read",
  status: null,
  startedAt: null,
  elapsedTimer: null,
};

const elements = {
  activityCount: document.querySelector("#activity-count"),
  activityList: document.querySelector("#activity-list"),
  boundaryList: document.querySelector("#boundary-list"),
  boundaryState: document.querySelector("#boundary-state"),
  composer: document.querySelector("#composer"),
  connection: document.querySelector("#connection"),
  connectionLabel: document.querySelector("#connection-label"),
  conversation: document.querySelector("#conversation"),
  modeHelp: document.querySelector("#mode-help"),
  modelName: document.querySelector("#model-name"),
  newSession: document.querySelector("#new-session"),
  prompt: document.querySelector("#prompt"),
  receipt: document.querySelector("#receipt"),
  receiptHash: document.querySelector("#receipt-hash"),
  receiptState: document.querySelector("#receipt-state"),
  sendButton: document.querySelector("#send-button"),
  sendLabel: document.querySelector("#send-label"),
  sessionCount: document.querySelector("#session-count"),
  sessionId: document.querySelector("#session-id"),
  sessionList: document.querySelector("#session-list"),
  toast: document.querySelector("#toast"),
  version: document.querySelector("#version"),
  welcome: document.querySelector("#welcome"),
  workspaceName: document.querySelector("#workspace-name"),
};

const EVENT_LABELS = {
  "run.started": "开始运行",
  "run.completed": "运行完成",
  "run.failed": "运行失败",
  "tool.completed": "工具完成",
  "mutation.preview": "变更预览",
  "mutation.applied": "变更已应用",
};

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const error = new Error(payload?.error || `请求失败（${response.status}）`);
    error.sessionId = payload?.session_id || null;
    throw error;
  }
  return payload;
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    elements.toast.hidden = true;
  }, 5000);
}

function shortPath(path) {
  if (!path) return "—";
  const parts = path.replaceAll("\\", "/").split("/").filter(Boolean);
  return parts.at(-1) || path;
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function currentMode() {
  return document.querySelector('input[name="mode"]:checked')?.value || "read";
}

function setMode(mode) {
  state.mode = mode;
  const preview = mode === "preview";
  elements.boundaryState.textContent = preview ? "PREVIEW ONLY" : "READ ONLY";
  elements.modeHelp.textContent = preview
    ? "变更会生成差异预览，但不会真正写入任何文件。"
    : "只读模式不会向模型开放任何文件写入工具。";
  const names = {
    list_files: "浏览文件",
    read_file: "读取文件",
    search_text: "搜索文本",
    write_file: "写入文件",
    edit_files: "编辑文件",
  };
  const decisions = {
    allow: "允许",
    deny: "不可用",
    ask: "需确认",
    preview: "仅预览",
  };
  const capabilities = state.status?.modes?.[mode]?.capabilities;
  const rows = capabilities
    ? capabilities.map((capability) => [
        names[capability.name] || capability.name,
        decisions[capability.decision] || capability.decision,
      ])
    : [
        ["文件读取", "允许"],
        ["文件写入", preview ? "仅预览" : "不可用"],
      ];
  rows.push(["Shell / 删除", "不可用"]);
  elements.boundaryList.replaceChildren(
    ...rows.map(([name, value]) => {
      const item = document.createElement("li");
      const label = document.createElement("span");
      const decision = document.createElement("b");
      label.textContent = name;
      decision.textContent = value;
      item.append(label, decision);
      return item;
    }),
  );
}

function setBusy(busy) {
  state.busy = busy;
  elements.sendButton.disabled = busy;
  elements.newSession.disabled = busy;
  elements.prompt.disabled = busy;
  document.querySelectorAll('input[name="mode"]').forEach((input) => {
    input.disabled = busy;
  });
  window.clearInterval(state.elapsedTimer);
  if (busy) {
    state.startedAt = Date.now();
    elements.sendLabel.textContent = "运行中 0 秒";
    state.elapsedTimer = window.setInterval(() => {
      const seconds = Math.floor((Date.now() - state.startedAt) / 1000);
      elements.sendLabel.textContent = `运行中 ${seconds} 秒`;
    }, 1000);
  } else {
    state.startedAt = null;
    state.elapsedTimer = null;
    elements.sendLabel.textContent = "运行任务";
  }
}

function removeWelcome() {
  elements.welcome?.remove();
  elements.welcome = null;
}

function appendMessage(role, text, options = {}) {
  removeWelcome();
  const article = document.createElement("article");
  article.className = `message ${role}${options.error ? " error" : ""}`;
  if (options.id) article.id = options.id;

  const avatar = document.createElement("div");
  avatar.className = "message-avatar";
  avatar.textContent = role === "user" ? "YOU" : "MLA";
  avatar.setAttribute("aria-hidden", "true");

  const body = document.createElement("div");
  body.className = "message-body";
  const meta = document.createElement("div");
  meta.className = "message-meta";
  meta.textContent = role === "user" ? "你" : options.error ? "运行错误" : "本地 Agent";
  const content = document.createElement("div");
  content.className = "message-content";
  content.textContent = text;
  body.append(meta, content);
  article.append(avatar, body);
  elements.conversation.append(article);
  elements.conversation.scrollTop = elements.conversation.scrollHeight;
  return article;
}

function appendTyping() {
  removeWelcome();
  const article = document.createElement("article");
  article.className = "message typing";
  article.id = "typing-message";
  article.innerHTML = `
    <div class="message-avatar" aria-hidden="true">MLA</div>
    <div class="message-body">
      <div class="message-meta">本地 Agent · 正在思考</div>
      <div class="message-content" aria-label="正在生成回答">
        <span class="typing-dot"></span>
        <span class="typing-dot"></span>
        <span class="typing-dot"></span>
      </div>
    </div>`;
  elements.conversation.append(article);
  elements.conversation.scrollTop = elements.conversation.scrollHeight;
}

function resetActivity() {
  elements.activityCount.textContent = "0 EVENTS";
  elements.activityList.innerHTML = `
    <div class="activity-empty">
      <span aria-hidden="true">⌁</span>
      <p>工具调用和预览会显示在这里。</p>
    </div>`;
}

function eventDetail(event) {
  const data = event.data || event.details || {};
  if (data.tool_name) {
    const status = data.status ? ` · ${data.status}` : "";
    return `${data.tool_name}${status}`;
  }
  if (event.tool_name) return `${event.tool_name} · ${event.status}`;
  if (data.paths?.length) return data.paths.join(", ");
  if (data.run_id) return `run #${data.run_id}`;
  if (data.model) return data.model;
  return formatTime(event.created_at);
}

function renderActivity(events = []) {
  const visible = events.slice(-12);
  elements.activityCount.textContent = `${events.length} EVENTS`;
  if (!visible.length) {
    resetActivity();
    return;
  }
  elements.activityList.replaceChildren(
    ...visible.map((event) => {
      const data = event.data || event.details || {};
      const item = document.createElement("div");
      const isPreview = event.type === "mutation.preview" || event.status === "preview";
      item.className = `activity-item${isPreview ? " preview" : ""}`;
      const title = document.createElement("strong");
      title.textContent = EVENT_LABELS[event.type] || event.type || "工具事件";
      const detail = document.createElement("span");
      detail.textContent = eventDetail(event);
      item.append(title, detail);
      if (data.diff) {
        const disclosure = document.createElement("details");
        const summary = document.createElement("summary");
        const pre = document.createElement("pre");
        summary.textContent = "查看差异";
        pre.textContent = data.diff;
        disclosure.append(summary, pre);
        item.append(disclosure);
      }
      return item;
    }),
  );
}

function renderReceipt(hash, verified = true) {
  if (!hash) {
    elements.receipt.hidden = true;
    return;
  }
  elements.receipt.hidden = false;
  elements.receiptHash.textContent = hash;
  elements.receiptHash.title = hash;
  elements.receiptState.textContent = verified ? "链路已验证" : "已记录";
}

function setCurrentSession(sessionId) {
  state.sessionId = sessionId || null;
  elements.sessionId.textContent = sessionId || "新会话";
  elements.sessionId.title = sessionId || "";
  document.querySelectorAll(".session-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.sessionId === sessionId);
  });
}

function renderSessions(sessions) {
  elements.sessionCount.textContent = String(sessions.length);
  if (!sessions.length) {
    elements.sessionList.innerHTML =
      '<p class="sidebar-empty">运行第一个任务后，会话会保存在这里。</p>';
    return;
  }
  elements.sessionList.replaceChildren(
    ...sessions.map((session) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "session-item";
      button.dataset.sessionId = session.id;
      button.title = session.id;
      const title = document.createElement("strong");
      title.textContent = session.last_prompt || "尚未完成的任务";
      const meta = document.createElement("span");
      meta.textContent = `${session.run_count} 次运行 · ${formatTime(session.updated_at)}`;
      button.append(title, meta);
      button.addEventListener("click", () => loadSession(session.id));
      if (session.id === state.sessionId) button.classList.add("active");
      return button;
    }),
  );
}

async function loadSessions() {
  try {
    const payload = await request("/api/sessions?limit=20");
    renderSessions(payload.sessions || []);
  } catch (error) {
    showToast(`无法读取会话：${error.message}`);
  }
}

async function loadSession(sessionId) {
  if (state.busy) return;
  try {
    const payload = await request(`/api/sessions/${encodeURIComponent(sessionId)}`);
    elements.conversation.replaceChildren();
    elements.welcome = null;
    for (const run of payload.runs || []) {
      appendMessage("user", run.prompt);
      if (run.response) {
        appendMessage("agent", run.response);
      } else if (run.error) {
        appendMessage("agent", run.error, { error: true });
      }
    }
    if (!(payload.runs || []).length) {
      appendMessage("agent", "这个会话还没有完成的运行。", { error: true });
    }
    setCurrentSession(sessionId);
    renderActivity(payload.tool_events || []);
    const receipts = payload.receipts || [];
    renderReceipt(
      receipts.at(-1)?.receipt_hash,
      Boolean(payload.receipt_chain?.verified),
    );
  } catch (error) {
    showToast(`无法载入会话：${error.message}`);
  }
}

function renderStatus(payload) {
  state.status = payload;
  elements.version.textContent = `v${payload.version}`;
  const providerName = payload.provider === "ollama" ? "Ollama" : "兼容 API";
  elements.modelName.textContent = `${providerName} · ${payload.model}`;
  elements.modelName.title = payload.model;
  elements.workspaceName.textContent = shortPath(payload.workspace);
  elements.workspaceName.title = payload.workspace;
  const modelStatus = payload.model_status || {};
  elements.connection.classList.remove("ready", "error");
  if (modelStatus.state === "ready") {
    elements.connection.classList.add("ready");
    elements.connectionLabel.textContent = "模型已就绪";
  } else if (modelStatus.state === "unverified") {
    elements.connectionLabel.textContent = "接口已连接，模型待验证";
  } else {
    elements.connection.classList.add("error");
    elements.connectionLabel.textContent = modelStatus.detail || "模型服务未连接";
  }
  elements.connection.title = modelStatus.detail || "";
  setMode(state.mode);
}

async function loadStatus() {
  try {
    renderStatus(await request("/api/status"));
  } catch (error) {
    elements.connection.classList.add("error");
    elements.connectionLabel.textContent = "本地服务异常";
    showToast(error.message);
  }
}

function resetSession() {
  setCurrentSession(null);
  elements.conversation.innerHTML = `
    <div class="welcome" id="welcome">
      <div class="welcome-index">01</div>
      <h3>把任务交给 Agent，从这里开始。</h3>
      <p>任务内容会发送到你配置的模型服务。输入任务开始新会话。</p>
      <div class="suggestions" aria-label="任务示例">
        <button type="button" data-prompt="概览工作区，并告诉我最重要的三个文件。">概览工作区 <span aria-hidden="true">↗</span></button>
        <button type="button" data-prompt="搜索工作区里的 TODO 和 FIXME，并按优先级总结。">查找待办项 <span aria-hidden="true">↗</span></button>
        <button type="button" data-prompt="阅读 README 文件，指出表达不清或缺失的内容。">审查 README <span aria-hidden="true">↗</span></button>
      </div>
    </div>`;
  elements.welcome = document.querySelector("#welcome");
  bindSuggestions();
  resetActivity();
  renderReceipt(null);
  elements.prompt.value = "";
  elements.prompt.focus();
}

async function runPrompt(prompt) {
  if (state.busy || !prompt.trim()) return;
  appendMessage("user", prompt.trim());
  appendTyping();
  setBusy(true);
  try {
    const payload = await request("/api/run", {
      method: "POST",
      body: JSON.stringify({
        prompt: prompt.trim(),
        session_id: state.sessionId,
        mode: currentMode(),
      }),
    });
    document.querySelector("#typing-message")?.remove();
    appendMessage("agent", payload.response);
    setCurrentSession(payload.session_id);
    renderActivity(payload.events || []);
    renderReceipt(payload.receipt_hash, Boolean(payload.receipt_chain_verified));
    elements.prompt.value = "";
    await loadSessions();
  } catch (error) {
    document.querySelector("#typing-message")?.remove();
    appendMessage("agent", error.message, { error: true });
    showToast(error.message);
    if (error.sessionId) {
      setCurrentSession(error.sessionId);
      await loadSessions();
    }
  } finally {
    setBusy(false);
    elements.prompt.focus();
  }
}

function bindSuggestions() {
  document.querySelectorAll("[data-prompt]").forEach((button) => {
    button.addEventListener("click", () => {
      elements.prompt.value = button.dataset.prompt || "";
      elements.prompt.focus();
    });
  });
}

elements.composer.addEventListener("submit", (event) => {
  event.preventDefault();
  runPrompt(elements.prompt.value);
});

elements.prompt.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    elements.composer.requestSubmit();
  }
});

document.querySelectorAll('input[name="mode"]').forEach((input) => {
  input.addEventListener("change", () => setMode(currentMode()));
});

elements.newSession.addEventListener("click", resetSession);

bindSuggestions();
setMode("read");
Promise.all([loadStatus(), loadSessions()]);
