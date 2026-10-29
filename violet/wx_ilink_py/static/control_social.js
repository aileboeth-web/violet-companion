// Violet Control Center: QQ groups and other people (2026-10-29), under the channel cards when "QQ" (NapCat) is used.
// Per group: how she takes part (关闭 / 只看不说 / 被 @ 才说 / 自己决定), daily limits (0 = no limit, the default), her
// impression of the group, and her recent decisions (why she spoke or stayed silent). Then other people's private
// chats and the people she knows. Everything saves through /api/control/social/*. DOM nodes only.
"use strict";
(() => {
  const PRIVATE_MODES = [{ value: "auto", text: "她自己决定回不回" }, { value: "off", text: "不理这个人" }];
  let modes = [];

  async function saveConv(id, body, quiet) {
    try { const r = await api("POST", `/api/control/social/conversation/${id}`, body); if (!quiet) toast("已保存"); return r; }
    catch (e) { toast(e.message, false); return null; }
  }

  const ACTION = { silent: "没说话", say: "说了话", reply: "引用回复", wait: "过会儿再看", error: "出错" };
  async function loadDecisions(id, box) {
    try {
      const d = await api("GET", `/api/control/social/conversation/${id}/decisions`);
      box.replaceChildren(...(d.decisions.length ? d.decisions.map((x) => h("div", { class: "decision" },
        h("div", { class: "row" }, pill(ACTION[x.action] || x.action, x.action === "error" ? "bad" : x.action === "silent" ? "" : "ok"),
          h("span", { class: "note", text: when(x.created_at) + (x.wait_minutes ? ` · ${x.wait_minutes} 分钟后再看` : "") })),
        x.text ? h("div", { text: x.text }) : null,
        x.reason ? h("p", { class: "note", text: "理由：" + x.reason }) : null,
        x.error ? h("p", { class: "err", text: x.error }) : null))
        : [h("p", { class: "note", text: "还没有做过决定。" })]));
    } catch (e) { box.replaceChildren(h("p", { class: "err", text: e.message })); }
  }

  function limitInput(value) {
    const i = h("input", { type: "number", min: 0, max: 100000, step: 1, class: "limit" });
    i.value = value ?? 0;
    return i;
  }

  function convCard(c, isGroup) {
    const sel = h("select", { "aria-label": "参与方式" }, ...(isGroup ? modes : PRIVATE_MODES).map((m) => h("option", { value: m.value, text: m.text })));
    sel.value = c.mode;
    const msgLimit = limitInput(c.daily_message_limit), decLimit = limitInput(c.daily_decision_limit);
    const notes = h("textarea", { rows: 2, maxlength: 600, placeholder: isGroup ? "她对这个群的印象（她自己会慢慢写，你也可以改）" : "" });
    notes.value = c.notes || "";
    const decisions = h("div", { class: "stack" });
    const on = c.mode !== "off";
    sel.addEventListener("change", async () => { if (await saveConv(c.id, { mode: sel.value })) loaders.channels(); });
    return h("div", { class: "card social-conv" + (on ? "" : " off") },
      h("div", { class: "row" }, h("strong", { text: c.title }),
        h("span", { class: "note", text: (isGroup ? `群 ${c.peer_id}` : `QQ ${c.peer_id}`) + (c.member_count ? ` · ${c.member_count} 人` : "") + (c.in_group === false ? " · 已不在这个群" : "") }),
        h("span", { class: "grow" }), sel),
      on ? h("p", { class: "note", text: `今天：做了 ${c.today.decisions} 次决定 · 说了 ${c.today.messages} 句` +
        (c.next_wake_at ? ` · ${when(c.next_wake_at)} 回来看看` : "") }) : null,
      on ? h("details", {}, h("summary", { text: "上限和印象" }), h("div", { class: "stack" },
        h("div", { class: "row" }, h("label", {}, "每天最多说几句 ", msgLimit), h("label", {}, "每天最多决定几次（每次调用一次模型） ", decLimit),
          h("span", { class: "note", text: "0 = 不限" })),
        isGroup ? h("label", {}, "她对这个群的印象 ", notes) : null,
        h("div", { class: "row" }, h("button", { type: "button", text: "保存", onclick: () => saveConv(c.id, {
          daily_message_limit: Number(msgLimit.value || 0), daily_decision_limit: Number(decLimit.value || 0),
          ...(isGroup ? { notes: notes.value } : {}) }) })))) : null,
      on ? h("details", { ontoggle: (ev) => { if (ev.target.open) loadDecisions(c.id, decisions); } },
        h("summary", { text: "最近的决定（为什么说 / 不说）" }), decisions) : null);
  }

  async function peopleBox(box) {
    try {
      const d = await api("GET", "/api/control/social/people");
      box.replaceChildren(...(d.people.length ? d.people.map((p) => {
        const t = h("input", { maxlength: 200, placeholder: "还没有印象" }); t.value = p.impression || "";
        return h("div", { class: "row person" }, h("strong", { text: p.nickname || p.user_id }),
          h("span", { class: "note", text: `QQ ${p.user_id} · ${p.message_count} 条 · 最近 ${when(p.last_seen_at)}` }),
          h("label", { class: "grow" }, t),
          h("button", { class: "ghost", type: "button", text: "保存印象", onclick: async () => {
            try { await api("POST", `/api/control/social/people/${p.id}`, { impression: t.value }); toast("已保存"); } catch (e) { toast(e.message, false); }
          } }));
      }) : [h("p", { class: "note", text: "她还不认识任何人。" })]));
    } catch (e) { box.replaceChildren(h("p", { class: "err", text: e.message })); }
  }

  window.renderSocial = async (root, used) => {
    if (!used) { root.replaceChildren(); return; }
    let d;
    try { d = await api("GET", "/api/control/social"); } catch (e) { root.replaceChildren(h("p", { class: "err", text: e.message })); return; }
    modes = d.modes;
    const people = h("div", { class: "stack" });
    const groups = [...d.groups].sort((a, b) => (b.mode !== "off") - (a.mode !== "off") || String(a.title).localeCompare(String(b.title)));
    const PRIV = { off: "不理", known: "只回群里认识的人", all: "谁都回" };
    root.replaceChildren(h("div", { class: "set-group social" },
      h("h3", { text: "QQ 群聊" }),
      h("p", { class: "note", text: "小号在哪些群里，这里就列出哪些群，新群默认关闭。打开的群里，她会看大家在聊什么，自己决定插不插话；“只看不说”只了解群里的事。上限默认不限。" }),
      d.error ? h("p", { class: "note", text: d.error }) : null,
      ...(groups.length ? groups.map((g) => convCard(g, true)) : [h("p", { class: "note", text: "还没有群。把这个 QQ 号拉进群后点“刷新状态”。" })]),
      h("h3", { text: "别人私聊她" }),
      h("p", { class: "note", text: `现在的设置：${PRIV[d.settings.private] || d.settings.private}（在“设置 → QQ 群聊与其他人”里改）。你自己的私聊不在这里，照常进你们的对话。` }),
      ...(d.privates.length ? d.privates.map((c) => convCard(c, false)) : [h("p", { class: "note", text: "还没有别人私聊过她。" })]),
      h("details", { ontoggle: (ev) => { if (ev.target.open) peopleBox(people); } },
        h("summary", { text: "她认识的人（对每个人的印象）" }), people)));
  };
})();
