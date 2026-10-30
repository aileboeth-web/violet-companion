// Violet Control Center: QQ groups and other people (2026-10-29), under the channel cards when "QQ" (SnowLuma) is used.
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

  // 0.7.9: QQ 空间 -- what she posted, why she is waiting, and "let her write one" (shown first, posted on confirm)
  async function qzoneBox(box) {
    let d;
    try { d = await api("GET", "/api/control/qzone"); } catch (e) { box.replaceChildren(h("p", { class: "err", text: e.message })); return; }
    const STATUS = { sent: ["已发", "ok"], failed: ["没发出去", "bad"] };
    const draft = h("textarea", { class: "qz-draft", maxlength: 1000, placeholder: "点“让她写一条”，或者自己写；发之前可以改" });
    const note = h("p", { class: "note" });
    const writeBtn = h("button", { type: "button", class: "ghost", text: "让她写一条", onclick: async () => {
      writeBtn.disabled = true; note.textContent = "她在想……";
      try {
        const r = await api("POST", "/api/control/qzone/draft", {});
        draft.value = r.text || "";
        note.textContent = r.post ? `她写好了（理由：${r.reason || "—"}），可以改了再发。` : `她觉得现在没什么想发的（${r.reason || "—"}）。`;
      } catch (e) { note.textContent = ""; toast(e.message, false); }
      writeBtn.disabled = false;
    } });
    const sendBtn = h("button", { type: "button", text: "发到 QQ 空间", onclick: async () => {
      const text = draft.value.trim();
      if (!text) { toast("动态内容是空的", false); return; }
      if (!(await confirmBox(`用小号在 QQ 空间发这条动态？\n\n${text}`, { danger: false, ok: "发出去" }))) return;
      try { await api("POST", "/api/control/qzone/publish", { text, confirm: true }); toast("已发出"); qzoneBox(box); }
      catch (e) { toast(e.message, false); qzoneBox(box); }
    } });
    sendBtn.disabled = !d.qq_connected;
    box.replaceChildren(
      h("p", { class: "note", text: (d.enabled ? `自己发动态：已打开（每天最多 ${d.daily || "不限"} 条）` : "自己发动态：没有打开（在“设置 → QQ 群聊、表情包与动态”里打开）")
        + (d.enabled && d.waiting ? ` · 现在不会发：${d.waiting}` : "") + (d.qq_connected ? "" : " · QQ 没有连接") }),
      draft, note, h("div", { class: "row" }, writeBtn, sendBtn),
      ...(d.posts.length ? d.posts.map((p) => {
        const [t, k] = STATUS[p.status] || [p.status, ""];
        return h("div", { class: "qz-post" }, h("div", { class: "row" }, pill(t, k),
          h("span", { class: "note", text: `${when(p.created_at)} · ${p.origin === "auto" ? "她自己发的" : "后台发的"}` })),
          h("div", { text: p.content }), p.reason ? h("p", { class: "note", text: "理由：" + p.reason }) : null,
          p.error ? h("p", { class: "err", text: p.error }) : null);
      }) : [h("p", { class: "note", text: "还没有发过动态。" })]));
  }

  window.renderSocial = async (root, used) => {
    if (!used) { root.replaceChildren(); return; }
    let d;
    try { d = await api("GET", "/api/control/social"); } catch (e) { root.replaceChildren(h("p", { class: "err", text: e.message })); return; }
    modes = d.modes;
    const people = h("div", { class: "stack" }), qzone = h("div", { class: "stack" });
    const groups = [...d.groups].sort((a, b) => (b.mode !== "off") - (a.mode !== "off") || String(a.title).localeCompare(String(b.title)));
    const PRIV = { off: "不理", known: "只回群里认识的人", all: "谁都回" };
    root.replaceChildren(h("div", { class: "set-group social" },
      h("h3", { text: "QQ 群聊" }),
      h("p", { class: "note", text: "小号在哪些群里，这里就列出哪些群，新群默认关闭。打开的群里，她会看大家在聊什么，自己决定插不插话；“只看不说”只了解群里的事。上限默认不限。" }),
      d.error ? h("p", { class: "note", text: d.error }) : null,
      ...(groups.length ? groups.map((g) => convCard(g, true)) : [h("p", { class: "note", text: "还没有群。把这个 QQ 号拉进群后点“刷新状态”。" })]),
      h("h3", { text: "别人私聊她" }),
      h("p", { class: "note", text: `现在的设置：${PRIV[d.settings.private] || d.settings.private}（在“设置 → QQ 群聊、表情包与动态”里改）。你自己的私聊不在这里，照常进你们的对话。` }),
      ...(d.privates.length ? d.privates.map((c) => convCard(c, false)) : [h("p", { class: "note", text: "还没有别人私聊过她。" })]),
      h("h3", { text: "QQ 动态" }),
      qzone,
      h("details", { ontoggle: (ev) => { if (ev.target.open) peopleBox(people); } },
        h("summary", { text: "她认识的人（对每个人的印象）" }), people)));
    qzoneBox(qzone);
  };
})();
