// Violet Control Center: her sticker collection (0.7.9), on the 图片 page under the image settings. Stickers come from
// what you send her on QQ, what she keeps from groups, the QQ account's own favourites (导入) or an upload here. Each has
// a one-line description: that is all the model sees when it picks one, so a good description matters. DOM nodes only.
"use strict";
(() => {
  const MAX = 5 * 1024 * 1024;

  async function act(method, url, body, done) {
    try { const r = await api(method, url, body || {}); if (done) toast(done); load(); return r; }
    catch (e) { toast(e.message, false); return null; }
  }

  function item(s) {
    const desc = h("input", { maxlength: 120, placeholder: "说明：画面、什么时候发、关键词" });
    desc.value = s.description || "";
    return h("div", { class: "stk-item" + (s.enabled ? "" : " off") },
      h("img", { src: `/api/control/stickers/${s.id}/file`, alt: s.description || "表情包", loading: "lazy" }),
      h("span", { class: "note", text: `#${s.id} · ${s.source_text}` + (s.use_count ? ` · 用过 ${s.use_count} 次` : "") }),
      desc,
      h("div", { class: "row" },
        h("button", { type: "button", class: "ghost", text: "保存", onclick: () => act("POST", `/api/control/stickers/${s.id}`, { description: desc.value }, "已保存") }),
        h("button", { type: "button", class: "ghost", text: "看一眼", title: "用识图模型重新写说明",
          onclick: () => act("POST", `/api/control/stickers/${s.id}`, { describe: true }, "说明已更新") }),
        h("button", { type: "button", class: "ghost", text: s.enabled ? "停用" : "启用",
          onclick: () => act("POST", `/api/control/stickers/${s.id}`, { enabled: !s.enabled }) }),
        h("button", { type: "button", class: "ghost", text: "删除", onclick: async () => {
          if (await confirmBox("删除这个表情包？")) act("DELETE", `/api/control/stickers/${s.id}`, null, "已删除");
        } })));
  }

  function upload() {
    const input = h("input", { type: "file", accept: "image/png,image/jpeg,image/gif,image/webp", multiple: true });
    input.addEventListener("change", async () => {
      let added = 0;
      for (const file of input.files) {
        if (file.size > MAX) { toast(`${file.name} 太大了（最多 5 MB）`, false); continue; }
        const data = await new Promise((resolve) => { const r = new FileReader(); r.onload = () => resolve(r.result); r.readAsDataURL(file); });
        try { await api("POST", "/api/control/stickers", { data }); added += 1; } catch (e) { toast(`${file.name}：${e.message}`, false); }
      }
      if (added) toast(`加了 ${added} 个表情包`);
      load();
    });
    input.click();
  }

  // 2026-10-31: a starter set of 12 drawn with her image model (ai/sticker_pack.py), in the background
  let packTimer = null;
  function packButton(p) {
    const btn = h("button", { type: "button", class: "ghost", text: p.running ? `画表情包中…（${p.done + p.failed}/${p.total}）` : "生成一套表情包",
      title: "用你的生图模型画 12 个她的 Q 版表情包（日常反应 + AI 自嘲梗），每个算一张图片的额度" });
    btn.disabled = p.running || !p.missing;
    if (!p.missing && !p.running) btn.textContent = "表情包套装已齐";
    btn.addEventListener("click", async () => {
      if (!(await confirmBox(`用你的生图模型画 ${p.missing} 个表情包？每个算一张图片（约 15–40 秒一张），画好的会自动放进收藏。`))) return;
      try { await api("POST", "/api/control/stickers/pack", {}); toast("开始画了，画好的会一个个出现"); load(); } catch (e) { toast(e.message, false); }
    });
    clearTimeout(packTimer);
    if (p.running) packTimer = setTimeout(load, 8000);
    if (p.running || p.missing === p.size) return btn;
    // 2026-10-31: the whole set again (the drawn ones are deleted first), e.g. after the style changed
    const redraw = h("button", { type: "button", class: "ghost", text: "重画整套",
      title: "删掉之前画的这一套，用现在的画风重新画 12 个" });
    redraw.addEventListener("click", async () => {
      if (!(await confirmBox(`删掉之前画的 ${p.size - p.missing} 个套装表情包，重新画整套 ${p.size} 个？每个算一张图片的额度。`))) return;
      try { await api("POST", "/api/control/stickers/pack", { redraw: true }); toast("开始重画了"); load(); } catch (e) { toast(e.message, false); }
    });
    return h("span", { class: "row" }, btn, redraw);
  }

  async function load() {
    const root = $("#stk-root");
    let d, pack = { running: false, missing: 0, done: 0, failed: 0, total: 0 };
    try { pack = await api("GET", "/api/control/stickers/pack"); } catch (_) { /* older server: no button state */ }
    try { d = await api("GET", "/api/control/stickers"); } catch (e) { root.replaceChildren(h("p", { class: "err", text: e.message })); return; }
    const importBtn = h("button", { type: "button", class: "ghost", text: "导入 QQ 收藏的表情", onclick: async () => {
      importBtn.disabled = true; importBtn.textContent = "导入中…";
      const r = await act("POST", "/api/control/stickers/import-qq", {});
      if (r) toast(`QQ 收藏里有 ${r.found} 个，新加了 ${r.added} 个` + (r.failed ? `，${r.failed} 个没下载下来` : "")
        + (r.added > r.described ? "；没有说明的可以点“看一眼”" : ""));
    } });
    importBtn.disabled = !d.qq_connected;
    const on = d.stickers.filter((s) => s.enabled).length;
    root.replaceChildren(
      h("div", { class: "row" }, h("h3", { text: "表情包" }), h("span", { class: "note", text: `${d.stickers.length} 个，${on} 个在用` })),
      h("p", { class: "note", text: (d.enabled ? "她聊天和在群里时会偶尔从这里挑一个发；挑的时候只看说明，所以说明要写清楚。" : "表情包功能关着（在“设置 → QQ 群聊、表情包与动态”里打开）。")
        + (d.auto_collect ? "你在 QQ 上发给她的表情包会自动收进来；" : "") + "群里别人发的，她觉得好玩会自己收藏。" }),
      h("div", { class: "row" }, h("button", { type: "button", text: "上传表情包", onclick: upload }), importBtn, packButton(pack),
        d.qq_connected ? null : h("span", { class: "note", text: "导入要先登录 QQ" })),
      d.stickers.length ? h("div", { class: "stk-grid" }, ...d.stickers.map(item))
        : h("p", { class: "note", text: "还没有表情包。" }));
  }

  const previous = loaders.images;
  loaders.images = async () => { if (previous) await previous(); load(); };
  $("#img-refresh").addEventListener("click", load);
})();
