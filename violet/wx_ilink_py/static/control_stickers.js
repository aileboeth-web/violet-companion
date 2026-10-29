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
    const desc = h("input", { maxlength: 60, placeholder: "说明：画面、情绪、什么时候用" });
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

  async function load() {
    const root = $("#stk-root");
    let d;
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
      h("div", { class: "row" }, h("button", { type: "button", text: "上传表情包", onclick: upload }), importBtn,
        d.qq_connected ? null : h("span", { class: "note", text: "导入要先登录 QQ" })),
      d.stickers.length ? h("div", { class: "stk-grid" }, ...d.stickers.map(item))
        : h("p", { class: "note", text: "还没有表情包。" }));
  }

  const previous = loaders.images;
  loaders.images = async () => { if (previous) await previous(); load(); };
  $("#img-refresh").addEventListener("click", load);
})();
