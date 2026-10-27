// Violet Control Center: diagnosis page (phase 10.6). Findings come from GET /api/control/diagnostics; a finding with a
// "fix" gets a button that calls POST /api/control/diagnostics/fix (safe, bounded actions only).
"use strict";
(() => {
  const AREA = { core: "核心服务", channels: "聊天渠道", model: "模型", mcp: "MCP", tools: "工具", network: "外部网络" };
  const LEVEL = { error: ["问题", "bad"], warn: ["需要注意", "warn"], info: ["提示", ""], ok: ["正常", "ok"] };
  const FIX = { restart_channel: "重启这个渠道", check_mcp: "重新检查连接", apply_profile: "一键套用模板",
    retest_model: "重新测试模型", network_check: "检查外部网络" };

  async function load(deep) {
    try {
      const data = await api("GET", "/api/control/diagnostics" + (deep ? "?deep=1" : ""));
      $("#diag-summary").textContent = data.problems ? `发现 ${data.problems} 个需要处理的问题（按严重程度排列）` : "没有发现问题";
      $("#diag-list").replaceChildren(...data.findings.map(card));
    } catch (e) { toast(e.message, false); }
  }

  function card(f) {
    const [text, kind] = LEVEL[f.level] || [f.level, ""];
    const button = f.fix ? h("button", { class: "ghost", text: FIX[f.fix.action] || "修复", onclick: async () => {
      try {
        const r = await api("POST", "/api/control/diagnostics/fix", f.fix);
        toast(f.fix.action === "retest_model" ? (r.ok ? "模型测试通过" : "模型测试失败：" + r.message) : "已完成", f.fix.action !== "retest_model" || r.ok);
        load(false);
      } catch (e) { toast(e.message, false); }
    } }) : null;
    return h("div", { class: "card" },
      h("div", { class: "row" }, pill(text, kind), pill(AREA[f.area] || f.area), h("span", { text: f.message }), button),
      f.suggestion ? h("p", { class: "note", text: "建议：" + f.suggestion }) : null,
      f.turns && f.turns.length ? h("p", { class: "note", text: "相关轮次：" + f.turns.join("、") }) : null);
  }

  loaders.diagnostics = () => load(false);
  $("#diag-refresh").addEventListener("click", () => load(false));
  $("#diag-deep").addEventListener("click", () => load(true));
  $("#diag-net").addEventListener("click", async () => {
    try { await api("POST", "/api/control/diagnostics/fix", { action: "network_check" }); toast("已检查"); load(false); }
    catch (e) { toast(e.message, false); }
  });
})();
