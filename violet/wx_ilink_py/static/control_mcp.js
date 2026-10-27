// Violet Control Center: MCP servers and tool permissions (phase 10.5). Replaces control.js's toolServerCard with a card
// that explains every state, offers safe fixes and lets the admin classify, authorise and test tools. The server
// decides and validates everything; unclassified tools stay refused until the admin classifies them one by one.
"use strict";
(() => {
  const LEVEL = { read: "只读", write: "写入", high_risk: "高风险", deny: "拒绝", unknown: "未分类（拒绝）" };
  const STATUS = { connected: ["已连接", "ok"], unavailable: ["未连接", "bad"], disabled: ["已停用", ""], unknown: ["还没检查", "warn"] };
  const FIND = { ok: "ok", info: "", warn: "warn", error: "bad" };
  const AGENTS = { chat: "Chat（聊天）", performer: "Performer（执行任务）", proactive: "Proactive（主动消息）" };
  const enc = encodeURIComponent;

  async function call(method, url, body, done) {
    try { const r = await api(method, url, body); if (done) toast(done); loaders.tools(); return r; }
    catch (e) { const p = e.data && e.data.problems; toast((p ? Object.values(p).join("；") : e.message), false); return null; }
  }

  function findingRow(s, f) {
    const fix = f.action === "apply_profile" ? h("button", { text: `一键套用模板“${f.profile}”`, onclick: () =>
        call("POST", `/api/control/tools/servers/${enc(s.name)}/profile`, { profile: f.profile }, "已套用模板，正在重新检查").then(() =>
          api("POST", "/api/control/tools/refresh", { server: s.name }).then(() => loaders.tools())) })
      : f.action === "check" ? h("button", { class: "ghost", text: "检查连接", onclick: () =>
        call("POST", "/api/control/tools/refresh", { server: s.name }, "已检查") })
      : f.action === "enable_server" ? h("button", { class: "ghost", text: "启用", onclick: () =>
        call("POST", `/api/control/tools/servers/${enc(s.name)}`, { enabled: true }, "已启用") }) : null;
    return h("div", { class: "row" }, pill({ ok: "正常", info: "提示", warn: "需要处理", error: "问题" }[f.level] || f.level, FIND[f.level]),
      h("span", { text: f.message }), fix);
  }

  function toolRow(s, t) {
    const select = h("select", { "aria-label": "分类" },
      h("option", { value: "", text: t.level_source === "template" ? `模板：${LEVEL[t.permission]}` : "未分类（拒绝）" }),
      ...["read", "write", "high_risk", "deny"].map((v) => h("option", { value: v, text: LEVEL[v] })));
    select.value = t.admin_level || "";
    select.addEventListener("change", async () => {
      const level = select.value || null;
      if (level === "write" || level === "high_risk") {
        if (!(await confirmBox(`把 ${t.name} 分类为“${LEVEL[level]}”？分类后还需要单独授权，Violet 才能用它执行操作。`))) { select.value = t.admin_level || ""; return; }
      }
      call("POST", `/api/control/tools/servers/${enc(s.name)}/tools/${enc(t.name)}/level`, { level }, "已保存分类");
    });
    const allow = t.needs_second_key ? h("button", { class: t.allowed_risky ? "danger" : "ghost", text: t.allowed_risky ? "撤销授权" : "授权使用",
      onclick: async () => {
        if (!t.allowed_risky && !(await confirmBox(`允许 Violet 使用 ${t.name}？这是${LEVEL[t.permission]}工具，Violet 会真的执行它的操作。`))) return;
        call("POST", `/api/control/tools/servers/${enc(s.name)}/tools/${enc(t.name)}/allow`, { allowed: !t.allowed_risky, confirm: true }, "已保存");
      } }) : null;
    const test = t.available && t.permission === "read" ? h("button", { class: "ghost", text: "测试", onclick: async () => {
      const props = Object.keys((t.input_schema || {}).properties || {});
      const raw = window.prompt(`测试 ${t.name}：参数（JSON）` + (props.length ? `，可用参数：${props.join("、")}` : ""), "{}");
      if (raw === null) return;
      let args; try { args = JSON.parse(raw || "{}"); } catch (_) { toast("参数不是合法的 JSON", false); return; }
      try {
        const r = await api("POST", `/api/control/tools/servers/${enc(s.name)}/tools/${enc(t.name)}/test`, { arguments: args });
        out.textContent = r.ok ? `成功（${r.latency_ms} 毫秒）：${r.result}` : `失败：${r.message}${r.code ? "（" + r.code + "）" : ""}`;
      } catch (e) { out.textContent = "失败：" + e.message; }
    } }) : null;
    const out = h("pre", { class: "note" });
    return h("div", { class: "stack" },
      h("div", { class: "row" }, h("strong", { text: t.name }), pill(t.available ? "可用" : "不可用", t.available ? "ok" : ""),
        select, allow, test,
        h("button", { class: "ghost", text: t.enabled ? "停用" : "启用", onclick: () =>
          call("POST", `/api/control/tools/servers/${enc(s.name)}/tools/${enc(t.name)}`, { enabled: !t.enabled }, t.enabled ? "已停用" : "已启用") })),
      h("div", { class: "note", text: (t.description || "") + (t.reason ? `　【${t.reason}】` : "") }), out);
  }

  window.toolServerCard = function (s) {
    const [st, kind] = STATUS[s.status] || [s.status, ""];
    const agents = h("div", { class: "row" }, h("span", { text: "授权给：" }), ...Object.entries(AGENTS).map(([a, label]) => {
      const box = h("input", { type: "checkbox" }); box.checked = s.agents.includes(a);
      box.addEventListener("change", () => {
        const next = Object.keys(AGENTS).filter((x) => (x === a ? box.checked : s.agents.includes(x)));
        call("POST", `/api/control/tools/servers/${enc(s.name)}`, { agents: next }, "已保存");
      });
      return h("label", { class: "check" }, box, " " + label);
    }));
    const origin = s.builtin ? "内置" : s.custom ? "后台添加" : ".env";
    const headers = (s.headers || []).map((x) => h("div", { class: "row" }, h("span", { text: `${x.header}：${x.is_set ? "已设置（" + x.fingerprint + "）" : "未设置"}` }),
      h("button", { class: "ghost", text: "替换", onclick: () => replaceToolSecret(s.name, x.header) })));
    return h("div", { class: "card" },
      h("div", { class: "row" }, h("strong", { text: s.name }), pill(st, kind), pill(origin),
        s.tool_profile ? pill("模板 " + s.tool_profile) : null,
        h("button", { class: "ghost", text: "检查连接", onclick: () => call("POST", "/api/control/tools/refresh", { server: s.name }, "已检查") }),
        h("button", { class: s.enabled ? "danger" : "ghost", text: s.enabled ? "停用服务器" : "启用服务器", onclick: () =>
          call("POST", `/api/control/tools/servers/${enc(s.name)}`, { enabled: !s.enabled }, "已保存") }),
        s.custom ? h("button", { class: "danger", text: "删除", onclick: async () => {
          if (!(await confirmBox(`删除 MCP 服务器“${s.name}”？它的凭据和工具分类会一起删除。`))) return;
          call("POST", `/api/control/tools/servers/${enc(s.name)}/delete`, { confirm: true }, "已删除");
        } }) : null),
      h("div", { class: "note", text: [s.url ? `地址 ${s.url}` : null, s.command, `传输 ${s.transport}`,
        `调用 ${s.total_calls} 次 / 失败 ${s.total_failures} 次`].filter(Boolean).join(" · ") }),
      ...(s.diagnosis || []).map((f) => findingRow(s, f)),
      agents, ...headers,
      ...(s.tools.length ? s.tools.map((t) => toolRow(s, t)) : [h("p", { class: "note", text: "还没有发现工具（连接成功后会列出）" })]));
  };

  $("#mcp-add").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const err = $("#mcp-add-error"); err.textContent = "";
    const body = { name: $("#mcp-name").value.trim(), url: $("#mcp-url").value.trim(), transport: $("#mcp-transport").value,
      agents: [...document.querySelectorAll("#mcp-add input[data-agent]:checked")].map((i) => i.dataset.agent),
      header: $("#mcp-header").value.trim() || "Authorization", header_value: $("#mcp-header-value").value || null };
    try {
      const s = await api("POST", "/api/control/tools/servers", body);
      ev.target.reset();
      const first = (s.diagnosis || [])[0];
      toast(first ? first.message : "已添加", !first || first.level === "ok");
      loaders.tools();
    } catch (e) { const p = e.data && e.data.problems; err.textContent = p ? Object.values(p).join("；") : e.message; }
  });
})();
