// Violet Control Center (phase 9.0). Everything is built with DOM nodes and textContent; data is never parsed as HTML.
"use strict";

const $ = (sel) => document.querySelector(sel);
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) if (c !== null && c !== undefined) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}
function toast(text, ok = true) {
  const t = $("#toast"); t.textContent = text; t.className = "toast show " + (ok ? "ok" : "bad");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => (t.className = "toast"), 3500);
}
async function api(method, url, body) {
  const opts = { method, headers: { Accept: "application/json" }, credentials: "same-origin" };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const resp = await fetch(url, opts);
  if (resp.status === 401) { location.href = "/auth/login?next=/control"; throw new Error("unauthorized"); }
  let data = null; try { data = await resp.json(); } catch (_) { /* not json */ }
  if (!resp.ok || (data && data.ok === false)) {
    const err = new Error((data && (data.error)) || ("请求失败 " + resp.status)); err.data = data; throw err;
  }
  return data && "data" in data ? data.data : data;
}
const fmt = (v) => (v === null || v === undefined || v === "" ? "—" : String(v));
const when = (iso) => (iso ? String(iso).replace("T", " ").slice(0, 16) : "—");
function confirmBox(text) {
  return new Promise((resolve) => {
    const d = $("#confirm"); $("#confirm-text").textContent = text; $("#confirm-reason").value = "";
    d.returnValue = ""; d.showModal();
    d.addEventListener("close", () => resolve(d.returnValue === "ok" ? { reason: $("#confirm-reason").value } : null), { once: true });
  });
}

// ---------------------------------------------------------------- tabs
const loaders = {};
document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + b.dataset.tab));
  history.replaceState(null, "", "#" + b.dataset.tab);
  document.title = b.textContent.trim() + " · Violet";
  setMenu(false);                                   // narrow screens: the menu closes after choosing a page
  window.scrollTo({ top: 0 });
  (loaders[b.dataset.tab] || (() => {}))();
}));
// Violet stage 3: go to a page and then to one place on it (the start page's checklist, the jump bars). Page
// loaders fill their lists asynchronously, so the target is looked for a few times before giving up.
function goTo(tab, anchor) {
  const btn = document.querySelector(`#tabs button[data-tab="${CSS.escape(tab)}"]`);
  if (btn && !btn.classList.contains("active")) btn.click();
  if (anchor) scrollToAnchor(anchor);
}
function scrollToAnchor(id, tries = 25) {
  const el = document.getElementById(id);
  if (!el || !el.offsetParent) { if (tries > 0) setTimeout(() => scrollToAnchor(id, tries - 1), 120); return; }
  if (el.tagName === "DETAILS") el.open = true;
  el.scrollIntoView({ behavior: "smooth", block: "start" });
  el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
}
// a row of buttons at the top of a long page: one per section, scrolls there
function jumpNav(nav, items) {
  nav.replaceChildren(...items.filter(Boolean).map(([label, id]) =>
    h("button", { type: "button", text: label, onclick: () => scrollToAnchor(id) })));
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast("已复制"); return; } catch (_) { /* not a secure context */ }
  const area = h("textarea", { readonly: true, style: "position:fixed;top:-1000px;opacity:0" }); area.value = text;
  document.body.append(area); area.select();
  let done = false; try { done = document.execCommand("copy"); } catch (_) { done = false; }
  area.remove();
  toast(done ? "已复制" : "复制失败：请手动选中复制", done);
}
// narrow screens: the sidebar is a top bar with a menu button
function setMenu(open) {
  $("#side").classList.toggle("open", open);
  $("#menu-toggle").setAttribute("aria-expanded", String(open));
}
$("#menu-toggle").addEventListener("click", () => setMenu(!$("#side").classList.contains("open")));

