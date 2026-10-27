// Violet Control Center: chat channels page (phase 10). Uses control.js's helpers ($, h, api, toast, pill, fmt, when,
// confirmBox, loaders). Built with DOM nodes and textContent only. The server validates everything.
"use strict";
(() => {
  const STATE = { connected: ["已连接", "ok"], starting: ["正在连接", "warn"], reconnecting: ["正在重连", "warn"],
    needs_login: ["需要登录", "bad"], error: ["出错", "bad"], stopped: ["未运行", ""], disabled: ["已关闭", ""] };
  const PEER = { bound: "已绑定", pending: "待批准", blocked: "已屏蔽" };
  const FIELD = { api_base: "接口地址", poll_timeout: "长轮询秒数", proxy: "代理（可选）", webhook_loopback_only: "只接受本机上报",
    check_interval: "检查间隔（秒）", bot_token: "Bot Token", access_token: "访问令牌", webhook_secret: "签名密钥",
    app_id: "AppID", app_secret: "AppSecret", sandbox: "沙箱环境（机器人还没上线时）" };
  let data = null;

  function fieldInput(name, value, secret) {
    if (typeof value === "boolean") {
      const box = h("input", { type: "checkbox", "data-field": name }); box.checked = value;
      return h("label", { class: "check" }, box, " " + (FIELD[name] || name));
    }
    const input = h("input", { "data-field": name, "data-secret": secret ? "1" : null, type: secret ? "password" : (typeof value === "number" ? "number" : "text"),
      autocomplete: "off", maxlength: 512, placeholder: secret ? "留空 = 不修改" : "" });
    if (!secret) input.value = value ?? "";
    return h("label", {}, (FIELD[name] || name) + " ", input);
  }
  function collect(root) {
    const config = {}, secrets = {};
    root.querySelectorAll("[data-field]").forEach((el) => {
      const name = el.dataset.field;
      if (el.dataset.secret) { if (el.value.trim()) secrets[name] = el.value.trim(); return; }
      if (el.type === "checkbox") config[name] = el.checked;
      else if (el.type === "number") config[name] = el.value === "" ? null : Number(el.value);
      else config[name] = el.value.trim();
    });
    return { config, secrets };
  }
  function problemsText(e) {
    const p = e.data && e.data.problems;
    return p ? Object.entries(p).map(([k, v]) => `${FIELD[k.replace("secret:", "")] || k}：${v}`).join("；") : e.message;
  }

  async function loadPeers(ch, box) {
    try {
      const peers = await api("GET", `/api/control/channels/${encodeURIComponent(ch.id)}/peers`);
      box.replaceChildren(...(peers.length ? peers.map((p) => h("div", { class: "row" },
        pill(PEER[p.status] || p.status, p.status === "bound" ? "ok" : p.status === "blocked" ? "bad" : "warn"),
        h("span", { text: `${fmt(p.display_name)}（${p.peer_id}）· ${p.message_count} 条 · 最近 ${when(p.last_seen_at)}` }),
        ...["bound", "pending", "blocked"].filter((s) => s !== p.status).map((s) => h("button", { class: "ghost", text: { bound: "绑定为我", pending: "设为待批准", blocked: "屏蔽" }[s],
          onclick: async () => {
            if (s === "bound" && !(await confirmBox(`把 ${p.peer_id} 绑定为你的账号？之后它发来的消息会进入你和 Violet 的对话和记忆。`))) return;
            try { await api("POST", `/api/control/channels/peers/${p.id}`, { status: s }); toast("已保存"); loadPeers(ch, box); } catch (e) { toast(e.message, false); }
          } })))) : [h("p", { class: "note", text: "还没有人在这个渠道上发过消息。" })]));
    } catch (e) { box.replaceChildren(h("p", { class: "err", text: e.message })); }
  }

  function channelCard(ch) {
    const [stateText, stateKind] = STATE[ch.status.state] || [ch.status.state, ""];
    const body = h("div", { class: "stack" });
    const fields = h("div", { class: "stack" });
    if (ch.type !== "wechat_ilink") {
      Object.entries(ch.config || {}).forEach(([k, v]) => fields.append(fieldInput(k, v, false)));
      (ch.secrets || []).forEach((s) => fields.append(h("div", {}, fieldInput(s.name, "", true),
        h("span", { class: "note", text: s.is_set ? ` 已设置（指纹 ${s.fingerprint}）` : " 未设置" }))));
    }
    const err = h("p", { class: "err" });
    const peers = h("div", { class: "stack" });
    const append = (...nodes) => body.append(...nodes.filter((n) => n !== null && n !== undefined));
    append(
      h("div", { class: "row" }, h("strong", { text: ch.label }), pill(stateText, stateKind), h("span", { class: "note", text: `${ch.id} · ${ch.type}` })),
      h("p", { class: "note", text: fmt(ch.status.detail) + (ch.status.last_error ? `（最近错误：${ch.status.last_error}）` : "") +
        ` · 收到 ${ch.status.received || 0} · 发出 ${ch.status.sent || 0} · 发送失败 ${ch.status.send_failures || 0}` }),
      ch.webhook_path ? h("p", { class: "note", text: `上报地址（填到 OneBot 实现里）：${location.origin}${ch.webhook_path}` }) : null,
      ch.type === "wechat_ilink" ? wechatBox() : null,
      fields, err,
      h("div", { class: "row" },
        h("button", { class: "ghost", text: ch.enabled ? "关闭" : "开启", onclick: async () => {
          try { await api("POST", `/api/control/channels/${encodeURIComponent(ch.id)}`, { enabled: !ch.enabled }); toast(ch.enabled ? "已关闭" : "已开启"); loaders.channels(); } catch (e) { toast(e.message, false); } } }),
        ch.type !== "wechat_ilink" ? h("button", { text: "保存配置", onclick: async () => {
          err.textContent = "";
          try { await api("POST", `/api/control/channels/${encodeURIComponent(ch.id)}`, collect(fields)); toast("已保存，渠道已按新配置重启"); loaders.channels(); } catch (e) { err.textContent = problemsText(e); } } }) : null,
        h("button", { class: "ghost", text: "测试连接", onclick: async () => {
          try { const r = await api("POST", `/api/control/channels/${encodeURIComponent(ch.id)}/test`); toast(r.ok ? "连接正常" : "连接失败：" + (r.error || r.detail || r.state), !!r.ok); } catch (e) { toast(e.message, false); } } }),
        ch.webhook_path ? h("button", { class: "ghost", text: "重新生成签名密钥", onclick: async () => {
          if (!(await confirmBox("旧的签名密钥会立即失效，需要把新密钥填到 OneBot 实现里。继续？"))) return;
          try { const r = await api("POST", `/api/control/channels/${encodeURIComponent(ch.id)}/regenerate`, { confirm: true });
            err.textContent = "新的签名密钥（只显示这一次）：" + r.shown_once.webhook_secret; } catch (e) { toast(e.message, false); } } }) : null,
        ch.id !== "wechat" ? h("button", { class: "danger", text: "删除", onclick: async () => {
          if (!(await confirmBox(`删除渠道“${ch.label}”？它的凭据会被删除；已经收到的消息和记忆不受影响。`))) return;
          try { await api("POST", `/api/control/channels/${encodeURIComponent(ch.id)}/delete`, { confirm: true }); toast("已删除"); loaders.channels(); } catch (e) { toast(e.message, false); } } }) : null),
      ch.id !== "wechat" ? h("details", { ontoggle: (ev) => { if (ev.target.open) loadPeers(ch, peers); } },
        h("summary", { text: `联系人（已绑定 ${ch.peers.bound} · 待批准 ${ch.peers.pending} · 已屏蔽 ${ch.peers.blocked}）` }), peers) : null);
    return h("div", { class: "card" }, body);
  }

  // ---------------------------------------------------------------- WeChat QR login (Violet stage 3; was the old page)
  let wxTimer = null;
  function wechatBox() {
    const box = h("div", { class: "wx-login", id: "wx-login" });
    loadWechat(box);
    return box;
  }
  function scheduleWechat(box, ms) {
    clearTimeout(wxTimer);
    wxTimer = setTimeout(() => {
      if (box.isConnected && $("#tab-channels").classList.contains("active") && !document.hidden) loadWechat(box);
      else if (box.isConnected) scheduleWechat(box, 3000);
    }, ms);
  }
  async function loadWechat(box) {
    let w;
    try { w = await api("GET", "/api/control/wechat"); } catch (e) { box.replaceChildren(h("p", { class: "err", text: e.message })); return; }
    if (w.logged_in) {
      const wasLoggingIn = box.dataset.loggingIn === "1";
      box.dataset.loggingIn = "";
      box.replaceChildren(h("div", { class: "row" }, pill("微信已登录", "ok"), h("span", { class: "note", text: `账号 ${w.account}` }),
        h("button", { class: "ghost", text: "退出微信登录", onclick: async () => {
          if (!(await confirmBox("退出微信登录？Violet 会停止收发微信消息，之后要重新扫码。"))) return;
          try { await api("POST", "/api/control/wechat/logout", { confirm: true }); toast("已退出微信登录"); loaders.channels(); }
          catch (e) { toast(e.message, false); }
        } })));
      if (wasLoggingIn) { toast("微信登录成功"); loaders.channels(); }
      return;
    }
    if (w.logging_in && w.qr) {
      box.dataset.loggingIn = "1";
      const img = w.qr.image ? h("img", { class: "wx-qr", src: w.qr.image, alt: "微信登录二维码" }) : h("div", { class: "wx-qr wx-qr-wait" });
      box.replaceChildren(h("div", { class: "wx-qr-wrap" }, img,
        h("div", { class: "stack" }, h("strong", { text: "用微信扫一扫登录" }),
          pill(w.qr.status_text, w.qr.status === "scaned" ? "ok" : "warn"),
          h("p", { class: "note", text: "用要给 Violet 使用的微信扫码，并在手机上确认。二维码过期会自动换新" +
            (w.qr.refreshes ? `（已换 ${w.qr.refreshes} 次）` : "") + "。" }),
          w.qr.url ? h("a", { href: w.qr.url, target: "_blank", rel: "noopener", text: "二维码显示不出来？打开登录链接" }) : null)));
      scheduleWechat(box, 2000);
      return;
    }
    box.dataset.loggingIn = "";
    box.replaceChildren(h("div", { class: "row" }, pill("微信未登录", "warn"),
      w.error ? h("span", { class: "note", text: `上次失败：${w.error}` }) : h("span", { class: "note", text: "扫码后 Violet 就能收发微信消息。" }),
      h("button", { text: "扫码登录", onclick: async () => {
        try { await api("POST", "/api/control/wechat/login", {}); box.dataset.loggingIn = "1"; scheduleWechat(box, 800); toast("正在获取二维码…"); }
        catch (e) { toast(e.message, false); }
      } })));
  }

  function renderAddForm() {
    const type = $("#ch-add-type").value;
    const spec = (data.types || []).find((t) => t.type === type);
    if (!spec) return;
    $("#ch-add-help").textContent = spec.help;
    $("#ch-add-fields").replaceChildren(
      ...Object.entries(spec.defaults).map(([k, v]) => fieldInput(k, v, false)),
      ...spec.secrets.map((s) => h("div", {}, fieldInput(s.name, "", true), h("span", { class: "note",
        text: s.name === "webhook_secret" ? " 留空会自动生成（只显示一次）" : (s.required ? " 必填" : " 可选") }))));
    if (!$("#ch-add-id").value) $("#ch-add-id").placeholder = "例如 " + type.replace("11", "");
  }

  loaders.channels = async () => {
    try { data = await api("GET", "/api/control/channels"); } catch (e) { toast(e.message, false); return; }
    $("#ch-list").replaceChildren(...data.channels.map(channelCard));
    const sel = $("#ch-proactive");
    sel.replaceChildren(h("option", { value: "auto", text: "自动（你最近聊天的渠道）" }),
      ...data.channels.map((c) => h("option", { value: c.id, text: c.label })));
    sel.value = data.proactive_channel || "auto";
    const pair = $("#ch-pair-channel");
    pair.replaceChildren(h("option", { value: "", text: "任意渠道" }),
      ...data.channels.filter((c) => c.id !== "wechat").map((c) => h("option", { value: c.id, text: c.label })));
    const typeSel = $("#ch-add-type");
    if (!typeSel.options.length) {
      typeSel.replaceChildren(...data.types.map((t) => h("option", { value: t.type, text: t.label })));
    }
    renderAddForm();
    $("#ch-history").replaceChildren(...(data.history || []).map((r) => h("div", { class: "card" },
      h("span", { text: `${when(new Date(r.at * 1000).toISOString())} · ${r.actor} · ${r.action} · ${r.channel}` }))));
  };

  $("#ch-refresh").addEventListener("click", () => loaders.channels());
  $("#ch-add-type").addEventListener("change", renderAddForm);
  $("#ch-proactive").addEventListener("change", async (ev) => {
    try { await api("POST", "/api/control/channels/proactive", { channel: ev.target.value }); toast("已保存"); } catch (e) { toast(e.message, false); }
  });
  $("#ch-add").addEventListener("submit", async (ev) => {
    ev.preventDefault(); $("#ch-add-error").textContent = "";
    const { config, secrets } = collect($("#ch-add-fields"));
    try {
      const r = await api("POST", "/api/control/channels", { type: $("#ch-add-type").value, id: $("#ch-add-id").value.trim(),
        label: $("#ch-add-label").value.trim(), config, secrets });
      ev.target.reset();
      if (r.shown_once && r.shown_once.webhook_secret) $("#ch-add-error").textContent = "已添加。签名密钥（只显示这一次，请填到 OneBot 实现里）：" + r.shown_once.webhook_secret;
      else toast("已添加并启动");
      loaders.channels();
    } catch (e) { $("#ch-add-error").textContent = problemsText(e); }
  });
  $("#ch-pair").addEventListener("click", async () => {
    try { const r = await api("POST", "/api/control/channels/pairing-code", { channel: $("#ch-pair-channel").value || null });
      $("#ch-pair-code").textContent = `/bind ${r.code}（${when(r.expires_at)} 前有效）`; } catch (e) { toast(e.message, false); }
  });
})();
