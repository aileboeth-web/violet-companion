// 2026-11-02 (user: 之前的聊天记录在 Eve，想导进来，可以选聊天渠道；Eve 配置文件里改过的发送频率也要): upload the
// package made on the Eve server, look at the counts (never the messages themselves), choose the channel and the parts,
// import. Server side: ai/eve_import.py, routes in control_routes.py (/api/control/eve-import/...).
"use strict";
(() => {
  const PACK = "mkdir -p /root/eve-export; D=$(dirname \"$(find / -xdev -name Memories.db -not -path '/opt/violet/*' -not -path '/proc/*' 2>/dev/null | head -n1)\"); " +
    "echo \"Eve 目录: $D\"; python3 -c \"import sqlite3,sys;s=sqlite3.connect(sys.argv[1]);d=sqlite3.connect(sys.argv[2]);s.backup(d);d.close();print('数据库已复制')\" \"$D/Memories.db\" /root/eve-export/Memories.db; " +
    "cp \"$D/proactive_config.json\" /root/eve-export/; grep -E '^[A-Z0-9_]+=' \"$D/.env\" | grep -vE 'KEY|TOKEN|SECRET|PASSWORD|PROMPT|URL|MQTT' > /root/eve-export/env-safe.txt; " +
    "cd /root; tar czf eve-export.tar.gz eve-export; ls -l /root/eve-export.tar.gz";
  const SIZE = (n) => (n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB");
  const show = (v) => (v === true ? "开" : v === false ? "关" : typeof v === "object" && v !== null ? JSON.stringify(v) : fmt(v));

  function upload(file, bar) {
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      x.open("POST", "/api/control/eve-import/upload");
      x.setRequestHeader("Content-Type", "application/octet-stream");
      x.setRequestHeader("X-Filename", encodeURIComponent(file.name).slice(0, 200));
      x.setRequestHeader("Accept", "application/json");
      x.upload.onprogress = (ev) => { if (ev.lengthComputable) bar.textContent = `上传中 ${Math.round(ev.loaded / ev.total * 100)}%`; };
      x.onload = () => {
        if (x.status === 401) { location.href = "/auth/login?next=/control"; return; }
        let d = null; try { d = JSON.parse(x.responseText); } catch (_) { /* not json */ }
        if (x.status >= 400 || !d || d.ok === false) reject(new Error((d && d.error) || "上传失败 " + x.status)); else resolve(d.data);
      };
      x.onerror = () => reject(new Error("网络错误，上传没有完成"));
      x.send(file);
    });
  }

  function renderPreview(p) {
    const box = $("#eve-preview");
    const m = p.messages, core = p.core, ev = p.events;
    const channel = h("select", { "aria-label": "导入到哪个渠道" }, ...p.channels.map((c) => h("option", { value: c.id, text: c.label + (c.persona ? "（人设：" + c.persona + "）" : "") })));
    channel.value = p.channels.some((c) => c.id === "wechat") ? "wechat" : (p.channels[0] || {}).id || "";
    const msgBox = h("input", { type: "checkbox", checked: !!(m && m.count) , disabled: !(m && m.count) });
    const memBox = h("input", { type: "checkbox", checked: !!((core && core.count) || (ev && ev.count)), disabled: !((core && core.count) || (ev && ev.count)) });
    const picks = [];
    const usable = p.settings.filter((s) => s.ok);
    const rows = usable.map((s) => {
      const cb = h("input", { type: "checkbox", checked: !s.same, "aria-label": s.label });
      picks.push([s.key, cb]);
      return h("tr", {}, h("td", {}, cb), h("td", { text: s.label }), h("td", { text: show(s.eve) }), h("td", { text: show(s.now) }),
        h("td", { class: "note", text: s.same ? "一样" : `${s.source} · ${s.eve_key}` }));
    });
    const skipped = p.settings.filter((s) => !s.ok);
    const before = p.imported_before.length
      ? h("p", { class: "err", text: `这个文件 ${when(p.imported_before[0].created_at)} 已经导入过：再导入时已有的消息和记忆会自动跳过，不会重复。` }) : null;
    box.replaceChildren(...[                       // (filter: replaceChildren would print "null")
      h("div", { class: "cards" },
        stat("聊天记录", m ? `${m.count} 条（你 ${m.user} · 她 ${m.assistant}）` : "文件里没有"),
        stat("时间范围", m && m.count ? `${when(m.first)} — ${when(m.last)}` : "—"),
        stat("核心记忆", core ? `${core.count} 条` + (core.existing ? `（${core.existing} 条 Violet 已有，保留 Violet 的）` : "") : "文件里没有"),
        stat("事件记忆", ev ? `${ev.count} 条` : "文件里没有")),
      before,
      h("div", { class: "card" },
        h("label", { class: "check" }, msgBox, h("span", { text: " 导入聊天记录到 " }), channel),
        h("p", { class: "note", text: "会排在现有聊天的前面（按时间），算作你在这个渠道上和她的私聊，她之后聊天、判断你们多熟时会看到。聊天记录和记忆都归这个渠道用的人设：别的人设不会知道（每个人设是不同的人）。想让 Eve 的记忆归 Eve，先在“人设与生活”里给这个渠道选好 Eve 人设，再导入。图片、语音只导入当时记下的文字。" }),
        h("label", { class: "check" }, memBox, h("span", { text: " 导入记忆（核心记忆和事件记忆）" }))),
      usable.length ? h("div", { class: "card" }, h("h3", { text: "设置（发送频率等）" }),
        h("p", { class: "note", text: "勾上的会覆盖 Violet 现在的值，记在“修改记录”里，可以回滚。和 Violet 一样的默认不勾。" }),
        h("div", { class: "table-wrap" }, h("table", {}, h("tr", {}, h("th", { text: "" }), h("th", { text: "设置" }), h("th", { text: "Eve" }),
          h("th", { text: "Violet 现在" }), h("th", { text: "来源" })), ...rows))) : null,
      skipped.length ? h("details", { class: "card" }, h("summary", { text: `不导入的 ${skipped.length} 项（看原因）` }),
        ...skipped.map((s) => h("p", { class: "note", text: `${s.eve_key} = ${show(s.eve).slice(0, 80)}：${s.why}` }))) : null,
      h("div", { class: "row" }, h("button", { type: "button", text: "确认导入", onclick: async () => {
        const settings = picks.filter(([, cb]) => cb.checked).map(([k]) => k);
        if (!msgBox.checked && !memBox.checked && !settings.length) { toast("什么都没选", false); return; }
        const what = [msgBox.checked ? `聊天记录 → ${channel.options[channel.selectedIndex].text}` : null, memBox.checked ? "记忆" : null,
          settings.length ? `${settings.length} 项设置` : null].filter(Boolean).join("、");
        if (!(await confirmBox(`导入 ${what}？聊天记录导入后不能在页面上删除。`, { danger: false, ok: "导入" }))) return;
        try {
          const r = await api("POST", `/api/control/eve-import/${p.token}/commit`, { confirm: true, channel: channel.value,
            messages: msgBox.checked, memories: memBox.checked, settings });
          box.replaceChildren(h("div", { class: "card" }, h("h3", { text: "导入完成" }),
            h("p", { text: `聊天记录 ${r.messages} 条` + (r.messages_skipped ? `（${r.messages_skipped} 条已经有了，跳过）` : "") +
              `；核心记忆 ${r.core} 条` + (r.core_skipped ? `（跳过 ${r.core_skipped} 条）` : "") + `；事件记忆 ${r.events} 条；设置 ${r.settings.length} 项。` }),
            h("p", { class: "note", text: "上传的文件已经从服务器上删掉。" }),
            h("div", { class: "row" }, h("button", { type: "button", class: "ghost", text: "去看聊天记录", onclick: () => goTo("chat") }))));
          toast("已导入");
        } catch (e) { toast("导入失败：" + e.message, false); }
      } }))].filter(Boolean));
  }

  // 2026-11-02: a part of the chat history page ("导入聊天记录和记忆", source Project Eve), not a page of its own
  $("#eve-pack").textContent = PACK;
  $("#eve-pack-copy").addEventListener("click", () => copyText(PACK));
  $("#eve-upload").addEventListener("click", async () => {
    const file = $("#eve-file").files[0];
    if (!file) { toast("先选择文件", false); return; }
    const bar = $("#eve-status");
    bar.textContent = `上传中（${SIZE(file.size)}）…`;
    try {
      const p = await upload(file, bar);
      bar.textContent = "已读取：下面是文件里有的内容，还没有导入。";
      renderPreview(p);
    } catch (e) { bar.textContent = ""; toast(e.message, false); }
  });
})();