// ---------------------------------------------------------------- overview
function card(k, v, cls) { return h("div", { class: "card" }, h("div", { class: "k", text: k }), h("div", { class: "v " + (cls || ""), text: fmt(v) })); }
// a status tile: a coloured dot (ok / bad / warn / none) before the label
function stat(k, v, state) { const c = card(k, v); c.classList.add("stat"); if (state) c.classList.add(state); return c; }
function pill(text, kind) { return h("span", { class: "pill " + (kind || ""), text }); }
function messageCount(m) {
  if (!m || typeof m !== "object") return fmt(m);
  if (m.total !== undefined) return String(m.total);
  const got = m.received_count ?? m.received, sent = m.sent_count ?? m.sent;
  return got !== undefined || sent !== undefined ? `收 ${fmt(got)} · 发 ${fmt(sent)}` : "—";
}
function lifeText(life) {
  if (!life.mode) return "—";
  const mode = { on: "运行中", off: "已关闭", paused: "已暂停" }[life.mode] || life.mode;
  return life.last_run_key ? `${mode}（上次 ${life.last_run_key}：${fmt(life.last_outcome)}）` : mode;
}
loaders.overview = async () => {
  try {
    const d = await api("GET", "/api/control/overview");
    const s = d.service || {}, p = d.proactive || {}, life = d.life || {}, w = d.wechat || {};
    const alive = (x) => (x ? "ok" : "bad");
    const cards = $("#ov-cards"); cards.replaceChildren(
      stat("微信", w.logged_in ? "已登录" : "未登录", w.logged_in ? "ok" : "warn"),
      stat("收消息线程", s.poll_thread_alive ? "运行中" : "未运行", alive(s.poll_thread_alive)),
      stat("回复线程", s.worker_thread_alive ? "运行中" : "未运行", alive(s.worker_thread_alive)),
      stat("记忆线程", s.memory_worker_alive ? "运行中" : "未运行", alive(s.memory_worker_alive)),
      stat("排队中的对话", s.queued_turns ?? 0),
      stat("主动联系", p.enabled === false ? "已关闭" : (p.due_reason || "—")),
      stat("今天主动", p.daily_sent_count === undefined ? "—" : `${p.daily_sent_count} / ${fmt(p.daily_message_limit)}`),
      stat("生活引擎", lifeText(life), life.mode === "on" ? "ok" : (life.mode ? "warn" : "")),
      stat("消息总数", messageCount(d.messages)),
      stat("设置修改次数", d.settings_revision ?? 0));
    const agents = $("#ov-agents");
    agents.replaceChildren(h("tr", {}, h("th", { text: "Agent" }), h("th", { text: "模型" }), h("th", { text: "备用模型" }), h("th", { text: "温度" })),
      ...(d.agents || []).map((a) => h("tr", {}, h("td", { text: a.agent }), h("td", { text: fmt(a.model) }), h("td", { text: fmt(a.fallback_model) }), h("td", { text: fmt(a.temperature) }))));
    const errors = $("#ov-errors");
    errors.replaceChildren(h("tr", {}, h("th", { text: "时间" }), h("th", { text: "位置" }), h("th", { text: "类型" }), h("th", { text: "编号" })),
      ...((d.errors || []).length ? d.errors.map((e) => h("tr", {}, h("td", { text: when(e.at) }), h("td", { text: e.where }), h("td", { text: fmt(e.type) }), h("td", { text: e.id })))
        : [h("tr", {}, h("td", { colspan: 4, text: "没有记录到失败" }))]));
    const aux = (d.aux_calls_24h || []).map((r) => `${r.status} ${r.n} 次`).join("，") || "无";
    $("#ov-note").textContent = `辅助模型最近 24 小时：${aux}。${d.notes ? d.notes.model_calls : ""}`;
  } catch (e) { toast("概览读取失败：" + e.message, false); }
};
$("#ov-refresh").addEventListener("click", () => loaders.overview());

// ---------------------------------------------------------------- chat
const KIND = { user: "对方", violet: "Violet 回复", proactive: "Violet 主动", reminder: "提醒", system: "系统" };
let chatBefore = null;
async function loadChat(append) {
  const params = new URLSearchParams({ limit: "100" });
  if ($("#chat-day").value) params.set("date", $("#chat-day").value);
  if ($("#chat-role").value) params.set("role", $("#chat-role").value);
  if ($("#chat-q").value.trim()) params.set("q", $("#chat-q").value.trim());
  if (append && chatBefore) params.set("before_id", chatBefore);
  try {
    const d = await api("GET", "/api/control/messages?" + params);
    const list = $("#chat-list"); const nodes = []; let day = null;
    for (const m of d.messages) {
      const dd = (m.created_at || "").slice(0, 10);
      if (dd !== day) { nodes.push(h("div", { class: "day", text: dd })); day = dd; }
      nodes.push(h("div", { class: "msg " + (m.role === "user" ? "user" : "mine") },
        h("div", { class: "meta" }, h("span", { class: "tag " + m.kind, text: KIND[m.kind] || m.kind }), h("span", { text: when(m.created_at) }),
          m.message_type !== "text" ? h("span", { text: "[" + m.message_type + "]" }) : null),
        h("div", { text: m.content || "" })));
    }
    if (append) list.prepend(...nodes); else list.replaceChildren(...(nodes.length ? nodes : [h("p", { class: "note", text: "没有符合条件的消息" })]));
    chatBefore = d.next_before_id; $("#chat-more").hidden = !chatBefore;
  } catch (e) { toast("聊天记录读取失败：" + e.message, false); }
}
loaders.chat = async () => {
  try {
    const days = await api("GET", "/api/control/messages/days");
    const sel = $("#chat-day"); const keep = sel.value;
    sel.replaceChildren(h("option", { value: "", text: "全部" }), ...days.map((d) => h("option", { value: d.day, text: `${d.day}（${d.n}）` })));
    sel.value = keep;
  } catch (_) { /* the list below reports */ }
  loadChat(false);
};
$("#chat-search").addEventListener("click", () => loadChat(false));
$("#chat-more").addEventListener("click", () => loadChat(true));
$("#exp-go").addEventListener("click", () => {
  const f = $("#exp-from").value, t = $("#exp-to").value;
  if (!f || !t) return toast("请选择导出的起止日期", false);
  location.href = "/api/control/messages/export?" + new URLSearchParams({ from: f, to: t });
});

