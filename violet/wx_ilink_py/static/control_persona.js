// Violet Control Center: persona and virtual life (Violet stage 3; was the separate /persona page). Uses control.js's
// helpers ($, h, api, toast, pill, fmt, when, confirmBox, loaders). Built with DOM nodes only, never parsed as HTML.
"use strict";
(() => {
  const STATUS = {
    active: ["进行中", "ok"], paused: ["已暂停", "warn"], completed: ["已完成", "ok"], abandoned: ["已放弃", ""],
    cancelled: ["已取消", ""], planned: ["计划中", "warn"], happened: ["已发生", "ok"], retracted: ["已撤回", "bad"],
    proposed: ["待批准", "warn"], approved: ["已批准", "ok"], rejected: ["已拒绝", "bad"], open: ["待处理", "warn"],
  };
  const status = (s) => { const [t, k] = STATUS[s] || [s, ""]; return pill(t, k); };
  const ENGINE = { on: ["运行中", "ok"], paused: ["已暂停", "warn"], off: ["已关闭", ""] };
  const day = (s) => (s ? String(s).replace("T", " ").slice(0, 16) : "—");
  const empty = (text) => h("p", { class: "note", text });

  async function act(url, body, done) {
    try { await api("POST", url, body || {}); toast(done || "已完成"); loaders.persona(); }
    catch (e) { toast("操作失败：" + e.message, false); }
  }

  // ---------------------------------------------------------------- history dialog
  let dialog = null;
  function historyDialog() {
    if (dialog) return dialog;
    dialog = h("dialog", { class: "pl-history" }, h("form", { method: "dialog", class: "stack" },
      h("h3", { id: "pl-history-title" }), h("div", { id: "pl-history-list", class: "history" }),
      h("div", { class: "row" }, h("button", { value: "close", class: "ghost", text: "关闭" }))));
    document.body.append(dialog);
    return dialog;
  }
  async function showHistory(title, url) {
    try {
      const r = await api("GET", url);
      const d = historyDialog();
      $("#pl-history-title").textContent = title + " · 修改历史";
      const rows = r.revisions || [];
      $("#pl-history-list").replaceChildren(...(rows.length ? rows.map((v) => {
        const rest = Object.fromEntries(Object.entries(v).filter(([k]) => !["id", "entity", "entity_id", "created_at", "actor", "reason"].includes(k)));
        return h("div", {}, h("div", { text: `${day(v.created_at)} · ${fmt(v.actor)} · ${fmt(v.reason)}` }),
          h("pre", { class: "note", text: JSON.stringify(rest, null, 1) }));
      }) : [empty("没有修改记录")]));
      d.showModal();
    } catch (e) { toast("读取失败：" + e.message, false); }
  }

  // ---------------------------------------------------------------- render
  function listBlock(title, items) {
    if (!items || !items.length) return null;
    return h("div", { class: "pl-list" }, h("div", { class: "k", text: title }),
      h("ul", {}, ...items.map((i) => h("li", { text: typeof i === "string" ? i : (i.label || JSON.stringify(i)) }))));
  }

  function renderCore(p) {
    const d = (p.core && p.core.data) || {};
    $("#pl-core").replaceChildren(
      h("div", { class: "row" }, h("strong", { text: (d.identity && d.identity.name) || "Violet" }),
        pill(`v${fmt(p.core && p.core.version)}`), h("span", { class: "note", text: ((p.core && p.core.sha256) || "").slice(0, 12) })),
      h("p", { text: (d.identity && d.identity.virtual_declaration) || "" }),
      h("div", { class: "pl-grid" }, listBlock("性格", d.personality), listBlock("说话习惯", d.communication_habits),
        listBlock("长期兴趣", d.long_term_interests), listBlock("价值观", d.values), listBlock("缺点", d.flaws),
        listBlock("生活边界", d.living_constraints)),
      h("p", { class: "note", text: (p.core && p.core.edit_policy) || "" }));
  }

  function renderDynamics(rows) {
    $("#pl-dynamics").replaceChildren(...(rows.length ? rows.map((x) => h("div", { class: "card" },
      h("div", { class: "row" }, status(x.status), h("strong", { text: x.aspect_key }), h("span", { class: "note", text: x.category }),
        h("button", { class: "ghost", text: "历史", onclick: () => showHistory(x.aspect_key, `/api/persona/dynamics/${x.id}/history`) })),
      h("p", { text: `现在：${fmt(x.value)}` }),
      x.previous_value ? h("p", { class: "note", text: `之前：${x.previous_value}` }) : null,
      h("p", { class: "note", text: `原因：${fmt(x.reason)}` }),
      x.status === "proposed" ? h("div", { class: "row" },
        h("button", { text: "批准", onclick: () => act(`/api/persona/dynamics/${x.id}/decide`, { approve: true }, "已批准") }),
        h("button", { class: "ghost", text: "拒绝", onclick: () => act(`/api/persona/dynamics/${x.id}/decide`, { approve: false }, "已拒绝") })) : null))
      : [empty("还没有变化")]));
  }

  function renderThreads(rows) {
    $("#pl-threads").replaceChildren(...(rows.length ? rows.map((t) => h("div", { class: "card" },
      h("div", { class: "row" }, status(t.status), h("strong", { text: t.title }), h("span", { class: "note", text: fmt(t.interest_key) }),
        h("button", { class: "ghost", text: "历史", onclick: () => showHistory(t.title, `/api/life/thread/${t.id}/history`) })),
      h("p", { class: "note", text: `开始于 ${day(t.started_at)} · 进度 ${fmt(t.progress_steps)}` }),
      h("div", { class: "row" },
        t.status === "active" ? h("button", { class: "ghost", text: "暂停", onclick: () => act(`/api/life/threads/${t.id}/pause`, {}, "已暂停") }) : null,
        t.status === "paused" ? h("button", { class: "ghost", text: "恢复", onclick: () => act(`/api/life/threads/${t.id}/resume`, {}, "已恢复") }) : null,
        ["active", "paused"].includes(t.status) ? h("button", { class: "ghost", text: "放弃", onclick: async () => {
          if (await confirmBox(`放弃生活线“${t.title}”？之后不会再推进。`)) act(`/api/life/threads/${t.id}/abandon`, {}, "已放弃");
        } }) : null)))
      : [empty("还没有生活线")]));
  }

  function renderPlans(rows) {
    $("#pl-plans").replaceChildren(...(rows.length ? rows.map((x) => h("div", { class: "card" },
      h("div", { class: "row" }, status(x.status), h("span", { text: x.content }),
        h("button", { class: "ghost", text: "历史", onclick: () => showHistory("计划", `/api/life/plan/${x.id}/history`) })),
      h("p", { class: "note", text: `计划时间 ${day(x.planned_for)}` }),
      ["planned", "active", "paused"].includes(x.status) ? h("div", { class: "row" },
        h("button", { class: "ghost", text: "取消计划", onclick: () => act(`/api/life/plans/${x.id}/cancel`, {}, "已取消") })) : null))
      : [empty("没有计划")]));
  }

  function renderEvents(rows) {
    $("#pl-events").replaceChildren(...(rows.length ? rows.slice().reverse().slice(0, 60).map((e) => h("div", { class: "card" },
      h("div", { class: "row" }, status(e.status), h("span", { class: "note", text: `${day(e.occurred_at)} · ${fmt(e.event_type)} · ${fmt(e.source)}` }),
        e.share_count ? pill("已经告诉过对方") : null,
        h("button", { class: "ghost", text: "历史", onclick: () => showHistory("生活事件", `/api/life/event/${e.id}/history`) })),
      h("p", { text: e.content }),
      e.status === "happened" ? h("div", { class: "row" },
        h("button", { class: "ghost", text: "更正", onclick: async () => {
          const content = window.prompt("更正后的内容（只改 Violet 的虚拟经历）", e.content); if (!content) return;
          const reason = window.prompt("更正原因"); if (!reason) return;
          try { await api("PATCH", `/api/life/events/${e.id}`, { content, reason }); toast("已更正"); loaders.persona(); }
          catch (err) { toast("更正失败：" + err.message, false); }
        } }),
        h("button", { class: "ghost", text: "撤回", onclick: async () => {
          const reason = window.prompt("撤回原因（这件事就当没发生过）"); if (reason) act(`/api/life/events/${e.id}/retract`, { reason }, "已撤回");
        } })) : null))
      : [empty("还没有生活事件")]));
  }

  function renderFlags(rows) {
    $("#pl-flags").replaceChildren(...(rows.length ? rows.map((f) => h("div", { class: "card" },
      h("div", { class: "row" }, pill(f.check_name, "warn"), h("span", { class: "note", text: `${day(f.created_at)} · ${fmt(f.entity)}` }),
        h("button", { class: "ghost", text: "已处理", onclick: () => act(`/api/life/flags/${f.id}/resolve`, {}, "已标记为处理") })),
      h("p", { text: fmt(f.detail) })))
      : [empty("没有需要处理的地方")]));
  }

  loaders.persona = async () => {
    try {
      const [p, l] = await Promise.all([api("GET", "/api/persona"), api("GET", "/api/life")]);
      renderCore(p);
      renderDynamics(p.dynamics || []);
      const [et, ek] = ENGINE[(l.engine || {}).mode] || [fmt((l.engine || {}).mode), ""];
      const engine = $("#pl-engine"); engine.textContent = et; engine.className = "pill " + ek;
      renderThreads(l.threads || []);
      renderPlans(l.plans || []);
      renderEvents(l.events || []);
      renderFlags(l.flags || []);
    } catch (e) { toast("人设与生活读取失败：" + e.message, false); }
  };
  $("#pl-refresh").addEventListener("click", () => loaders.persona());
  $("#pl-pause").addEventListener("click", () => act("/api/life/engine/pause", {}, "生活引擎已暂停"));
  $("#pl-resume").addEventListener("click", () => act("/api/life/engine/resume", {}, "生活引擎已恢复"));
})();
