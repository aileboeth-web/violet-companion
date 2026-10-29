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
// opts.reason: show the "原因（可选）" box (only where the reason is recorded, e.g. deleting a memory) -- 0.7.3
function confirmBox(text, opts = {}) {
  return new Promise((resolve) => {
    const d = $("#confirm"); $("#confirm-text").textContent = text; $("#confirm-reason").value = "";
    $("#confirm-reason").hidden = !opts.reason;
    $("#confirm-ok").textContent = opts.ok || "确定";
    $("#confirm-ok").className = opts.danger === false ? "" : "danger";
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
// 0.7.0: a dialog with a progress bar for work the server's root helper does (installing the relay, updating Violet).
// It follows GET `url` ({state: queued | running | done | failed | stale | none, percent, step, message}). With
// `restarts` (an update) Violet itself goes away for a while: the dialog says so, waits for /healthz and goes on.
// The dialog can be closed at any time; the work goes on on the server.
function progressDialog({ title, url, restarts = false, onFinish = null }) {
  const fill = h("div", { class: "pbar-fill" });
  const bar = h("div", { class: "pbar queued", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100" }, fill);
  const pct = h("span", { class: "pbar-pct", text: "" });
  const step = h("p", { class: "pbar-step", text: "已提交，等待服务器开始…" });
  const msg = h("p", { class: "note" });
  const close = h("button", { type: "button", class: "ghost", text: "关闭（任务在服务器上继续）" });
  const extra = h("div", { class: "row" });
  const d = h("dialog", { class: "progress-dialog" }, h("div", { class: "stack" },
    h("h3", { text: title }), bar, h("div", { class: "pbar-line" }, step, pct), msg, extra, h("div", { class: "row" }, close)));
  document.body.append(d);
  let stopped = false, finished = false, lostSince = 0;
  const stop = () => { stopped = true; if (d.open) d.close(); d.remove(); };
  close.addEventListener("click", stop);
  d.addEventListener("cancel", (ev) => { ev.preventDefault(); stop(); });
  d.showModal();

  function show(p) {
    const percent = Math.max(0, Math.min(100, Number(p.percent) || 0));
    const wasQueued = bar.classList.contains("queued");
    bar.className = "pbar " + ({ queued: "queued", running: "", done: "done", failed: "failed", stale: "failed" }[p.state] ?? "queued");
    // from the full-width "waiting" stripes to the first real value: jump, do not animate backwards
    if (wasQueued && p.state !== "queued") { fill.style.transition = "none"; void fill.offsetWidth; }
    fill.style.width = (p.state === "queued" ? 100 : percent) + "%";
    if (wasQueued && p.state !== "queued") { void fill.offsetWidth; fill.style.transition = ""; }
    bar.setAttribute("aria-valuenow", String(percent));
    pct.textContent = p.state === "queued" ? "" : percent + "%";
    step.textContent = p.step || "";
    msg.textContent = p.state === "failed" || p.state === "stale" ? (p.message || "失败") : "";
    if (p.state === "queued" && p.at && Date.now() / 1000 - p.at > 60) {
      msg.textContent = "服务器一分钟内还没有开始：在服务器上查看 systemctl status violet-task.path violet-update.path";
    }
  }
  async function waitHealthy() {
    for (;;) {
      if (stopped) return;
      try { const r = await fetch("/healthz", { cache: "no-store" }); if (r.ok) return; } catch (_) { /* still down */ }
      await new Promise((ok) => setTimeout(ok, 2000));
    }
  }
  async function tick() {
    if (stopped || finished) return;
    let p;
    try { p = await api("GET", url); lostSince = 0; }
    catch (e) {
      if (restarts) {
        lostSince = lostSince || Date.now();
        step.textContent = "Violet 正在重启（升级期间后台暂时打不开），等它回来…";
        await waitHealthy();
      } else { step.textContent = "读取进度失败：" + e.message; await new Promise((ok) => setTimeout(ok, 2000)); }
      return tick();
    }
    show(p);
    if (p.state === "done" || p.state === "failed" || p.state === "stale") {
      finished = true;
      close.textContent = "关闭";
      if (p.state === "done") toast(p.step || "完成");
      if (onFinish) onFinish(p, extra);
      return;
    }
    setTimeout(tick, 1000);
  }
  tick();
  return { close: stop };
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
// 0.7.5: the scheduler's reasons are internal English codes
const DUE = { "not due yet": "还没到时间", "next proactive time initialized": "已排好下次时间", "quiet hours": "免打扰时段",
  "no user message yet": "还没聊过天", "too soon after user message": "刚聊过，晚点再说", "due": "到时间了" };
function proactiveText(p) {
  if (p.enabled === false) return "已关闭";
  const reason = DUE[p.due_reason] || p.due_reason || "—";
  const next = p.next_proactive_at ? String(p.next_proactive_at).replace("T", " ").slice(5, 16) : "";
  return p.due_reason === "not due yet" && next ? `${reason}（下次 ${next}）` : reason;
}
// 0.7.5: every channel, not only WeChat ("已连接：QQ"; WeChat not logged in while another one works is not a warning)
function channelsStat(d) {
  const rows = (d.channels || []).filter((c) => c.state !== "disabled");
  const live = rows.filter((c) => c.state === "connected");
  if (live.length) return stat("聊天渠道", "已连接：" + live.map((c) => c.label).join("、"), "ok");
  if (!rows.length && d.wechat) return stat("聊天渠道", d.wechat.logged_in ? "微信已登录" : "还没有可用的渠道", d.wechat.logged_in ? "ok" : "warn");
  return stat("聊天渠道", rows.length ? "都没有连上" : "还没有可用的渠道", "warn");
}
loaders.overview = async () => {
  try {
    const d = await api("GET", "/api/control/overview");
    const s = d.service || {}, p = d.proactive || {}, life = d.life || {};
    const alive = (x) => (x ? "ok" : "bad");
    const totals = d.message_totals ? `收 ${d.message_totals.received} · 发 ${d.message_totals.sent}` : messageCount(d.messages);
    const cards = $("#ov-cards"); cards.replaceChildren(
      channelsStat(d),
      // 0.7.6: this thread is WeChat's -- not red when WeChat is switched off and another channel is used
      (d.channels || []).some((c) => c.type === "wechat_ilink" && c.state === "disabled")
        ? stat("微信收消息线程", "微信未使用")
        : stat("微信收消息线程", s.poll_thread_alive ? "运行中" : "未运行", alive(s.poll_thread_alive)),
      stat("回复线程", s.worker_thread_alive ? "运行中" : "未运行", alive(s.worker_thread_alive)),
      stat("记忆线程", s.memory_worker_alive ? "运行中" : "未运行", alive(s.memory_worker_alive)),
      stat("排队中的对话", s.queued_turns ?? 0),
      stat("主动联系", proactiveText(p)),
      stat("今天主动", p.daily_sent_count === undefined ? "—" : `${p.daily_sent_count} / ${fmt(p.daily_message_limit)}`),
      stat("生活引擎", lifeText(life), life.mode === "on" ? "ok" : (life.mode ? "warn" : "")),
      stat("消息总数", totals),
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
    watchOverview(d.channels || []);
  } catch (e) { toast("概览读取失败：" + e.message, false); }
};
// 0.7.6: while a channel is still connecting (just after a start or an update), look again every few seconds
let ovPending = null, ovTries = 0;
function watchOverview(channels) {
  clearTimeout(ovPending);
  if (!channels.some((c) => ["starting", "reconnecting"].includes(c.state))) { ovTries = 0; return; }
  if (ovTries++ > 40) return;
  ovPending = setTimeout(() => {
    if ($("#tab-overview").classList.contains("active") && !document.hidden) loaders.overview();
  }, 3000);
}
$("#ov-refresh").addEventListener("click", () => loaders.overview());

// ---------------------------------------------------------------- chat
const KIND = { user: "对方", violet: "Violet 回复", proactive: "Violet 主动", reminder: "提醒", system: "系统" };
// 2026-10-29: channel -> conversations (left) -> messages (right). "你" is the owner's chat on that channel; groups
// and other people are Violet's own conversations (each message shows who said it).
const hist = { channel: null, conv: "owner", before: null, channels: [], items: [] };
const KIND_ICON = { owner: "★", group: "群", private: "人" };
function convName(item) { return item ? item.title : ""; }
function renderChannels() {
  $("#hist-chans").replaceChildren(...(hist.channels.length ? hist.channels.map((c) => h("button", {
    type: "button", role: "tab", class: "chip" + (c.id === hist.channel ? " active" : ""), "aria-selected": String(c.id === hist.channel),
    text: c.label + (c.conversations ? `（${c.conversations + (c.owner_messages ? 1 : 0)}）` : ""),
    onclick: () => { hist.channel = c.id; hist.conv = "owner"; loadHistoryList(); } }))
    : [h("p", { class: "note", text: "还没有聊天记录" })]));
}
async function loadHistoryList() {
  renderChannels();
  if (!hist.channel) { $("#hist-list").replaceChildren(); $("#chat-list").replaceChildren(); return; }
  try {
    const d = await api("GET", `/api/control/history/${encodeURIComponent(hist.channel)}`);
    hist.items = d.items;
    if (!hist.items.some((i) => i.key === hist.conv)) hist.conv = hist.items.length ? hist.items[0].key : "owner";
    renderList();
    await loadDays();
    loadChat(false);
  } catch (e) { toast("聊天记录读取失败：" + e.message, false); }
}
function renderList() {
  $("#hist-list").replaceChildren(...(hist.items.length ? hist.items.map((i) => h("button", {
    type: "button", class: "hist-item" + (i.key === hist.conv ? " active" : ""),
    onclick: () => { hist.conv = i.key; renderList(); loadDays().then(() => loadChat(false)); } },
    h("span", { class: "hist-avatar " + i.kind, text: KIND_ICON[i.kind] || "·" }),
    h("span", { class: "hist-text" },
      h("span", { class: "hist-title" }, h("strong", { text: i.title }), h("span", { class: "note", text: i.last_at ? when(i.last_at) : "" })),
      h("span", { class: "note hist-last", text: i.last_text || i.subtitle || "" }))))
    : [h("p", { class: "note", text: "这个渠道还没有聊天" })]));
  const cur = hist.items.find((i) => i.key === hist.conv);
  $("#hist-head").replaceChildren(...(cur ? [h("strong", { text: convName(cur) }), h("span", { class: "note", text: " " + (cur.subtitle || "") })] : []));
  const owner = hist.conv === "owner";
  $("#chat-role-wrap").hidden = !owner; $("#chat-legend").hidden = !owner;
}
async function loadDays() {
  const sel = $("#chat-day");
  if (hist.conv !== "owner") { sel.replaceChildren(h("option", { value: "", text: "全部" })); return; }
  try {
    const days = await api("GET", "/api/control/messages/days?" + new URLSearchParams({ channel: hist.channel }));
    const keep = sel.value;
    sel.replaceChildren(h("option", { value: "", text: "全部" }), ...days.map((d) => h("option", { value: d.day, text: `${d.day}（${d.n}）` })));
    sel.value = days.some((d) => d.day === keep) ? keep : "";
  } catch (_) { /* the list reports */ }
}
async function loadChat(append) {
  if (!hist.channel) return;
  const params = new URLSearchParams({ limit: "100" });
  if ($("#chat-day").value) params.set("date", $("#chat-day").value);
  if ($("#chat-q").value.trim()) params.set("q", $("#chat-q").value.trim());
  if (append && hist.before) params.set("before_id", hist.before);
  const owner = hist.conv === "owner";
  if (owner) { params.set("channel", hist.channel); if ($("#chat-role").value) params.set("role", $("#chat-role").value); }
  try {
    const d = owner ? await api("GET", "/api/control/messages?" + params)
      : await api("GET", `/api/control/history/${encodeURIComponent(hist.channel)}/${hist.conv}?` + params);
    const list = $("#chat-list"); const nodes = []; let day = null;
    for (const m of d.messages) {
      const dd = (m.created_at || "").slice(0, 10);
      if (dd !== day) { nodes.push(h("div", { class: "day", text: dd })); day = dd; }
      const mine = owner ? m.role !== "user" : !!m.is_self;
      const tag = owner ? h("span", { class: "tag " + m.kind, text: KIND[m.kind] || m.kind })
        : h("span", { class: "tag " + (mine ? "violet" : "user"), text: mine ? "Violet" : (m.sender_name || m.sender_id || "对方") });
      nodes.push(h("div", { class: "msg " + (mine ? "mine" : "user") },
        h("div", { class: "meta" }, tag, h("span", { text: when(m.created_at) }),
          !owner && !mine && m.sender_id ? h("span", { class: "note", text: m.sender_id }) : null,
          m.message_type && m.message_type !== "text" ? h("span", { text: "[" + m.message_type + "]" }) : null,
          m.mentions_self ? h("span", { class: "note", text: "@她" }) : null),
        h("div", { text: m.content || "" })));
    }
    if (append) list.prepend(...nodes); else list.replaceChildren(...(nodes.length ? nodes : [h("p", { class: "note", text: "没有符合条件的消息" })]));
    hist.before = d.next_before_id; $("#chat-more").hidden = !hist.before;
    if (!append) list.scrollTop = list.scrollHeight;
  } catch (e) { toast("聊天记录读取失败：" + e.message, false); }
}
loaders.chat = async () => {
  try {
    const d = await api("GET", "/api/control/history");
    hist.channels = d.channels.filter((c) => c.owner_messages || c.conversations);
    if (!hist.channels.some((c) => c.id === hist.channel)) {
      const latest = [...hist.channels].sort((a, b) => String(b.last_at || "").localeCompare(String(a.last_at || "")))[0];
      hist.channel = latest ? latest.id : null;
    }
  } catch (e) { toast("聊天记录读取失败：" + e.message, false); }
  loadHistoryList();
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
  const ok = await confirmBox(text, { reason: true }); if (!ok) return;
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
  const oneClick = h("div", { class: "stack-wide", id: "searxng-box" });
  loadSearxng(oneClick);
  box.replaceChildren(h("div", { class: "card" }, h("h3", { text: "联网工具（violet-web）" }), oneClick,
    h("details", {}, h("summary", { text: "详细状态和限额" }),
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
    ...(failures.length ? [h("div", { class: "note", text: "今天的失败：" }), ...failures] : []))));
}
// 0.7.1: one click installs a private SearXNG on this server (root task service, progress dialog) and switches web
// search on with it; uninstalling removes it and switches web search off
async function searxngAction(action) {
  return api("POST", "/api/control/tools/web/searxng", { action, confirm: true });
}
function watchSearxng(title, after) {
  progressDialog({ title, url: "/api/control/progress/task", onFinish: async (p, extra) => {
    if (p.state === "done" && after) {
      try { await searxngAction(after); toast(after === "enable" ? "联网搜索已开启" : "联网搜索已关闭"); }
      catch (e) { toast(e.message, false); }
    }
    if (p.state === "done" && p.notes && p.notes.length) extra.replaceChildren(h("ul", { class: "note" }, ...p.notes.map((n) => h("li", { text: n }))));
    loaders.tools();
  } });
}
// 0.7.4: two ways to search -- Bing directly (recommended: nothing to install; the user's server in mainland China
// got Bing's results while every SearXNG engine timed out) and the optional self-hosted SearXNG
async function loadSearxng(box) {
  let s;
  try { s = await api("GET", "/api/control/tools/web/searxng"); } catch (e) { box.replaceChildren(h("p", { class: "err", text: e.message })); return; }
  const p = s.progress || {};
  const running = (p.state === "queued" || p.state === "running") && String(p.task || "").startsWith("searxng");
  const using = s.web_enabled ? s.provider : null;
  const NAMES = { bing: "必应直连", searxng: "自建 SearXNG", bocha: "博查", brave: "Brave Search", mock: "测试数据" };
  const head = h("div", { class: "row" }, h("strong", { text: "联网搜索" }),
    using ? pill(`已开启 · ${NAMES[using] || using}`, "ok") : pill("未开启", ""));
  const intro = h("p", { class: "note", text: "让 Violet 能查实时信息（天气、新闻、资料）。选一种搜索方式就行，下面的“测试联网”可以马上看到能不能用。" });

  // option 1: Bing
  const bingOn = using === "bing";
  const bing = h("div", { class: "card web-option" + (bingOn ? " current" : "") },
    h("div", { class: "row" }, h("strong", { text: "必应直连（推荐）" }), bingOn ? pill("正在使用", "ok") : null),
    h("p", { class: "note", text: "直接用必应的搜索结果，不用安装任何东西、不用 Key，国内服务器也能用。" }),
    h("div", { class: "row" }, bingOn ? null : h("button", { type: "button", text: "使用必应直连", onclick: async () => {
      try { await searxngAction("bing"); toast("联网搜索已开启：必应直连"); loaders.tools(); } catch (e) { toast(e.message, false); }
    } })));

  // option 2: SearXNG
  const sxOn = using === "searxng" && s.in_use;
  const buttons = h("div", { class: "row" });
  if (running) {
    buttons.append(h("button", { type: "button", text: "正在执行…查看进度", onclick: () => watchSearxng("联网搜索", p.task === "searxng-install" ? "enable" : p.task === "searxng-uninstall" ? "disable" : null) }));
  } else if (!s.available) {
    buttons.append(h("p", { class: "note", text: "这种安装方式不能在网页上安装。以 root 在服务器上运行：/opt/violet/violet searxng install，再在“设置 → 功能开关”里打开联网搜索。" }));
  } else if (!s.installed) {
    buttons.append(h("button", { class: "ghost", type: "button", text: "一键安装 SearXNG", onclick: async () => {
      // switched to SearXNG afterwards only when nothing else is searching yet
      try { await searxngAction("install"); watchSearxng("安装 SearXNG", using ? null : "enable"); } catch (e) { toast(e.message, false); }
    } }));
  } else {
    if (!sxOn) {
      buttons.append(h("button", { class: "ghost", type: "button", text: "改用 SearXNG", onclick: async () => {
        try { await searxngAction("enable"); toast("联网搜索已开启：自建 SearXNG"); loaders.tools(); } catch (e) { toast(e.message, false); }
      } }));
    }
    buttons.append(h("button", { class: "ghost", type: "button", text: "选择搜索引擎", onclick: () => engineDialog(s) }));
    buttons.append(h("button", { class: "danger", type: "button", text: "卸载 SearXNG", onclick: async () => {
      if (!(await confirmBox("卸载这台服务器上的 SearXNG？服务、程序和缓存都会删除。" + (sxOn ? "它正在用来搜索，卸载后联网搜索会关闭（可以改用必应直连）。" : "")))) return;
      try { await searxngAction("uninstall"); watchSearxng("卸载 SearXNG", "disable"); } catch (e) { toast(e.message, false); }
    } }));
  }
  const last = p.state === "failed" && String(p.task || "").startsWith("searxng") ? h("p", { class: "err", text: "上次操作失败：" + (p.message || "") }) : null;
  const engines = s.engines_text ? h("p", { class: "note", text: `搜索引擎：${s.engines_text}` +
    (s.last_note ? `。上次测试：${s.last_note}` : "") }) : null;
  const searxng = h("details", { class: "card web-option" + (sxOn ? " current" : ""), open: sxOn || s.installed || running ? true : null },
    h("summary", {}, h("strong", { text: "自建 SearXNG（可选）" }), sxOn ? " · 正在使用" : s.installed ? " · 已安装" : ""),
    ...[h("p", { class: "note", text: "在这台服务器上装一个私有的开源搜索（只在本机 127.0.0.1 监听、不需要 Docker），它同时问好几个搜索引擎。" +
      "国内服务器上它的搜索引擎经常连不上，那样请用上面的必应直连。卸载会把它和它的文件全部删掉。" }),
      s.installed && s.url ? h("p", { class: "note", text: `地址 ${s.url}（只有本机能访问）` }) : null, engines, buttons, last].filter(Boolean));

  // the test: whatever is in use right now
  const testOut = h("div", { class: "stack web-test" });
  const test = h("div", { class: "row" }, h("button", { type: "button", text: "测试联网", disabled: using ? null : true, onclick: async (ev) => {
    ev.target.disabled = true;
    testOut.replaceChildren(h("p", { class: "note", text: "正在测试：搜索一次“今天天气”，再打开一个网页…" }));
    try { showWebTest(testOut, await api("POST", "/api/control/tools/web/test", {}), s.installed && using === "searxng"); }
    catch (e) { testOut.replaceChildren(h("p", { class: "err", text: e.message })); }
    ev.target.disabled = false;
  } }), using ? null : h("span", { class: "note", text: "先选一种搜索方式" }));
  box.replaceChildren(head, intro, bing, searxng, test, testOut);
}
// 0.7.3 (user: "做成可以自定义的，点开来可以自己选择搜索引擎"): tick the engines (only names from the server's list),
// IPv4 only, then "保存并测试" (exactly that choice) or "自动选择" (try the usual choices until one finds results)
function engineDialog(s) {
  const chosen = new Set(Array.isArray(s.engines) ? s.engines : []);
  const boxes = {};
  const group = (title, region) => h("fieldset", { class: "engine-group" }, h("legend", { text: title }),
    ...s.catalog.filter((e) => e.region === region).map((e) => {
      const box = h("input", { type: "checkbox" }); box.checked = chosen.has(e.name); boxes[e.name] = box;
      return h("label", { class: "check" }, box, ` ${e.label}`);
    }));
  const useDefault = h("input", { type: "checkbox" }); useDefault.checked = s.engines === "default";
  const ipv4 = h("input", { type: "checkbox" }); ipv4.checked = !!s.ipv4;
  const err = h("p", { class: "err" });
  const d = h("dialog", { class: "engine-dialog" }, h("div", { class: "stack" },
    h("h3", { text: "选择搜索引擎" }),
    h("p", { class: "note", text: "勾选 SearXNG 要用的搜索引擎。服务器在国内时，国外的通常连不上，只勾国内的即可。保存后会重启 SearXNG 并测试一次搜索，下面的“测试联网”可以看到每个引擎为什么失败。" }),
    group("国内能访问", "cn"), group("国外（国内服务器通常连不上）", "global"),
    h("label", { class: "check" }, useDefault, " 用 SearXNG 默认的那一组（忽略上面的勾选）"),
    h("label", { class: "check" }, ipv4, " 只用 IPv4（服务器的 IPv6 配了但不通时，所有搜索引擎都会超时，勾上这个）"),
    err,
    h("div", { class: "row" },
      h("button", { class: "ghost", type: "button", text: "取消", onclick: () => { d.close(); d.remove(); } }),
      h("button", { class: "ghost", type: "button", text: "自动选择", onclick: async () => {
        try { await searxngAction("tune"); d.close(); d.remove(); watchSearxng("自动选择搜索引擎", null); }
        catch (e) { err.textContent = e.message; }
      } }),
      h("button", { type: "button", text: "保存并测试", onclick: async () => {
        const names = Object.keys(boxes).filter((n) => boxes[n].checked);
        if (!useDefault.checked && !names.length) { err.textContent = "至少选一个搜索引擎"; return; }
        try {
          await api("POST", "/api/control/tools/web/searxng",
            { action: "configure", confirm: true, engines: useDefault.checked ? "default" : names, ipv4: ipv4.checked });
          d.close(); d.remove(); watchSearxng("更换搜索引擎", null);
        } catch (e) { err.textContent = e.message; }
      } }))));
  document.body.append(d);
  d.addEventListener("cancel", () => d.remove());
  d.showModal();
}
function showWebTest(box, t, installed) {
  const s = t.search || {}, f = t.fetch || {};
  const rows = [
    h("div", { class: "row" }, pill(s.ok ? "搜索正常" : "搜索失败", s.ok ? "ok" : "bad"),
      h("span", { class: "note", text: s.ok ? `搜到 ${s.count} 条（${s.latency_ms} 毫秒）` : `${s.error || "没有结果"}（${s.latency_ms} 毫秒）` })),
    s.titles && s.titles.length ? h("ul", { class: "note" }, ...s.titles.map((x) => h("li", { text: x }))) : null,
    t.by_engine && Object.keys(t.by_engine).length ? h("p", { class: "note", text: "有结果的搜索引擎：" +
      Object.entries(t.by_engine).map(([k, v]) => `${k} ${v} 条`).join("、") }) : null,
    t.dead_engines && t.dead_engines.length ? h("p", { class: "note", text: "没有响应的搜索引擎：" + t.dead_engines.join("、") }) : null,
    !s.ok && installed ? h("p", { class: "note", text: "搜不到东西时点“选择搜索引擎”：只勾国内能访问的；如果每个引擎都是“超时”，再勾上“只用 IPv4”。还不行就用上面的“必应直连”。" }) : null,
    !s.ok && t.provider === "bing" ? h("p", { class: "note", text: "必应没有给结果：可以在“设置 → 功能开关 → 必应地址”换成另一个地址再试；还不行，说明必应拒绝了这台服务器，可以改用博查等付费搜索（同一页的“搜索服务”）。" }) : null,
    h("div", { class: "row" }, pill(f.ok ? "网页阅读正常" : "网页阅读失败", f.ok ? "ok" : "bad"),
      h("span", { class: "note", text: f.ok ? `打开了 ${f.url}（${f.latency_ms} 毫秒）` : `打不开 ${f.url}：${f.error}` })),
  ];
  box.replaceChildren(...rows.filter(Boolean));
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
const SETTINGS_ELSEWHERE = ["api", "models", "images"];          // the model page; 0.7.2: the images page
const SETTINGS_ORDER = ["behavior", "messages", "proactive", "life", "memory", "features", "images", "guard", "prompts",
  "mcp", "system", "logs", "updates"];
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