// ---------------------------------------------------------------- memories
let memOffset = 0; const MEM_PAGE = 30;
async function loadMemories() {
  const kind = $("#mem-kind").value;
  const params = new URLSearchParams({ kind, limit: String(MEM_PAGE), offset: String(memOffset) });
  if ($("#mem-q").value.trim()) params.set("q", $("#mem-q").value.trim());
  if (kind === "event" && $("#mem-status").value) params.set("status", $("#mem-status").value);
  try {
    const d = await api("GET", "/api/control/memories?" + params);
    if (kind === "event") {
      const sel = $("#mem-status"); const keep = sel.value;
      sel.replaceChildren(h("option", { value: "", text: "全部" }), ...d.statuses.map((s) => h("option", { value: s.status, text: `${s.status}（${s.n}）` })));
      sel.value = keep;
    }
    $("#mem-status").disabled = kind !== "event";
    $("#mem-total").textContent = `共 ${d.total} 条，第 ${memOffset + 1}–${Math.min(memOffset + MEM_PAGE, d.total)} 条`;
    $("#mem-prev").disabled = memOffset === 0; $("#mem-next").disabled = memOffset + MEM_PAGE >= d.total;
    $("#mem-list").replaceChildren(...(d.memories.length ? d.memories.map((m) => memoryCard(kind, m)) : [h("p", { class: "note", text: "没有记忆" })]));
  } catch (e) { toast("记忆读取失败：" + e.message, false); }
}
function memoryCard(kind, m) {
  const facts = kind === "core"
    ? [`分类 ${fmt(m.category)}`, `键 ${fmt(m.memory_key)}`, `来源 ${fmt(m.source)}`, `优先级 ${fmt(m.priority)}`]
    : [`状态 ${fmt(m.status)}`, `分类 ${fmt(m.category)}`, m.progress ? `进展 ${m.progress}` : null, `重要度 ${fmt(m.importance)}`,
       m.source ? `来源 ${m.source.label}` : null];
  if (m.confirmed_by_user) facts.push("用户已确认");
  for (const c of m.pending_conflicts || []) facts.push(`待确认：“${c.old}”已被纠正为“${c.new}”`);
  return h("div", { class: "card" },
    h("div", { text: m.content }),
    h("div", { class: "badges" }, ...facts.filter(Boolean).map((f) => pill(f)), pill(`更新 ${when(m.updated_at)}`), pill(`修改记录 ${m.revisions}`)),
    h("div", { class: "row", style: "justify-content:flex-end;margin-top:6px" },
      h("button", { class: "ghost", onclick: () => editMemory(kind, m), text: "查看 / 纠正" }),
      (kind === "event" && m.status === "deleted") ? null : h("button", { class: "danger", onclick: () => deleteMemory(kind, m), text: "删除" }),
      kind === "event" ? h("button", { class: "danger", onclick: () => forgetMemory(m), text: "遗忘" }) : null));
}
async function forgetMemory(m) {
  const ok = await confirmBox("遗忘这件事？除了删除这条记忆，还会把它来源那一轮里用户说这件事的句子和 Violet 当时的相关回复，" +
    "从聊天记录中抹掉，Violet 之后从任何地方都看不到。修改记录也会一并删除，不能恢复。\n\n" + m.content);
  if (!ok) return;
  try {
    const r = await api("POST", `/api/control/memories/event/${m.id}/forget`, { confirm: true });
    toast(r.history_note ? "已遗忘这条记忆；" + r.history_note : `已遗忘，聊天记录里改动了 ${r.messages_changed.length} 条消息`);
    loadMemories();
  } catch (e) { toast("遗忘失败：" + e.message, false); }
}
async function editMemory(kind, m) {
  $("#editor-title").textContent = (kind === "core" ? "核心记忆 #" : "事件记忆 #") + m.id;
  $("#editor-text").value = m.content; $("#editor-reason").value = "";
  const hist = $("#editor-history"); hist.replaceChildren(h("div", { text: "读取修改记录…" }));
  const d = $("#editor"); d.returnValue = ""; d.showModal();
  try {
    const r = await api("GET", `/api/control/memories/${kind}/${m.id}/history`);
    const sources = r.source ? [h("div", { text: "来源：" + r.source.label }),
      ...(r.source_messages || []).map((s) => h("div", { text: `${when(s.created_at)} 用户：${s.content || ""}` }))] : [];
    hist.replaceChildren(...sources, ...(r.revisions.length ? r.revisions.slice().reverse().map((v) => h("div", {},
      h("div", { text: `${when(v.created_at)} · ${v.action} · ${fmt(v.source)}${v.reason ? " · " + v.reason : ""}` }),
      v.old_content ? h("div", { text: "原来：" + v.old_content }) : null,
      v.new_content ? h("div", { text: "改为：" + v.new_content }) : null)) : [h("div", { text: "还没有修改记录" })]));
  } catch (e) { hist.replaceChildren(h("div", { text: "修改记录读取失败" })); }
  d.addEventListener("close", async () => {
    if (d.returnValue !== "ok") return;
    try {
      await api("POST", `/api/control/memories/${kind}/${m.id}/correct`, { content: $("#editor-text").value, reason: $("#editor-reason").value });
      toast("已保存纠正，原来的内容记在修改记录里"); loadMemories();
    } catch (e) { toast("保存失败：" + e.message, false); }
  }, { once: true });
}
async function deleteMemory(kind, m) {
  const text = kind === "event" ? `删除这条事件记忆？它会被标记为已删除（可在修改记录中看到原文），Violet 不再使用它。` +
      `聊天记录不变：最近的聊天里如果还有这件事的原话，Violet 仍能看到（要连原话一起抹掉，用“遗忘”）。\n\n${m.content}`
    : `删除这条核心记忆？原文会先写进修改记录。\n\n${m.content}`;
  const ok = await confirmBox(text); if (!ok) return;
  try { await api("POST", `/api/control/memories/${kind}/${m.id}/delete`, { confirm: true, reason: ok.reason }); toast("已删除"); loadMemories(); }
  catch (e) { toast("删除失败：" + e.message, false); }
}
loaders.memories = () => { memOffset = 0; loadMemories(); };

