"use strict";

(() => {
  const tabs = ["runs", "messages", "context", "memory"];
  const terminal = new Set(["completed", "failed", "cancelled", "interrupted"]);
  const runStates = { "run.started": "running", "run.completed": "completed", "run.failed": "failed", "run.cancelled": "cancelled", "run.interrupted": "interrupted" };
  const labels = { running: "运行中", completed: "已完成", failed: "失败", cancelled: "已取消", interrupted: "已中断", observed: "仅有观察记录" };
  const memoryLabels = { "memory.retrieved": "记忆检索", "memory.proposed": "候选记忆 · 待批准", "memory.committed": "记忆已提交", "memory.rejected": "候选记忆未提交" };
  const memoryStates = {
    candidate: "候选待批准 · 未生效",
    approved: "已批准 · 可参与检索",
    rejected: "已拒绝 · 未生效",
    forgotten: "已忘记 · 不参与检索",
    superseded: "已替换 · 不参与检索",
  };
  const byId = (id) => document.getElementById(id);
  const state = { events: [], runs: new Map(), cursor: 0, hubId: null, tab: "runs", run: "", connection: "loading", lastResponse: 0, lastEvent: null, errorSince: null, source: null, timer: null, repairing: false, retention: {}, meta: {}, openDetails: new Set() };
  Object.assign(state, {
    memory: { enabled: null, scope: null, records: [] },
    memoryUpdatedAt: null,
    memoryError: null,
    memoryTimer: null,
    memoryFetching: false,
    memoryDirty: false,
  });

  function fromUrl() {
    const params = new URL(window.location.href).searchParams;
    state.tab = tabs.includes(params.get("tab")) ? params.get("tab") : "runs";
    state.run = params.get("run")?.slice(0, 120) || "";
  }

  function toUrl() {
    const url = new URL(window.location.href);
    url.searchParams.set("tab", state.tab);
    if (state.run) url.searchParams.set("run", state.run);
    else url.searchParams.delete("run");
    if (url.href !== window.location.href) window.history.pushState(null, "", url);
  }

  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = String(text);
    return item;
  }

  function formatTime(value) {
    if (value === null || value === undefined) return "未知";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "未知" : new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date);
  }

  function number(value, unit = "") {
    return typeof value === "number" && Number.isFinite(value) ? `${value.toLocaleString("zh-CN")}${unit}` : "未知";
  }

  function tokenUsage(usage) {
    if (!usage || usage.token_usage_known === false) return "未知 / 未报告";
    const input = usage.input_tokens ?? usage.request_tokens;
    const output = usage.output_tokens ?? usage.response_tokens;
    const total = input + output;
    if (!Number.isSafeInteger(input) || !Number.isSafeInteger(output) || input < 0 || output < 0 || !Number.isSafeInteger(total) || total <= 0) return "未知 / 未报告";
    return `${number(total)} tokens（已报告）`;
  }

  function details(key, title, value) {
    const element = node("details");
    element.open = state.openDetails.has(key);
    element.append(node("summary", null, title));
    const text = typeof value === "string" ? value : JSON.stringify(value, null, 2) || "未观察到内容";
    element.append(node("pre", null, text.slice(0, 64_000) + (text.length > 64_000 ? "\n[显示长度已限制]" : "")));
    element.addEventListener("toggle", () => {
      if (!element.isConnected) return;
      if (element.open) state.openDetails.add(key);
      else state.openDetails.delete(key);
      if (state.openDetails.size > 512) state.openDetails.delete(state.openDetails.values().next().value);
    });
    return element;
  }

  function card(title, event) {
    const element = node("article", "observation-card");
    const heading = node("div", "card-heading");
    const time = node("time", null, formatTime(event.updated_at || event.created_at));
    time.dateTime = event.updated_at || event.created_at || "";
    heading.append(node("h3", null, title), time);
    element.append(heading);
    return element;
  }

  function measurements(values) {
    const list = node("dl", "measurements");
    for (const [label, value] of values) {
      const item = node("div");
      item.append(node("dt", null, label), node("dd", null, value));
      list.append(item);
    }
    return list;
  }

  function selectedEvents() {
    return state.events.filter((event) => !state.run || event.execution_id === state.run || event.data?.parent_execution_id === state.run);
  }

  function empty(container, message) {
    container.append(node("p", "empty", message));
  }

  function eventDetails(event, title) {
    const element = card(title, event);
    element.append(node("p", "card-caption", `事件 #${event.cursor} · 运行 ${event.execution_id || "未关联"}`));
    if (event.observation_truncated) element.append(node("p", "card-caption", "内容超过观察上限，已裁剪；这不是完整记录。"));
    element.append(details(`event-${event.cursor}`, "查看已观察的数据", event.data || {}));
    return element;
  }

  function renderRuns(container) {
    const runs = [...state.runs.values()].reverse().filter((run) => !state.run || run.execution_id === state.run || run.parent_execution_id === state.run);
    for (const run of runs) {
      const element = card(run.execution_id, run);
      const badge = node("span", "state-badge", labels[run.state] || run.state);
      badge.dataset.state = run.state;
      element.querySelector("h3").prepend(badge);
      element.append(measurements([["Agent", run.agent_id || "local"], ["会话", run.session_id || "未知"], ["Tokens", tokenUsage(run.usage)]]));
      element.append(node("p", "card-caption", `最近事件：${run.last_event || "未知"} · 已观察 ${run.events_seen || 0} 条${run.parent_execution_id ? ` · 父运行 ${run.parent_execution_id}` : ""}`));
      const related = state.events.filter((event) => event.execution_id === run.execution_id && (event.type.startsWith("plan.") || event.type.startsWith("peer.")));
      if (related.length) {
        const list = node("div", "detail-list");
        for (const event of related) {
          const title = event.type === "plan.generated" ? "计划已生成" : `${event.data?.peer_name || event.data?.agent_id || "子任务"} · ${event.type.replace("peer.", "")}`;
          list.append(details(`event-${event.cursor}`, title, event.data));
        }
        element.append(list);
      }
      container.append(element);
    }
    if (!runs.length) empty(container, state.run ? "此运行不在当前保留窗口。链接选择仍保留；可切换到全部近期运行。" : "暂无运行。请在工作台提交任务；没有事件不代表 Agent 已完成任务。");
  }

  function renderMessages(container) {
    const latest = new Map();
    for (const event of selectedEvents()) {
      if (event.type !== "message.observed" || event.data?.scope !== "original") continue;
      const key = event.execution_id || `unassociated-${event.cursor}`;
      if (!latest.has(key) || event.cursor > latest.get(key).cursor) latest.set(key, event);
    }
    const knownParts = new Set(["system-prompt", "user-prompt", "tool-return", "retry-prompt", "text", "tool-call", "thinking", "builtin-tool-call", "builtin-tool-return"]);
    for (const event of [...latest.values()].sort((left, right) => right.cursor - left.cursor)) {
      const data = event.data || {};
      const messages = Array.isArray(data.messages) ? data.messages : null;
      const complete = messages !== null && !event.observation_truncated;
      const kindsKnown = complete && messages.every((message) => message && (message.kind === "request" || message.kind === "response"));
      const partsKnown = kindsKnown && messages.every((message) => Array.isArray(message?.parts) && message.parts.every((part) => part && (knownParts.has(part.part_kind) || part.notice === "Hidden reasoning is not observed")));
      const parts = partsKnown ? messages.flatMap((message) => message.parts) : [];
      const requests = kindsKnown ? messages.filter((message) => message.kind === "request").length : null;
      const responses = kindsKnown ? messages.filter((message) => message.kind === "response").length : null;
      const calls = parts.filter((part) => part.part_kind === "tool-call" || part.part_kind === "builtin-tool-call").length;
      const returns = parts.filter((part) => part.part_kind === "tool-return" || part.part_kind === "builtin-tool-return").length;
      const element = card("会话消息 · 最新原始快照", event);
      element.append(measurements([
        ["Message 条目", kindsKnown ? number(messages.length) : "未知（观察副本不完整）"],
        ["请求 / 响应 Message", kindsKnown ? `${number(requests)} / ${number(responses)}` : "未知"],
        ["工具调用 / 返回片段", partsKnown ? `${number(calls)} / ${number(returns)}` : "未知"],
      ]));
      element.append(node("p", "card-caption", `运行 ${event.execution_id || "未关联"} · 最新事件 #${event.cursor}${event.observation_truncated ? " · 内容已裁剪" : ""}`));
      element.append(node("p", "card-caption", "快照可能包含载入的历史。请求 Message 数不是 API 调用数，工具片段不代表副作用成功。"));
      element.append(details(`messages-${event.execution_id || event.cursor}`, "查看原始 Message 观察快照", data.messages ?? data.message ?? data.content ?? data));
      container.append(element);
    }
    if (!latest.size) empty(container, "当前窗口没有原始 Message 观察事件。可能尚未运行、模块未开启或内容已被淘汰；这里不补造归档历史。");
  }

  function renderContext(container) {
    const events = selectedEvents().filter((event) => event.type.startsWith("context.")).reverse();
    for (const event of events) {
      const data = { ...(event.data?.details || {}), ...(event.data || {}) };
      const rejected = event.type === "context.rejected" || data.status === "rejected";
      const element = card(rejected ? "上下文预算拒绝 · 未提交模型" : data.reduced ? "压缩后的提交前视图" : "提交前视图", event);
      element.append(measurements([["原始 Message", number(data.original_bytes, " B")], ["提交 Message", rejected ? "未提交" : number(data.submitted_bytes, " B")], ["字节预算", number(data.max_bytes, " B")]]));
      element.append(node("p", "card-caption", `运行 ${event.execution_id || "未关联"} · Tokens：${tokenUsage(data.usage)}${event.observation_truncated ? " · 观察内容已裁剪" : ""}`));
      const messages = data.messages ?? data.model_view ?? data.submitted_messages;
      if (messages !== undefined && !rejected) element.append(details(`context-${event.cursor}`, "查看提交前 Message 视图", messages));
      else element.append(node("p", "card-caption", rejected ? "预算检查失败，没有本次提交视图。" : "此事件仅提供预算元数据，未保留提交内容。"));
      container.append(element);
    }
    if (!events.length) empty(container, "当前窗口没有上下文检查事件。Message 字节预算与模型 token 窗口不同，不能由会话长度推算精确 tokens。");
  }

  function renderMemory(container) {
    container.append(node("h3", null, "持久化记忆 · 当前作用域"));
    if (state.memory.enabled === false) {
      empty(container, "记忆模块未开启。会话历史独立保存，不会因此变成长期开启的知识记忆。");
    } else if (state.memory.enabled === null) {
      empty(container, "服务未提供持久化记忆快照。保留实时事件，但不推断记忆存储是否启用。");
    } else {
      container.append(node("p", "panel-note", `作用域 ${state.memory.scope || "未知"} · 最新至多 50 条 · ${state.memoryError ? "过期快照" : "快照"}读取 ${formatTime(state.memoryUpdatedAt)}。此列表按作用域读取，不受运行筛选影响；服务约每 15 秒检查 CLI 变更并推送更新。`));
      if (state.memory.truncated || state.memory.notice) {
        container.append(node("p", state.memory.truncated ? "notice" : "panel-note", state.memory.notice || "受限展示：列表受条数和总字节预算限制，并非完整记忆库。"));
      }
      for (const record of state.memory.records) {
        const title = record.kind === "entity" ? `实体 · ${record.entity_key || record.id}` : `知识条目 · ${record.id}`;
        const element = card(title, record);
        const badge = node("span", "state-badge", memoryStates[record.status] || "状态未知 · 不推断生效");
        badge.dataset.state = record.status || "unknown";
        element.querySelector("h3").prepend(badge);
        const source = String(record.source || "未提供来源");
        element.append(measurements([
          ["版本", number(record.revision)],
          ["实体键", record.entity_key || "非实体"],
          ["来源", source.slice(0, 120) + (source.length > 120 ? "…（详情可见）" : "")],
        ]));
        element.append(details(`memory-${record.id}`, "查看内容、来源与实体属性（只读）", {
          content: record.content,
          source: record.source,
          revision: record.revision,
          entity_key: record.entity_key,
          attributes: record.attributes,
          status: record.status,
          replaces_id: record.replaces_id,
        }));
        container.append(element);
      }
      if (!state.memory.records.length) {
        empty(container, state.memoryError ? "记忆存储当前不可读，没有可用的旧列表；不能据此判断库为空。" : state.memory.truncated ? "受限窗口内没有可显示的条目，不代表记忆库为空。" : "此作用域暂无持久化记忆。只有明确批准的条目可被长期记忆检索；候选不会自动生效。");
      }
    }
    if (state.memoryError) container.append(node("p", "notice", state.memoryError));
    container.append(node("h3", null, "实时记忆操作 · 当前观察窗口"));
    const events = selectedEvents().filter((event) => event.type.startsWith("memory.") || event.type.startsWith("entity.")).reverse();
    for (const event of events) {
      const title = memoryLabels[event.type] || (event.type.startsWith("entity.") ? "实体记忆事件" : event.type);
      const element = eventDetails(event, title);
      if (event.data?.query) element.insertBefore(node("p", "card-caption", `检索：${event.data.query}`), element.lastChild);
      container.append(element);
    }
    if (!events.length) empty(container, "尚未观察到记忆检索、候选或提交。会话历史不是长期知识记忆；没有事件也可能表示可选记忆模块未开启。");
  }

  function connectionStatus() {
    const badge = byId("connection-state");
    badge.dataset.state = state.connection;
    badge.textContent = { loading: "正在连接", live: "实时连接", stale: "内容已过期 · 重连中", offline: "离线 · 保留最后内容" }[state.connection];
    byId("connection-detail").textContent = state.connection === "live" ? "只读事件流；断开观察不会取消任务。" : state.lastResponse ? `最近响应 ${formatTime(state.lastResponse)}；不会清空已有记录。` : "尚未取得观察快照，不推断任务状态。";
    byId("running-label").textContent = state.connection === "live" ? "运行中" : "最近记录运行中";
  }

  function render() {
    state.timer = null;
    connectionStatus();
    byId("running-count").textContent = String([...state.runs.values()].filter((run) => run.state === "running").length);
    byId("event-count").textContent = `${state.events.length} / ${state.retention.max_events || 256}`;
    byId("last-updated").textContent = state.lastEvent ? formatTime(state.lastEvent) : "尚无事件";
    byId("window-summary").textContent = `内存窗口 · 游标 ${state.cursor}${state.retention.discarded_events ? ` · 已淘汰 ${state.retention.discarded_events} 条` : ""}${state.meta.observer_errors ? ` · ${state.meta.observer_errors} 次观察异常` : ""}`;
    const options = [new Option("全部近期运行", ""), ...[...state.runs.keys()].reverse().map((id) => new Option(id, id))];
    if (state.run && !state.runs.has(state.run)) options.push(new Option(`${state.run}（已不在窗口）`, state.run));
    byId("run-select").replaceChildren(...options);
    byId("run-select").value = state.run;
    for (const tab of tabs) {
      const active = tab === state.tab;
      byId(`tab-${tab}`).setAttribute("aria-selected", String(active));
      byId(`tab-${tab}`).tabIndex = active ? 0 : -1;
      byId(`panel-${tab}`).hidden = !active;
    }
    const container = byId(`${state.tab}-content`);
    container.replaceChildren();
    ({ runs: renderRuns, messages: renderMessages, context: renderContext, memory: renderMemory })[state.tab](container);
    document.body.dataset.renderReady = "true";
  }

  function schedule() {
    if (state.timer === null) state.timer = window.setTimeout(render, document.hidden ? 750 : 200);
  }

  function acceptEvent(event) {
    if (!Number.isInteger(event.cursor) || event.cursor <= state.cursor) return;
    state.events.push(event);
    if (state.events.length > Math.min(256, state.retention.max_events || 256)) state.events.shift();
    state.cursor = event.cursor;
    state.lastEvent = event.created_at;
    const id = event.execution_id;
    if (!id) return;
    const data = event.data || {};
    const run = state.runs.get(id) || { execution_id: id, session_id: event.session_id, agent_id: data.agent_id || "local", parent_execution_id: data.parent_execution_id, state: "observed", events_seen: 0 };
    if (runStates[event.type] && !terminal.has(run.state)) run.state = runStates[event.type];
    run.events_seen += 1;
    run.updated_at = event.created_at;
    run.last_event = event.type;
    if (data.usage && event.type.startsWith("run.")) run.usage = data.usage;
    state.runs.delete(id);
    state.runs.set(id, run);
    if (state.runs.size > Math.min(32, state.retention.max_runs || 32)) state.runs.delete(state.runs.keys().next().value);
  }

  async function getSnapshot() {
    const response = await fetch("/api/inspector/snapshot", { headers: { Accept: "application/json" }, cache: "no-store" });
    if (!response.ok) throw new Error("观察快照不可用");
    return response.json();
  }

  function applyMemory(payload) {
    if (!payload.memory || typeof payload.memory !== "object") return false;
    if (payload.memory.error) {
      if (state.memory.enabled === null && typeof payload.memory.enabled === "boolean") state.memory.enabled = payload.memory.enabled;
      if (state.memory.scope === null && typeof payload.memory.scope === "string") state.memory.scope = payload.memory.scope;
      const errorType = typeof payload.memory.error === "string" ? payload.memory.error.slice(0, 64) : "未知类型";
      state.memoryError = `记忆列表已过期：存储读取失败（${errorType}）。保留上次成功快照，不将故障当成空记忆库。`;
      return true;
    }
    state.memory = {
      enabled: typeof payload.memory.enabled === "boolean" ? payload.memory.enabled : null,
      scope: payload.memory.scope || null,
      records: Array.isArray(payload.memory.records) ? payload.memory.records.slice(0, 50) : [],
      truncated: payload.memory.truncated === true,
      notice: typeof payload.memory.notice === "string" ? payload.memory.notice.slice(0, 500) : null,
    };
    state.memoryUpdatedAt = Date.now();
    state.memoryError = null;
    return true;
  }

  async function refreshMemory() {
    if (state.memoryFetching) { state.memoryDirty = true; return; }
    state.memoryFetching = true;
    state.memoryDirty = false;
    byId("memory-refresh").disabled = true;
    try { applyMemory(await getSnapshot()); }
    catch { state.memoryError = "持久化记忆列表未能刷新，保留上次快照；实时连接状态不代表此列表已更新。"; }
    finally {
      state.memoryFetching = false;
      byId("memory-refresh").disabled = false;
      if (state.tab === "memory") schedule();
      if (state.memoryDirty) scheduleMemoryRefresh();
    }
  }

  function scheduleMemoryRefresh() {
    state.memoryDirty = true;
    if (state.memoryTimer !== null || state.memoryFetching) return;
    state.memoryTimer = window.setTimeout(() => {
      state.memoryTimer = null;
      refreshMemory();
    }, 1000);
  }

  async function snapshot() {
    const payload = await getSnapshot();
    applyMemory(payload);
    state.events = (payload.events || []).slice(-256);
    state.runs = new Map((payload.runs || []).slice(0, 32).reverse().map((run) => [run.execution_id, run]));
    state.cursor = payload.cursor || 0;
    state.meta = payload.meta || {};
    state.hubId = state.meta.hub_id || null;
    state.retention = payload.retention || {};
    state.lastEvent = state.events.at(-1)?.created_at || null;
    state.lastResponse = Date.now();
    schedule();
  }

  async function repair() {
    if (state.repairing) return;
    state.repairing = true;
    state.source?.close();
    byId("gap-notice").hidden = false;
    byId("gap-notice").textContent = "观察窗口出现缺口或服务已重启，正在重新读取快照；此前被淘汰的原始内容不能恢复。";
    try {
      await snapshot();
      byId("gap-notice").textContent = "已重新读取最近快照。观察记录存在缺口，被淘汰或重启前的原始内容不能在此恢复。";
      connect();
    }
    catch { state.connection = "stale"; schedule(); window.setTimeout(repair, 3000); }
    finally { state.repairing = false; }
  }

  function connect() {
    state.source?.close();
    state.source = new EventSource(`/api/inspector/events?cursor=${state.cursor}`);
    state.source.onopen = () => { state.connection = "live"; state.errorSince = null; state.lastResponse = Date.now(); connectionStatus(); };
    state.source.onmessage = (message) => {
      try {
        const payload = JSON.parse(message.data);
        state.lastResponse = Date.now();
        state.connection = "live";
        state.errorSince = null;
        if (payload.gap || payload.reset || (state.hubId && payload.hub_id && payload.hub_id !== state.hubId)) { repair(); return; }
        const memoryChanged = applyMemory(payload);
        if (!(payload.events || []).length) {
          if (memoryChanged && state.tab === "memory") schedule();
          else connectionStatus();
          return;
        }
        for (const event of payload.events || []) acceptEvent(event);
        if (!payload.memory && (payload.events || []).some((event) => event.type.startsWith("memory.") || event.type.startsWith("entity."))) scheduleMemoryRefresh();
        state.retention.discarded_events = Math.max(0, state.cursor - state.events.length);
        schedule();
      } catch {
        state.connection = "stale";
        byId("gap-notice").hidden = false;
        byId("gap-notice").textContent = "收到无法解析的观察数据，保留最后可用内容；不推断执行结果。";
        schedule();
      }
    };
    state.source.onerror = () => { state.errorSince ??= Date.now(); state.connection = state.events.length || state.runs.size ? "stale" : "offline"; schedule(); };
  }

  for (const [index, tab] of tabs.entries()) {
    byId(`tab-${tab}`).addEventListener("click", () => { state.tab = tab; toUrl(); schedule(); });
    byId(`tab-${tab}`).addEventListener("keydown", (event) => {
      const next = event.key === "ArrowRight" ? (index + 1) % tabs.length : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : null;
      if (next === null) return;
      event.preventDefault();
      state.tab = tabs[next];
      toUrl();
      schedule();
      byId(`tab-${state.tab}`).focus();
    });
  }
  byId("run-select").addEventListener("change", () => { state.run = byId("run-select").value; toUrl(); schedule(); });
  byId("memory-refresh").addEventListener("click", refreshMemory);
  window.addEventListener("popstate", () => { fromUrl(); schedule(); });
  window.addEventListener("pagehide", () => state.source?.close());
  window.addEventListener("pageshow", (event) => { if (event.persisted) repair(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) schedule(); });
  window.setInterval(() => {
    if (state.errorSince && Date.now() - state.errorSince > 60_000) state.connection = "offline";
    else if (state.lastResponse && Date.now() - state.lastResponse > 45_000) state.connection = "stale";
    connectionStatus();
  }, 5000);
  fromUrl();
  snapshot().then(connect).catch(() => { state.connection = "offline"; schedule(); window.setTimeout(repair, 3000); });
})();
