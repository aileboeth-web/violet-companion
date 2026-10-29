// Violet Control Center: first-run setup page (phase 10.3). Uses control.js's helpers ($, h, api, toast, pill, fmt,
// loaders). Saves through PUT /api/control/settings (validated, recorded, reversible); the server decides everything.
"use strict";
(() => {
  const STATE = { ok: ["已完成", "ok"], todo: ["待完成", "bad"], warn: ["需要检查", "warn"], optional: ["可选", ""] };

  // Violet stage 3: every item is a link to the exact place where it is set (page + anchor, from the server)
  function renderChecklist(data) {
    $("#setup-summary").textContent = data.complete
      ? "必需的设置都完成了，Violet 可以正常聊天。下面的可选项可以随时调整，点一项直接过去。"
      : "还有必需的设置没有完成：" + (data.next ? data.next.title + "（" + data.next.detail + "）" : "") + "。点下面的卡片直接过去。";
    $("#setup-list").replaceChildren(...data.items.map((item) => {
      const [text, kind] = STATE[item.state] || [item.state, ""];
      const go = () => goTo(item.tab, item.anchor);
      return h("div", { class: "card link " + (item.state === "todo" || item.state === "warn" ? "attention" : ""),
        role: "button", tabindex: "0", onclick: go, onkeydown: (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); go(); } } },
        h("div", { class: "row" }, pill(text, kind), h("strong", { text: item.title }),
          h("span", { class: "go", text: item.state === "ok" || item.state === "optional" ? "查看" : "去处理" })),
        h("p", { class: "note", text: item.detail }));
    }));
  }

  // 0.7.6: while a channel is still connecting, look again every few seconds
  let pending = null, tries = 0;
  loaders.setup = async (again) => {
    clearTimeout(pending);
    try {
      const data = await api("GET", "/api/control/setup");
      renderChecklist(data);
      if (data.items.some((i) => i.connecting) && tries++ < 40) {
        pending = setTimeout(() => { if ($("#tab-setup").classList.contains("active") && !document.hidden) loaders.setup(true); }, 3000);
      } else tries = 0;
    } catch (e) { toast(e.message, false); }
    if (again !== true) loadUpdate(false);
  };

  // version & updates: the server asks GitHub (cached 6 h). Violet stage 3: a root + systemd install updates itself
  // (mode "auto") or on "update now"; other installs show the command to run on the server
  const MODES = { auto: "自动更新", notify: "自动检查，只提醒", manual: "手动检查" };
  const RESULTS = { updated: "已更新", up_to_date: "已是最新", failed: "更新失败", error: "检查失败" };
  function renderUpdate(u) {
    const when = u.published_at ? `，发布于 ${fmt(u.published_at.slice(0, 10))}` : "";
    const auto = u.auto || {};
    let pillArgs, text;
    if (u.error) { pillArgs = ["无法检查", "warn"]; text = `当前版本 ${u.current}。${u.error}`; }
    else if (!u.latest && !u.enabled) { pillArgs = ["未检查", ""]; text = `当前版本 ${u.current}。手动检查模式：点“检查更新”查询。`; }
    else if (u.newer) { pillArgs = ["有新版本", "ok"]; text = `当前版本 ${u.current}，最新版本 ${u.latest}${when}。`; }
    else { pillArgs = ["已是最新", ""]; text = `当前版本 ${u.current}，最新发布 ${u.latest || "无"}${when}。`; }
    if (auto.requested) text += " 已提交更新，服务器正在升级，完成后 Violet 会自动重启。";
    $("#update-pill").replaceChildren(pill(...pillArgs));
    // replaceChildren(null) would insert the text "null": only real nodes
    $("#update-text").replaceChildren(...[h("span", { text }),
      u.newer && u.url ? h("a", { href: u.url, target: "_blank", rel: "noopener", text: " 查看更新说明" }) : null].filter(Boolean));
    const mode = `更新方式：${MODES[u.mode] || u.mode}（设置 → 版本更新）。`;
    const last = auto.last_result ? ` 上次自动更新：${when_(auto.last_run_at)} ${RESULTS[auto.last_result] || auto.last_result}` +
      (auto.last_message ? `（${auto.last_message}）` : "") + "。" : "";
    $("#update-auto").textContent = auto.available ? mode + last : mode + " " + (auto.reason || "");
    // an update waiting / running (from this page or the automatic mode): the same button shows its progress
    $("#update-apply").hidden = !(auto.available && (auto.requested || u.newer));
    $("#update-apply").textContent = auto.requested ? "查看升级进度" : "立即更新";
    $("#update-apply").dataset.watch = auto.requested ? "1" : "";
    $("#update-command").textContent = u.command;
    $("#update-command-row").hidden = !(u.newer && !auto.available);
  }
  const when_ = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "");

  // 0.7.0: the progress dialog; Violet restarts during the update, the dialog waits for it and reads the outcome
  function watchUpdate() {
    progressDialog({ title: "升级 Violet", url: "/api/control/progress/update", restarts: true, onFinish: (p, extra) => {
      loadUpdate(false);
      if (p.state === "done" && p.version) {
        extra.replaceChildren(h("button", { type: "button", text: "刷新页面（加载新版本）", onclick: () => location.reload() }));
      }
    } });
  }
  $("#update-apply").addEventListener("click", async () => {
    if ($("#update-apply").dataset.watch) { watchUpdate(); return; }
    if (!(await confirmBox("现在升级到最新版本？会先备份，升级期间 Violet 停止大约一两分钟；新版本起不来会自动退回旧版本。"))) return;
    try { await api("POST", "/api/control/update/apply", { confirm: true }); loadUpdate(false); watchUpdate(); }
    catch (e) { toast("提交失败：" + e.message, false); }
  });

  async function loadUpdate(refresh) {
    try { renderUpdate(await api("GET", "/api/control/update" + (refresh ? "?refresh=1" : ""))); }
    catch (e) { $("#update-text").textContent = e.message; }
  }

  $("#update-refresh").addEventListener("click", () => loadUpdate(true));
  $("#update-copy").addEventListener("click", () => copyText($("#update-command").textContent));
})();