// ---------------------------------------------------------------- tools (phase 9.2)
const TOOL_STATUS = { connected: "已连接", unavailable: "连不上", disabled: "已停用", unknown: "还没检查" };
const PERMISSION = { read: "只读", write: "写入（需另外授权）", high_risk: "高风险（需另外授权）", unknown: "不在白名单（隐藏）" };
function renderTools(servers) {
  $("#tools-updated").textContent = "更新于 " + new Date().toLocaleTimeString();
  $("#tools-list").replaceChildren(...(servers.length ? servers.map((s) => {
    const el = toolServerCard(s); el.id = "tool-srv-" + s.name; return el;
  }) : [h("p", { class: "note", text: "没有配置任何 MCP 服务器" })]));
  jumpNav($("#tools-jump"), [["手机 MCP", "phone-mcp"], ["联网工具", "tools-web"],
    ...servers.filter((s) => s.name !== "violet-web").map((s) => [s.name, "tool-srv-" + s.name]), ["添加服务器", "mcp-add-box"]]);
}
function toolServerCard(s) {
  const facts = [`状态 ${TOOL_STATUS[s.status] || s.status}`, `传输 ${s.transport}`, s.url ? `地址 ${s.url}` : null, s.command,
    `可用于 ${s.agents.join("、") || "—"}`, `提供方式 ${s.offer === "always" ? "每轮都给 Chat" : "需要时才给"}`,
    `调用 ${s.total_calls} 次 / 失败 ${s.total_failures} 次`];
  const tools = s.tools.map((t) => h("div", { class: "row" },
    h("span", { text: `${t.name}：${t.description || ""}` }), pill(PERMISSION[t.permission] || t.permission),
    pill(t.available ? "可用" : "不可用"),
    t.permission === "unknown" ? null : h("button", { class: "ghost", text: t.enabled ? "停用" : "启用",
      onclick: async () => { try { await api("POST", `/api/control/tools/servers/${encodeURIComponent(s.name)}/tools/${encodeURIComponent(t.name)}`, { enabled: !t.enabled }); toast(t.enabled ? "已停用" : "已启用"); loaders.tools(); } catch (e) { toast("失败：" + e.message, false); } } })));
  const headers = s.headers.map((x) => h("div", { class: "row" }, h("span", { text: `${x.header}：${x.is_set ? "已设置（" + x.fingerprint + "）" : "未设置"}` }),
    h("button", { class: "ghost", text: "替换", onclick: () => replaceToolSecret(s.name, x.header) })));
  return h("div", { class: "card" },
    h("div", { class: "row" }, h("strong", { text: s.name }),
      h("button", { class: s.enabled ? "danger" : "ghost", text: s.enabled ? "停用服务器" : "启用服务器",
        onclick: async () => { try { await api("POST", `/api/control/tools/servers/${encodeURIComponent(s.name)}`, { enabled: !s.enabled }); toast("已保存"); loaders.tools(); } catch (e) { toast("失败：" + e.message, false); } } })),
    h("div", { class: "badges" }, ...facts.filter(Boolean).map((f) => pill(f))),
    s.last_error_code ? h("p", { class: "note", text: `最近错误：${s.last_error_code}${s.last_error ? "（" + s.last_error + "）" : ""}` }) : null,
    s.unclassified_tools.length ? h("p", { class: "note", text: "未列入白名单（不会给 Violet）：" + s.unclassified_tools.join("、") }) : null,
    ...tools, ...headers);
}
async function replaceToolSecret(server, header) {
  const value = window.prompt(`替换 ${server} 的 ${header}（输入新值；保存后不会再显示）`);
  if (!value) return;
  try { await api("POST", `/api/control/tools/servers/${encodeURIComponent(server)}/secret`, { header, value, confirm: true }); toast("凭据已替换"); loaders.tools(); }
  catch (e) { toast("替换失败：" + e.message, false); }
}
// phase 9.3: the web tools (search provider, credential replace-only, limits, today's use and failures)
const WEB_SETTING_LABELS = { max_results: "每次搜索结果数", max_searches_per_day: "每天搜索次数上限",
  max_fetches_per_day: "每天读网页次数上限", max_text_chars: "网页正文最多字数" };
