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
      h("div", { class: "row" }, h("strong", { text: (d.identity && d.identity.name) || (d.blank ? "空白人设（还没有名字）" : "Violet") }),
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

  // 2026-10-29: persona templates -- switching asks whether to keep the memory or start empty (archived first)
  function switchDialog(t) {
    return new Promise((resolve) => {
      const keep = h("input", { type: "radio", name: "pt-mem", value: "keep", checked: true });
      const clear = h("input", { type: "radio", name: "pt-mem", value: "clear" });
      const name = h("input", { maxlength: 20, placeholder: "可以不填，以后聊天时再起" });
      const d = h("dialog", {}, h("form", { method: "dialog", class: "stack" },
        h("h3", { text: `换成“${t.title}”` }),
        h("p", { class: "note", text: t.summary }),
        t.blank ? h("label", {}, "给她起个名字（可选） ", name) : null,
        h("label", { class: "check" }, keep, " 保留记忆：她记得你、记得以前聊过的事，只是性格和说话方式换了"),
        h("label", { class: "check" }, clear, " 清除记忆：从零开始（记忆、你的资料、她的生活经历都清空；聊天记录保留；清除前会在服务器上存一份备份）"),
        h("div", { class: "row" },
          h("button", { type: "button", class: "ghost", text: "取消", onclick: () => { d.close(); d.remove(); resolve(null); } }),
          h("button", { type: "button", text: "换人设", onclick: () => {
            const memory = clear.checked ? "clear" : "keep"; d.close(); d.remove(); resolve({ memory, name: name.value.trim() }); } }))));
      document.body.append(d); d.showModal();
    });
  }
  // the admin's own persona: starts from the persona in use; one item per line
  async function customDialog() {
    let core = {};
    try { core = ((await api("GET", "/api/persona")).core || {}).data || {}; } catch (_) { /* empty form */ }
    const ident = core.identity || {};
    const area = (value, rows, ph) => { const t = h("textarea", { rows, placeholder: ph }); t.value = value; return t; };
    const lines = (list) => (list || []).map((x) => (typeof x === "string" ? x : x.label)).join("\n");
    const f = {
      name: h("input", { maxlength: 20, placeholder: "她的名字" }),
      summary: h("input", { maxlength: 60, placeholder: "一句话介绍（只在后台显示）" }),
      self_description: area(ident.self_description || "", 2, "她平时过着什么样的日子"),
      personality: area(lines(core.personality), 4, "性格，每行一条"),
      communication_habits: area(lines(core.communication_habits), 3, "说话习惯，每行一条"),
      interests: area(lines(core.long_term_interests), 3, "兴趣爱好，每行一条（看书、音乐、写日记会带动她的虚拟生活）"),
      values: area(lines(core.values), 3, "看重什么，每行一条"),
      flaws: area(lines(core.flaws), 2, "小缺点，每行一条"),
    };
    f.name.value = ident.name || "";
    const keep = h("input", { type: "radio", name: "pc-mem", checked: true }), clear = h("input", { type: "radio", name: "pc-mem" });
    const err = h("p", { class: "err" });
    const d = h("dialog", { class: "wide" }, h("form", { method: "dialog", class: "stack" },
      h("h3", { text: "自定义人设" }),
      h("p", { class: "note", text: "从现在用的人设开始改。不能编造家人、工作、学校这些背景，也不会自称真人，这两条是固定的。" }),
      h("label", {}, "名字 ", f.name), h("label", {}, "介绍 ", f.summary), h("label", {}, "日常 ", f.self_description),
      h("label", {}, "性格 ", f.personality), h("label", {}, "说话习惯 ", f.communication_habits),
      h("label", {}, "兴趣 ", f.interests), h("label", {}, "看重 ", f.values), h("label", {}, "小缺点 ", f.flaws),
      h("label", { class: "check" }, keep, " 保留记忆"), h("label", { class: "check" }, clear, " 清除记忆（先在服务器上存备份）"),
      err,
      h("div", { class: "row" },
        h("button", { type: "button", class: "ghost", text: "取消", onclick: () => { d.close(); d.remove(); } }),
        h("button", { type: "button", text: "保存并使用", onclick: async () => {
          err.textContent = "";
          const memory = clear.checked ? "clear" : "keep";
          if (memory === "clear" && !(await confirmBox("确定清除全部记忆？她会忘记你和以前的一切（服务器上会保留一份备份文件）。"))) return;
          const persona = Object.fromEntries(Object.entries(f).map(([k, el]) => [k, el.value]));
          try {
            await api("POST", "/api/persona/custom", { persona, memory, confirm: true });
            d.close(); d.remove(); toast("已换成自定义人设"); loaders.persona();
          } catch (e) { err.textContent = e.message; }
        } }))));
    document.body.append(d); d.showModal();
  }

  // 0.7.9: the whole persona on hover (or "预览" on a phone), to compare before switching
  function previewBox(t) {
    const block = (title, items) => (items && items.length ? h("div", { class: "pt-block" }, h("div", { class: "k", text: title }),
      h("ul", {}, ...items.map((i) => h("li", { text: i })))) : null);
    return h("div", { class: "pt-preview", role: "tooltip" },
      h("strong", { text: t.blank ? "空白人设" + (t.name ? `（${t.name}）` : "") : (t.name || t.title) }),
      t.self_description ? h("p", { class: "note", text: t.self_description }) : null,
      t.blank ? h("p", { class: "note", text: "没有名字、性格和经历，什么都从和你聊天开始慢慢长出来。" }) : null,
      block("性格", t.personality), block("说话习惯", t.habits), block("兴趣", t.interests),
      block("看重", t.values), block("小缺点", t.flaws));
  }
  function withPreview(card, t) {
    if (!t) return card;
    card.classList.add("pt-card");
    card.tabIndex = 0;
    const toggle = h("button", { type: "button", class: "ghost pt-toggle", text: "预览", onclick: (ev) => {
      ev.stopPropagation(); card.classList.toggle("pt-open"); } });
    card.querySelector(".platform-head").append(toggle);
    card.append(previewBox(t));
    return card;
  }

  async function renderTemplates() {
    let d;
    try { d = await api("GET", "/api/persona/templates"); } catch (e) { $("#pl-templates").replaceChildren(h("p", { class: "err", text: e.message })); return; }
    const own = h("div", { class: "card platform" },
      h("div", { class: "platform-head" }, h("strong", { text: "自定义人设" }), d.current.template === "custom" ? pill("正在使用", "ok") : pill("自己写", "")),
      h("p", { class: "note platform-intro", text: "名字、性格、说话方式、兴趣都自己写；从现在用的人设开始改。" }),
      h("div", { class: "platform-body" }),
      h("div", { class: "row platform-foot" }, h("button", { type: "button", class: "ghost",
        text: d.current.template === "custom" ? "修改" : "自己写一个", onclick: customDialog })));
    withPreview(own, d.custom);
    $("#pl-templates").replaceChildren(...d.templates.map((t) => {
      const using = d.current.template === t.key;
      return withPreview(h("div", { class: "card platform" },
        h("div", { class: "platform-head" }, h("strong", { text: t.title }), using ? pill("正在使用", "ok") : pill("模板", "")),
        h("p", { class: "note platform-intro", text: t.summary }),
        h("div", { class: "platform-body" }, t.personality.length ? h("p", { class: "note", text: "性格：" + t.personality.slice(0, 2).join("；") }) : null),
        h("div", { class: "row platform-foot" }, using ? h("span", { class: "note", text: d.current.switched_at ? `${when(d.current.switched_at)} 换上` : "默认人设" })
          : h("button", { type: "button", class: "ghost", text: "用这个人设", onclick: async () => {
            const choice = await switchDialog(t);
            if (!choice) return;
            if (choice.memory === "clear" && !(await confirmBox("确定清除全部记忆？她会忘记你和以前的一切（服务器上会保留一份备份文件）。"))) return;
            try {
              const r = await api("POST", "/api/persona/switch", { template: t.key, memory: choice.memory, name: choice.name, confirm: true });
              toast(r.archive ? `已换人设，记忆已清除（备份：${r.archive}）` : "已换人设，记忆保留");
              loaders.persona();
            } catch (e) { toast(e.message, false); }
          } }))), t);
    }), own);
  }

  loaders.persona = async () => {
    renderTemplates();
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
