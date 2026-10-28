// Violet Control Center: the run log (Violet stage 3). The server reads the newest part of main.log, groups it into
// records, filters it and removes every secret before answering. Uses control.js's helpers. DOM nodes only.
"use strict";
(() => {
  const LEVEL = { DEBUG: ["调试", ""], INFO: ["信息", ""], WARNING: ["警告", "warn"], ERROR: ["错误", "bad"], CRITICAL: ["严重", "bad"] };
  let timer = null;

  function query() {
    const p = new URLSearchParams({ level: $("#log-level").value, limit: "300" });
    if ($("#log-q").value.trim()) p.set("q", $("#log-q").value.trim());
    if ($("#log-turn").value.trim()) p.set("turn", $("#log-turn").value.trim());
    if ($("#log-trace").checked) p.set("logger", "violet.trace");        // 0.7.5: the troubleshooting log only
    return p.toString();
  }

  function entry(r) {
    const [label, kind] = LEVEL[r.level] || [r.level, ""];
    const turn = r.turn ? h("button", { class: "mini ghost", text: r.turn, title: "只看这一轮对话",
      onclick: () => { $("#log-turn").value = r.turn; loaders.logs(); } }) : null;
    const head = h("div", { class: "log-head" }, pill(label, kind), h("span", { class: "log-time", text: r.time || "" }),
      h("span", { class: "log-logger", text: r.logger }), turn);
    const msg = h("div", { class: "log-msg", text: r.message });
    const detail = r.detail && r.detail.length
      ? h("details", {}, h("summary", { text: `详细信息（${r.detail.length} 行）` }), h("pre", { class: "log-detail", text: r.detail.join("\n") }))
      : null;
    return h("div", { class: "log-entry lv-" + r.level.toLowerCase() }, head, msg, detail);
  }

  loaders.logs = async () => {
    try {
      const d = await api("GET", "/api/control/logs?" + query());
      if (!d.available) {
        $("#log-summary").replaceChildren(pill("没有日志", "warn"), h("span", { class: "note", text: d.reason }));
        $("#log-list").replaceChildren();
        return;
      }
      const c = d.counts || {};
      $("#log-summary").replaceChildren(
        pill(`错误 ${(c.ERROR || 0) + (c.CRITICAL || 0)}`, (c.ERROR || c.CRITICAL) ? "bad" : ""),
        pill(`警告 ${c.WARNING || 0}`, c.WARNING ? "warn" : ""), pill(`信息 ${c.INFO || 0}`),
        h("span", { class: "note", text: `显示 ${d.entries.length} 条${d.truncated ? "（只显示最新的 300 条，用筛选缩小范围）" : ""}` +
          ` · 最近 ${Math.round(d.window_bytes / 1024)} KB${d.older_not_shown ? "，更早的没有显示" : ""}` +
          ` · 更新于 ${new Date(d.modified * 1000).toLocaleString()}` }));
      const empty = $("#log-trace").checked && !d.trace_enabled
        ? "排查日志没有开：在“设置 → 日志与排查”里打开“排查日志”，再聊一句，这里就会出现每一步的记录。"
        : "没有符合条件的记录";
      $("#log-list").replaceChildren(...(d.entries.length ? d.entries.map(entry) : [h("p", { class: "note", text: empty })]));
    } catch (e) { toast("日志读取失败：" + e.message, false); }
  };

  $("#log-search").addEventListener("click", () => loaders.logs());
  $("#log-level").addEventListener("change", () => loaders.logs());
  $("#log-trace").addEventListener("change", () => loaders.logs());
  for (const id of ["#log-q", "#log-turn"]) $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") loaders.logs(); });
  $("#log-auto").addEventListener("change", (e) => {
    clearInterval(timer);
    timer = e.target.checked ? setInterval(() => {
      if ($("#tab-logs").classList.contains("active") && !document.hidden) loaders.logs();
    }, 5000) : null;
  });
  $("#log-download").addEventListener("click", () => { location.href = "/api/control/logs/download"; });
})();