function renderWeb(web) {
  const box = $("#tools-web");
  if (!web) { box.replaceChildren(); return; }
  const cred = web.credential.source ? `已设置（${web.credential.source === "dashboard" ? "后台替换" : ".env"}，指纹 ${web.credential.fingerprint}）` : "未设置";
  const facts = [`联网 ${web.enabled ? "已开启" : "未开启（.env 的 VIOLET_WEB_ENABLED）"}`, `搜索服务 ${web.provider}`,
    `搜索 ${web.search_configured ? "可用" : "未配置"}${web.search_problem ? `（${web.search_problem}）` : ""}`, `凭据 ${cred}`,
    `搜索工具 ${web.search_tool ? "开" : "关"} / 网页阅读 ${web.fetch_tool ? "开" : "关"}`,
    `今天 搜索 ${web.used_today.search} 次 · 读网页 ${web.used_today.fetch} 次`];
  const settings = Object.entries(web.settings).map(([k, v]) => h("label", { class: "row" },
    h("span", { text: WEB_SETTING_LABELS[k] || k }),
    h("input", { type: "number", value: v, min: web.limits[k][0], max: web.limits[k][1], "data-web": k })));
  const failures = web.recent_failures.map((f) => h("div", { class: "note", text: `${f.created_at.slice(11, 16)} ${f.kind} ${f.status} ${f.error_code || ""}` }));
  box.replaceChildren(h("div", { class: "card" }, h("h3", { text: "联网工具（violet-web）" }),
    ...facts.map((t) => h("div", { class: "note", text: t })), ...settings,
    h("div", { class: "row" },
      h("button", { text: "保存设置", onclick: async () => {
        const body = {};
        document.querySelectorAll("[data-web]").forEach((i) => { body[i.dataset.web] = parseInt(i.value, 10); });
        try { await api("POST", "/api/control/tools/web/settings", body); toast("已保存"); loaders.tools(); } catch (e) { toast("失败：" + e.message, false); }
      } }),
      h("button", { class: "ghost", text: "替换搜索凭据", onclick: async () => {
        const value = window.prompt("替换搜索服务的 API Key（输入新值；保存后不会再显示）");
        if (!value) return;
        try { await api("POST", "/api/control/tools/web/secret", { value, confirm: true }); toast("凭据已替换"); loaders.tools(); } catch (e) { toast("替换失败：" + e.message, false); }
      } })),
    h("p", { class: "note", text: "搜索服务、SearXNG 地址和搜索语言在“设置 → 功能开关”里改；停用单个联网工具在下面 violet-web 的工具列表里。" }),
    ...(failures.length ? [h("div", { class: "note", text: "今天的失败：" }), ...failures] : [])));
}
loaders.tools = async () => {
  try { const data = await api("GET", "/api/control/tools"); renderTools(data.servers); renderWeb(data.web); }
  catch (e) { toast("工具读取失败：" + e.message, false); }
};
$("#tools-refresh").addEventListener("click", async () => {
  try { renderTools((await api("POST", "/api/control/tools/refresh", {})).servers); toast("已检查连接"); }
  catch (e) { toast("检查失败：" + e.message, false); }
});
$("#mem-search").addEventListener("click", () => { memOffset = 0; loadMemories(); });
$("#mem-kind").addEventListener("change", () => { memOffset = 0; loadMemories(); });
$("#mem-prev").addEventListener("click", () => { memOffset = Math.max(0, memOffset - MEM_PAGE); loadMemories(); });
$("#mem-next").addEventListener("click", () => { memOffset += MEM_PAGE; loadMemories(); });

