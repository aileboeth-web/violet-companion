// Violet Control Center: the phone MCP card on the tools page (Violet stage 3). Steps in the order the user does them:
// relay on this server -> app on the phone -> the three relay values (copy buttons) -> the phone's access token back
// here. GET /api/control/phone-mcp, POST /api/control/phone-mcp/connect. DOM nodes only.
"use strict";
(() => {
  let showSecret = false;

  function valueRow(label, value, { secret = false, help = "" } = {}) {
    const shown = secret && !showSecret && value ? "•".repeat(Math.min(value.length, 24)) : (value || "—");
    return h("div", { class: "kv" },
      h("span", { class: "k", text: label }),
      h("code", { class: "v", text: shown }),
      h("span", { class: "kv-actions" },
        secret && value ? h("button", { class: "ghost", type: "button", text: showSecret ? "隐藏" : "显示",
          onclick: () => { showSecret = !showSecret; load(); } }) : null,
        value ? h("button", { type: "button", text: "复制", onclick: () => copyText(value) }) : null),
      help ? h("p", { class: "note", text: help }) : null);
  }

  function step(n, title, ...body) {
    return h("div", { class: "phone-step" }, h("div", { class: "phone-num", text: String(n) }),
      h("div", { class: "phone-body" }, h("strong", { text: title }), ...body));
  }

  function statusPill(d) {
    if (!d.installed) return pill("还没有中继", "warn");
    if (d.health.checked && !d.health.reachable) return pill("中继没有响应", "bad");
    if (d.health.device_connected) return pill("手机已连接", "ok");
    return pill("中继运行中 · 手机未连接", "warn");
  }

  function render(d) {
    const box = $("#phone-mcp");
    const head = h("div", { class: "row phone-head" }, h("h3", { text: "手机 MCP（Violet MCP App）" }), statusPill(d),
      h("button", { class: "ghost", type: "button", text: "刷新", onclick: load }));
    const intro = h("p", { class: "note", text: "让 Violet 能看手机上的一些状态（前台应用、屏幕、电量等，只读）。手机主动连到这台服务器上的中继，不需要手机有公网 IP。" });
    if (!d.installed) {
      box.replaceChildren(head, intro,
        step(1, "在服务器上安装中继（只需一次）",
          d.install_command ? h("div", { class: "kv" }, h("code", { class: "v", text: d.install_command }),
            h("span", { class: "kv-actions" }, h("button", { type: "button", text: "复制", onclick: () => copyText(d.install_command) })))
            : h("p", { class: "note", text: "这份 Violet 不是用安装器装的：请按 Violet MCP 的 DEPLOY_RELAY.md 部署中继。" }),
          h("p", { class: "note", text: "以 root 在服务器上运行这条命令：自动下载中继、校验、注册服务并在本机防火墙放行端口。装好后点上面的“刷新”，这里会列出手机要填的三项。" })));
      return;
    }
    const r = d.relay;
    const address = r.device_url || `wss://${location.hostname}:${r.device_port}${r.device_path || "/device"}`;
    const server = d.server;
    const token = h("input", { type: "password", maxlength: 600, autocomplete: "off", placeholder: server && server.token_set ? "已设置，粘贴新的访问令牌可以替换" : "粘贴手机上复制的访问令牌" });
    const result = h("p", { class: "note" });
    box.replaceChildren(head, intro,
      step(1, "在手机上安装 Violet MCP",
        h("p", { class: "note" }, "从 ", h("a", { href: d.app_url, target: "_blank", rel: "noopener", text: "GitHub 发布页" }),
          " 下载 APK 安装。打开后在“设置 → 权限”里允许“使用情况访问权限”。")),
      step(2, "在手机“设置 → 远程访问”里填这三项",
        valueRow("中继地址", address, { help: r.device_url ? "" : "服务器的公网 IP 没有取到，这里用的是你现在访问后台的地址；手机在外网时要用公网地址。" }),
        valueRow("设备密钥", r.device_secret, { secret: true }),
        r.formal_certificate ? valueRow("证书指纹", "", { help: "中继用的是正式证书，手机上的证书指纹留空。" })
          : valueRow("证书指纹", r.pin, { help: r.pin ? "" : "中继还没有生成证书（第一次启动后才有）：等几秒再点刷新。" }),
        h("p", { class: "note", text: "然后点“保存并应用”，打开“启用中继连接”，回到主页点“启动”。上面的状态变成“手机已连接”就好了。" +
          ` 连不上时检查云服务器安全组是否放行了 TCP ${r.device_port}。` })),
      step(3, "把手机的访问令牌填到这里",
        h("p", { class: "note", text: "手机上打开“安全”页，复制访问令牌，粘贴到下面。Violet 会添加名为 android-phone 的工具服务器，并按模板把这些工具设为只读、交给 Chat 使用。" }),
        h("div", { class: "phone-token" }, token, h("button", { type: "button", text: server ? "替换令牌并检查" : "连接", onclick: async () => {
          result.textContent = "正在连接…";
          try {
            const res = await api("POST", "/api/control/phone-mcp/connect", { token: token.value });
            token.value = "";
            const s = res.server || {};
            result.textContent = s.status === "connected" ? `已连接，手机提供了 ${s.tools} 个工具。` : "已保存，但现在还连不上：确认手机已连接中继后，在下面 android-phone 卡片里点“检查连接”。";
            loaders.tools();
          } catch (e) { result.textContent = "失败：" + e.message; }
        } })), result,
        server ? h("p", { class: "note" }, `android-phone：${server.status === "connected" ? "已连接" : "未连接"}，${server.tools} 个工具。 `,
          h("a", { href: "#tools", text: "查看工具和权限", onclick: (ev) => { ev.preventDefault(); scrollToAnchor("tool-srv-android-phone"); } })) : null));
  }

  async function load() {
    try { render(await api("GET", "/api/control/phone-mcp")); }
    catch (e) { $("#phone-mcp").replaceChildren(h("p", { class: "err", text: "读取失败：" + e.message })); }
  }

  const previous = loaders.tools;
  loaders.tools = async () => { showSecret = false; load(); return previous(); };
})();