// ---------------------------------------------------------------- settings
const pending = {}; const SOURCE = { dashboard: "后台设置", env: ".env", default: "代码默认", file: "配置文件", unset: "未设置" };
const SETTINGS_PAGE = { pending, changed: () => { $("#set-save").disabled = false; } };
// ctx: where the change is collected (the settings page, or another page with its own save button)
function settingInput(s, ctx = SETTINGS_PAGE) {
  const pending = ctx.pending;
  const mark = (value) => { pending[s.key] = value; row.classList.add("changed"); ctx.changed(); };
  let input;
  if (s.kind === "bool") {
    input = h("select", {}, h("option", { value: "1", text: "开" }), h("option", { value: "0", text: "关" }));
    input.value = (s.value === true || s.value === "1" || s.value === "true") ? "1" : "0";
    input.addEventListener("change", () => mark(input.value === "1"));
  } else if (s.kind === "prompt_version") {
    input = h("select", {}, h("option", { value: "", text: "用 .env 里的提示词" }), ...(s.choices || []).map((c) => h("option", { value: c, text: c })));
    input.value = s.value || ""; input.addEventListener("change", () => mark(input.value));
  } else if (s.kind === "choice") {
    input = h("select", {}, ...(s.choices || []).map((c) => h("option", { value: c.value, text: c.label })));
    input.value = s.value ?? s.default ?? ""; input.addEventListener("change", () => mark(input.value));
  } else if (s.kind === "secret") {
    input = h("input", { type: "password", autocomplete: "new-password", placeholder: s.is_set ? `已设置（${s.fingerprint}），输入新值以替换` : "未设置" });
    input.addEventListener("input", () => input.value ? mark(input.value) : (delete pending[s.key]));
  } else {
    const type = (s.kind === "float" || s.kind === "int") ? "number" : (s.kind === "hhmm" ? "time" : "text");
    input = h("input", { type, value: s.value ?? "", min: s.min ?? null, max: s.max ?? null, step: s.kind === "int" ? "1" : (s.kind === "float" ? "any" : null),
      placeholder: s.optional ? "留空 = 不用" : (s.default !== null && s.default !== undefined ? `默认 ${s.default}` : "") });
    input.addEventListener("input", () => mark(input.value));
  }
  const err = h("div", { class: "err", "data-err": s.key });
  const reset = s.source === "dashboard" ? h("button", { type: "button", class: "ghost", text: "不再覆盖（用回 .env / 默认值）",
    onclick: () => { mark(null); input.disabled = true; reset.disabled = true; } }) : null;
  // what Violet is running right now, when it differs from (or is not shown by) the input above
  let running = null;
  if (s.running_version !== undefined) running = "正在使用：" + (s.running_version ? s.running_version : ".env 里的提示词（不是版本文件中的任何一个）");
  else if (s.saved !== null && s.saved !== undefined && String(s.saved) !== String(s.value)) running = `已保存 ${s.saved}，当前运行值 ${fmt(s.value)}`;
  const row = h("div", { class: "set-row" },
    h("div", {}, h("div", { text: s.label }), h("div", { class: "badges" },
      pill(s.effect === "now" ? "立即生效" : "重启后生效", s.effect === "now" ? "ok" : "warn"), pill("来源：" + (SOURCE[s.source] || s.source)))),
    h("div", {}, input, s.help ? h("div", { class: "help", text: s.help }) : null,
      running ? h("div", { class: "help", "data-running": s.key, text: running }) : null, reset, err));
  return row;
}
const SETTINGS_ELSEWHERE = ["api", "models"];          // the model page
const SETTINGS_ORDER = ["behavior", "messages", "proactive", "life", "memory", "features", "images", "guard", "prompts",
  "mcp", "system", "updates"];
loaders.settings = async () => {
  try {
    const d = await api("GET", "/api/control/settings");
    for (const k of Object.keys(pending)) delete pending[k];
    $("#set-save").disabled = true;
    const box = $("#set-groups"); box.replaceChildren();
    // Violet stage 3: API and models are on the model page; the rest in a useful order, with a jump bar on top
    const order = SETTINGS_ORDER.filter((g) => g in d.groups).concat(Object.keys(d.groups).filter((g) => !SETTINGS_ORDER.includes(g)));
    const shown = [];
    for (const g of order) {
      if (SETTINGS_ELSEWHERE.includes(g)) continue;
      const rows = d.settings.filter((s) => s.group === g);
      if (!rows.length) continue;
      box.append(h("div", { class: "set-group", id: "set-g-" + g }, h("h3", { text: d.groups[g] }), ...rows.map((s) => settingInput(s))));
      shown.push([d.groups[g], "set-g-" + g]);
    }
    jumpNav($("#set-jump"), shown);
    const life = await api("GET", "/api/life/engine").catch(() => null);
    $("#life-mode").textContent = "当前：" + fmt(life && (life.mode || (life.data && life.data.mode)));
    const rs = await api("GET", "/api/control/restart").catch(() => null);
    $("#set-restart").disabled = !(rs && rs.available);
    $("#set-restart-note").textContent = rs && !rs.available ? rs.reason : "";
  } catch (e) { toast("设置读取失败：" + e.message, false); }
};
async function restartViolet() {
  if (!(await confirmBox("重启 Violet？正在处理的消息会在重启后继续，大约需要十几秒。"))) return;
  try { await api("POST", "/api/control/restart", { confirm: true }); }
  catch (e) { toast("重启失败：" + e.message, false); return; }
  toast("正在重启…");
  // wait until the old process is gone, then until the new one answers
  const started = Date.now(); let down = false;
  while (Date.now() - started < 120000) {
    await new Promise((r) => setTimeout(r, 1500));
    let up = false;
    try { up = (await fetch("/healthz", { cache: "no-store" })).ok; } catch (_) { up = false; }
    if (!up) down = true;
    else if (down || Date.now() - started > 15000) { toast("Violet 已重新启动"); location.reload(); return; }
  }
  toast("两分钟内没有等到 Violet 重新启动，请到服务器上查看", false);
}
$("#set-restart").addEventListener("click", restartViolet);
$("#set-save").addEventListener("click", async () => {
  document.querySelectorAll("[data-err]").forEach((e) => (e.textContent = ""));
  try {
    const r = await api("PUT", "/api/control/settings", { changes: pending, reason: $("#set-reason").value });
    const parts = [];
    if (r.applied_now.length) parts.push(`${r.applied_now.length} 项已生效`);
    if (r.needs_restart.length) parts.push(`${r.needs_restart.length} 项需要重启 Violet 后生效（可以点“重启 Violet”）`);
    toast("已保存（第 " + r.revision + " 次修改）：" + parts.join("，"));
    $("#set-reason").value = ""; loaders.settings();
  } catch (e) {
    const problems = (e.data && e.data.problems) || {};
    for (const [k, v] of Object.entries(problems)) { const el = document.querySelector(`[data-err="${CSS.escape(k)}"]`); if (el) el.textContent = v; }
    toast(e.message, false);
  }
});
document.querySelectorAll("[data-life]").forEach((b) => b.addEventListener("click", async () => {
  try { await api("POST", "/api/life/engine/" + b.dataset.life); toast("生活引擎已" + (b.dataset.life === "pause" ? "暂停" : "恢复")); loaders.settings(); }
  catch (e) { toast("操作失败：" + e.message, false); }
}));

// ---------------------------------------------------------------- history
loaders.history = async () => {
  try {
    const rows = await api("GET", "/api/control/settings/history");
    $("#hist-list").replaceChildren(...(rows.length ? rows.map((r) => h("div", { class: "card" },
      h("div", { class: "row" }, h("strong", { text: `第 ${r.revision} 次 · ${new Date(r.at * 1000).toLocaleString()} · ${r.actor}` }),
        h("button", { class: "ghost", onclick: () => rollback(r.revision), text: "回滚到这次之后的状态" })),
      r.reason ? h("div", { class: "note", text: "原因：" + r.reason }) : null,
      ...r.changes.map((c) => h("div", { text: `${c.key}：${fmt(c.old)} → ${fmt(c.new)}` })),
      ...r.changes.filter((c) => c.key === "LLM_API_KEY").map(() => h("button", { class: "ghost", onclick: restoreKey, text: "API Key 恢复上一个值" }))))
      : [h("p", { class: "note", text: "还没有修改记录" })]));
  } catch (e) { toast("修改记录读取失败：" + e.message, false); }
};
async function rollback(revision) {
  if (!(await confirmBox(`把普通设置恢复到第 ${revision} 次修改之后的样子？API Key 不受影响。`))) return;
  try { const r = await api("POST", "/api/control/settings/rollback", { revision, confirm: true }); toast("已回滚（第 " + r.revision + " 次修改）"); loaders.history(); }
  catch (e) { toast("回滚失败：" + e.message, false); }
}
async function restoreKey() {
  if (!(await confirmBox("把 API Key 恢复成上一个值？"))) return;
  try { await api("POST", "/api/control/settings/restore-secret", { key: "LLM_API_KEY", confirm: true }); toast("已恢复上一个 API Key"); loaders.history(); }
  catch (e) { toast("恢复失败：" + e.message, false); }
}

// ---------------------------------------------------------------- account
loaders.account = async () => {
  try {
    const s = await api("GET", "/auth/status");
    $("#acct-status").replaceChildren(
      h("div", { class: "k", text: "管理员密码" }), h("div", { class: "v", text: s.must_change ? "安装时随机生成的密码（可以一直用，想换也可以在下面改）" :(s.password_set ? "已设置" : "未设置（目前用管理令牌登录）") }),
      h("div", { class: "note", text: `登录方式：${s.via}；无操作 ${s.idle_minutes} 分钟后需要重新登录，最长 ${s.session_hours} 小时；Cookie 仅限 HTTPS：${s.cookie_secure ? "是" : "否"}；这次连接是 HTTPS：${s.https ? "是" : "否"}` }),
      h("div", { class: "note", text: "管理令牌：" + ({ default: "使用 VIOLET_ADMIN_TOKEN / admin_token 文件", rotated: "已更换（只保存哈希）", disabled: "已禁用" }[s.token_mode] || s.token_mode) +
        "。禁用或更换令牌请在本机运行 python -m tools.violet_admin token-disable / token-rotate" }));
    $("#acct-username").value = s.username || "";
    $("#acct-entrance").value = s.entrance || "";
    $("#acct-entrance-random").checked = false;
    $("#acct-entrance").disabled = false;
    renderEntranceNote(s.entrance, s.port);
  } catch (e) { toast("读取失败：" + e.message, false); }
};
function renderEntranceNote(entrance, port) {
  const base = `${location.protocol}//${location.hostname}${port ? ":" + port : (location.port ? ":" + location.port : "")}`;
  $("#acct-entrance-note").textContent = (entrance
    ? `后台地址：${base}/${entrance}（没有带安全入口的访问一律显示 404）。`
    : "没有设置安全入口：任何人都能打开登录页，建议设置一个。") +
    " 修改端口需要重启服务，请在服务器上执行 /opt/violet/violet set-port 新端口；忘记地址或密码时执行 /opt/violet/violet info / reset-password。";
}
$("#acct-entrance-random").addEventListener("change", (ev) => { $("#acct-entrance").disabled = ev.target.checked; });
$("#acct-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const body = { current: $("#acct-current").value, username: $("#acct-username").value.trim() };
  if ($("#acct-entrance-random").checked) body.entrance_random = true;
  else body.entrance = $("#acct-entrance").value.trim();
  if (!body.entrance_random && !body.entrance && !(await confirmBox("清空安全入口后，任何人都能打开登录页。确定？"))) return;
  try {
    const r = await api("POST", "/auth/account", body);
    $("#acct-current").value = "";
    if (!r.changed.length) { toast("没有变化"); return; }
    toast(r.changed.includes("entrance") ? `已保存。新的后台地址：…/${r.entrance}，请记下来` : "已保存，其他登录已失效");
    loaders.account();
  } catch (e) { toast(e.message, false); }
});
$("#pw-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await api("POST", "/auth/password", { current: $("#pw-current").value, new: $("#pw-new").value, confirm: $("#pw-confirm").value });
    ev.target.reset(); toast("密码已修改，其他登录已失效"); loaders.account();
    if (location.hash === "#account" || !location.hash) setTimeout(() => { location.hash = ""; location.reload(); }, 800);
  } catch (e) { toast(e.message, false); }
});
$("#logout-all").addEventListener("click", async () => {
  if (!(await confirmBox("退出所有设备上的登录（包括当前这个）？"))) return;
  try { await api("POST", "/auth/logout-all"); location.href = "/auth/login"; } catch (e) { toast(e.message, false); }
});

// ---------------------------------------------------------------- start (after every page script registered its loaders)
document.addEventListener("DOMContentLoaded", async () => {
  let first = location.hash ? location.hash.slice(1) : "";
  if (!first) {
    // the setup page while something required is missing, else the overview
    try {
      first = (await api("GET", "/api/control/setup")).complete ? "overview" : "setup";
    } catch (_) { first = "overview"; }
  }
  const btn = document.querySelector(`#tabs button[data-tab="${CSS.escape(first)}"]`) || document.querySelector("#tabs button");
  btn.click();
});
