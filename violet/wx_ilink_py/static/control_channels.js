// Violet Control Center: chat channels page (phase 10; Violet 0.6.1: WeChat, QQ and Telegram side by side). Uses
// control.js's helpers ($, h, api, toast, pill, fmt, when, confirmBox, copyText, loaders). Built with DOM nodes and
// textContent only. The server validates everything.
//
// Each platform is one card. A platform not in use shows one button; clicking it opens numbered steps with the link to
// the platform's own page (q.qq.com, @BotFather) and only the fields that step needs. A platform in use shows its
// state, the binding code while no account of yours is bound yet, and its buttons. Anything else (OneBot, a second
// bot, the proactive channel, the change history) is under "更多设置".
"use strict";
(() => {
  const STATE = { connected: ["已连接", "ok"], starting: ["正在连接", "warn"], reconnecting: ["正在重连", "warn"],
    needs_login: ["需要登录", "bad"], error: ["出错", "bad"], stopped: ["未运行", ""], disabled: ["已关闭", ""] };
  const PEER = { bound: "已绑定", pending: "待批准", blocked: "已屏蔽" };
  const FIELD = { api_base: "接口地址", poll_timeout: "长轮询秒数", proxy: "代理（可选）", webhook_loopback_only: "只接受本机上报",
    check_interval: "检查间隔（秒）", bot_token: "Bot Token", access_token: "访问令牌", webhook_secret: "签名密钥",
    app_id: "AppID", app_secret: "AppSecret", sandbox: "沙箱环境（机器人还没审核上线时勾选）" };
  const QQ_URL = "https://q.qq.com/#/";
  const BOTFATHER_URL = "https://t.me/BotFather";
  const PLATFORMS = [
    { key: "wechat", type: "wechat_ilink", name: "微信", intro: "用要给 Violet 用的微信扫一下码就行，不用申请任何东西。" },
    { key: "qq", type: "qqbot", name: "QQ", intro: "在 QQ 开放平台免费创建一个机器人，填两项就能用，不需要 NapCat。" },
    { key: "telegram", type: "telegram", name: "Telegram", intro: "找 @BotFather 创建一个机器人，填一个 Token 就能用。" },
  ];
  let data = null;
  const opened = new Set();             // platforms whose setup steps are open
  const editing = new Set();            // channels whose settings form is open

  const chUrl = (ch, tail = "") => `/api/control/channels/${encodeURIComponent(ch.id)}${tail}`;
  const extLink = (href, text) => h("a", { href, target: "_blank", rel: "noopener", text });
  const statePill = (ch) => { const [t, k] = STATE[ch.status.state] || [ch.status.state, ""]; return pill(t, k); };
  const step = (n, title, ...body) => h("div", { class: "phone-step" }, h("div", { class: "phone-num", text: String(n) }),
    h("div", { class: "phone-body" }, h("strong", { text: title }), ...body));

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
  function secretInput(name, placeholder) {
    return h("label", {}, (FIELD[name] || name) + " ", h("input", { "data-field": name, "data-secret": "1", type: "password",
      autocomplete: "off", maxlength: 512, placeholder }));
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

  // ---------------------------------------------------------------- shared pieces
  async function setEnabled(ch, enabled) {
    try { await api("POST", chUrl(ch), { enabled }); toast(enabled ? "已开启" : "已关闭"); loaders.channels(); }
    catch (e) { toast(e.message, false); }
  }
  async function testChannel(ch) {
    try { const r = await api("POST", chUrl(ch, "/test")); toast(r.ok ? "连接正常" : "连接失败：" + (r.error || r.detail || r.state), !!r.ok); }
    catch (e) { toast(e.message, false); }
  }
  function statusLine(ch) {
    return h("p", { class: "note", text: fmt(ch.status.detail) + (ch.status.last_error ? `（最近错误：${ch.status.last_error}）` : "") +
      ` · 收到 ${ch.status.received || 0} · 发出 ${ch.status.sent || 0}` + (ch.status.send_failures ? ` · 发送失败 ${ch.status.send_failures}` : "") });
  }

  // "/bind <code>" from the user's own account: the last step of QQ / Telegram
  function bindBox(ch, who) {
    const out = h("div", { class: "row" });
    return h("div", { class: "stack" },
      h("p", { class: "note", text: `生成绑定码（10 分钟有效），用${who}把整句发给机器人，Violet 就认得你了。别人发来的消息不会被处理。` }),
      h("div", { class: "row" }, h("button", { type: "button", text: "生成绑定码", onclick: async () => {
        try {
          const r = await api("POST", "/api/control/channels/pairing-code", { channel: ch.id });
          const cmd = `/bind ${r.code}`;
          out.replaceChildren(h("code", { class: "bind-code", text: cmd }),
            h("button", { class: "ghost", type: "button", text: "复制", onclick: () => copyText(cmd) }),
            h("span", { class: "note", text: `${when(r.expires_at)} 前有效` }));
        } catch (e) { toast(e.message, false); }
      } })), out);
  }

  async function loadPeers(ch, box) {
    try {
      const peers = await api("GET", chUrl(ch, "/peers"));
      box.replaceChildren(...(peers.length ? peers.map((p) => h("div", { class: "row" },
        pill(PEER[p.status] || p.status, p.status === "bound" ? "ok" : p.status === "blocked" ? "bad" : "warn"),
        h("span", { text: `${fmt(p.display_name)}（${p.peer_id}）· ${p.message_count} 条 · 最近 ${when(p.last_seen_at)}` }),
        ...["bound", "pending", "blocked"].filter((s) => s !== p.status).map((s) => h("button", { class: "ghost", text: { bound: "绑定为我", pending: "设为待批准", blocked: "屏蔽" }[s],
          onclick: async () => {
            if (s === "bound" && !(await confirmBox(`把 ${p.peer_id} 绑定为你的账号？之后它发来的消息会进入你和 Violet 的对话和记忆。`))) return;
            try { await api("POST", `/api/control/channels/peers/${p.id}`, { status: s }); toast("已保存"); loaders.channels(); } catch (e) { toast(e.message, false); }
          } })))) : [h("p", { class: "note", text: "还没有人在这个渠道上发过消息。" })]));
    } catch (e) { box.replaceChildren(h("p", { class: "err", text: e.message })); }
  }
  function peersDetails(ch) {
    const box = h("div", { class: "stack" });
    return h("details", { ontoggle: (ev) => { if (ev.target.open) loadPeers(ch, box); } },
      h("summary", { text: `联系人（已绑定 ${ch.peers.bound} · 待批准 ${ch.peers.pending} · 已屏蔽 ${ch.peers.blocked}）` }), box);
  }

  // the settings of a channel in use: hidden behind "修改配置"
  function editForm(ch) {
    const fields = h("div", { class: "stack" });
    Object.entries(ch.config || {}).forEach(([k, v]) => fields.append(fieldInput(k, v, false)));
    (ch.secrets || []).forEach((s) => fields.append(h("div", {}, fieldInput(s.name, "", true),
      h("span", { class: "note", text: s.is_set ? ` 已设置（指纹 ${s.fingerprint}）` : " 未设置" }))));
    const err = h("p", { class: "err" });
    return h("div", { class: "stack" }, fields, err, h("div", { class: "row" },
      h("button", { type: "button", text: "保存配置", onclick: async () => {
        err.textContent = "";
        try { await api("POST", chUrl(ch), collect(fields)); editing.delete(ch.id); toast("已保存，渠道已按新配置重启"); loaders.channels(); }
        catch (e) { err.textContent = problemsText(e); }
      } }),
      ch.webhook_path ? h("button", { class: "ghost", type: "button", text: "重新生成签名密钥", onclick: async () => {
        if (!(await confirmBox("旧的签名密钥会立即失效，需要把新密钥填到 OneBot 实现里。继续？"))) return;
        try { const r = await api("POST", chUrl(ch, "/regenerate"), { confirm: true });
          err.textContent = "新的签名密钥（只显示这一次）：" + r.shown_once.webhook_secret; } catch (e) { toast(e.message, false); }
      } }) : null,
      h("button", { class: "danger", type: "button", text: "删除这个渠道", onclick: async () => {
        if (!(await confirmBox(`删除渠道“${ch.label}”？它的凭据会被删除；已经收到的消息和记忆不受影响。`))) return;
        try { await api("POST", chUrl(ch, "/delete"), { confirm: true }); editing.delete(ch.id); toast("已删除"); loaders.channels(); }
        catch (e) { toast(e.message, false); }
      } })));
  }

  // a bot channel in use (QQ, Telegram, and the extra ones under "更多设置")
  function channelBody(ch, who) {
    const toggleEdit = () => { if (editing.has(ch.id)) editing.delete(ch.id); else editing.add(ch.id); loaders.channels(); };
    return [
      statusLine(ch),
      ch.webhook_path ? h("p", { class: "note", text: `上报地址（填到 OneBot 实现里）：${location.origin}${ch.webhook_path}` }) : null,
      ch.enabled && !ch.peers.bound ? step("✓", "最后一步：绑定你的账号", bindBox(ch, who)) : null,
      h("div", { class: "row" },
        h("button", { class: "ghost", type: "button", text: "测试连接", onclick: () => testChannel(ch) }),
        h("button", { class: "ghost", type: "button", text: ch.enabled ? "关闭" : "开启", onclick: () => setEnabled(ch, !ch.enabled) }),
        h("button", { class: "ghost", type: "button", text: editing.has(ch.id) ? "收起配置" : "修改配置", onclick: toggleEdit })),
      editing.has(ch.id) ? editForm(ch) : null,
      peersDetails(ch),
      ch.peers.bound ? h("details", {}, h("summary", { text: "再绑定一个账号" }), bindBox(ch, who)) : null,
    ];
  }

  // ---------------------------------------------------------------- the three platform cards
  function platformCard(p, ch, ...body) {
    return h("div", { class: "card platform", id: "ch-card-" + (ch ? ch.id : p.key) },
      h("div", { class: "platform-head" }, h("strong", { text: p.name }),
        ch && ch.status.state !== "disabled" ? statePill(ch) : pill(ch ? "未使用（已关闭）" : "未使用", "")), ...body);
  }

  function wechatCard(p, ch) {
    return platformCard(p, ch, h("p", { class: "note", text: p.intro }), wechatBox(ch),
      ch.enabled ? h("div", { class: "row" },
        h("button", { class: "ghost", type: "button", text: "测试连接", onclick: () => testChannel(ch) }),
        h("button", { class: "ghost", type: "button", text: "关闭微信", onclick: () => setEnabled(ch, false) })) : null);
  }

  function setupSteps(p) {
    const spec = (data.types || []).find((t) => t.type === p.type) || { defaults: {} };
    const form = h("div", { class: "stack" });
    const err = h("p", { class: "err" });
    let steps;
    if (p.type === "qqbot") {
      steps = [
        step(1, "打开 QQ 开放平台，创建一个机器人", h("p", { class: "note" }, "用手机 QQ 扫码登录 ", extLink(QQ_URL, "q.qq.com"),
          "，在“机器人”里新建一个（个人就可以注册）。")),
        step(2, "在机器人的“开发设置”里复制 AppID 和 AppSecret",
          h("label", {}, "AppID ", h("input", { "data-field": "app_id", maxlength: 32, autocomplete: "off", placeholder: "一串数字" })),
          secretInput("app_secret", "开发设置里的 AppSecret")),
        step(3, "在“沙箱配置”里把你自己的 QQ 加进去", h("p", { class: "note", text: "机器人审核上线之前，只有沙箱里的成员能和它聊天。" }),
          fieldInput("sandbox", !!spec.defaults.sandbox, false)),
      ];
    } else {
      const adv = h("details", {}, h("summary", { text: "高级：代理、接口地址" }), h("div", { class: "stack" },
        fieldInput("proxy", spec.defaults.proxy ?? "", false), fieldInput("api_base", spec.defaults.api_base ?? "", false),
        fieldInput("poll_timeout", spec.defaults.poll_timeout ?? 25, false)));
      steps = [
        step(1, "在 Telegram 里打开 @BotFather", h("p", { class: "note" }, "点 ", extLink(BOTFATHER_URL, "t.me/BotFather"),
          " 打开，发送 /newbot，按提示给机器人起名字和用户名。")),
        step(2, "把 BotFather 回复的 Token 填到这里", secretInput("bot_token", "形如 123456789:AAE…"),
          h("p", { class: "note", text: "服务器在中国大陆时连不上 Telegram，需要在下面的“高级”里填代理。" }), adv),
      ];
    }
    form.append(...steps);
    return h("div", { class: "stack" }, form, err, h("div", { class: "row" },
      h("button", { type: "button", text: "保存并连接", onclick: async () => {
        err.textContent = "";
        const { config, secrets } = collect(form);
        const ids = new Set(data.channels.map((c) => c.id));
        const id = [p.key, p.key + "bot", ...[2, 3, 4, 5, 6, 7, 8, 9].map((n) => p.key + n)].find((x) => !ids.has(x));
        try {
          await api("POST", "/api/control/channels", { type: p.type, id, label: p.name, config, secrets });
          opened.delete(p.key); toast(`${p.name} 已保存，正在连接`); loaders.channels();
        } catch (e) { err.textContent = problemsText(e); }
      } }),
      h("button", { class: "ghost", type: "button", text: "取消", onclick: () => { opened.delete(p.key); loaders.channels(); } })),
      p.type === "qqbot" ? h("p", { class: "note", text: "注意：QQ 只允许机器人在你发消息后的一段时间内回复，Violet 主动发的消息可能被 QQ 拒绝。" }) : null);
  }

  function botCard(p, ch) {
    if (ch) return platformCard(p, ch, ...channelBody(ch, p.type === "qqbot" ? "你的 QQ" : "你自己的 Telegram 账号"));
    if (!opened.has(p.key)) {
      return platformCard(p, null, h("p", { class: "note", text: p.intro }),
        h("div", { class: "row" }, h("button", { type: "button", text: `使用 ${p.name}`, onclick: () => { opened.add(p.key); loaders.channels(); } })));
    }
    return platformCard(p, null, setupSteps(p));
  }

  // ---------------------------------------------------------------- WeChat QR login (Violet stage 3; was the old page)
  let wxTimer = null;
  function wechatBox(ch) {
    const box = h("div", { class: "wx-login", id: "wx-login" });
    loadWechat(box, ch);
    return box;
  }
  function scheduleWechat(box, ch, ms) {
    clearTimeout(wxTimer);
    wxTimer = setTimeout(() => {
      if (box.isConnected && $("#tab-channels").classList.contains("active") && !document.hidden) loadWechat(box, ch);
      else if (box.isConnected) scheduleWechat(box, ch, 3000);
    }, ms);
  }
  async function loadWechat(box, ch) {
    let w;
    try { w = await api("GET", "/api/control/wechat"); } catch (e) { box.replaceChildren(h("p", { class: "err", text: e.message })); return; }
    if (w.logged_in) {
      const wasLoggingIn = box.dataset.loggingIn === "1";
      box.dataset.loggingIn = ""; box.dataset.qr = "";
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
      const status = [pill(w.qr.status_text, w.qr.status === "scaned" ? "ok" : "warn"),
        h("p", { class: "note", text: "用要给 Violet 使用的微信扫码，并在手机上确认。二维码过期会自动换新" +
          (w.qr.refreshes ? `（已换 ${w.qr.refreshes} 次）` : "") + "。" })];
      const key = w.qr.image || w.qr.url || "";
      const slot = box.querySelector(".wx-status");
      if (slot && box.dataset.qr === key) {
        slot.replaceChildren(...status);        // same code: only the words change, the picture stays (no flicker)
      } else {
        box.dataset.qr = key;
        const img = w.qr.image ? h("img", { class: "wx-qr", src: w.qr.image, alt: "微信登录二维码" }) : h("div", { class: "wx-qr wx-qr-wait" });
        box.replaceChildren(h("div", { class: "wx-qr-wrap" }, img,
          h("div", { class: "stack" }, h("strong", { text: "用微信扫一扫登录" }), h("div", { class: "stack wx-status" }, ...status),
            w.qr.url ? h("a", { href: w.qr.url, target: "_blank", rel: "noopener", text: "二维码显示不出来？打开登录链接" }) : null)));
      }
      scheduleWechat(box, ch, 2000);
      return;
    }
    box.dataset.loggingIn = ""; box.dataset.qr = "";
    box.replaceChildren(h("div", { class: "row" }, pill("微信未登录", "warn"),
      w.error ? h("span", { class: "note", text: `上次失败：${w.error}` }) : null,
      h("button", { text: "扫码登录", onclick: async () => {
        try {
          if (ch && !ch.enabled) await api("POST", chUrl(ch), { enabled: true });       // scanning means "use WeChat"
          await api("POST", "/api/control/wechat/login", {}); box.dataset.loggingIn = "1"; scheduleWechat(box, ch, 800); toast("正在获取二维码…");
        } catch (e) { toast(e.message, false); }
      } })));
  }

  // ---------------------------------------------------------------- "更多设置": other channels, the add form, history
  function extraCard(ch) {
    return h("div", { class: "card platform", id: "ch-card-" + ch.id },
      h("div", { class: "platform-head" }, h("strong", { text: ch.label }), h("span", { class: "note", text: `${ch.id} · ${ch.type}` }), statePill(ch)),
      ...channelBody(ch, "你自己的账号"));
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
  }

  loaders.channels = async () => {
    try { data = await api("GET", "/api/control/channels"); } catch (e) { toast(e.message, false); return; }
    const used = new Set();
    const pick = (p) => { const ch = data.channels.find((c) => c.type === p.type && !used.has(c.id) && (p.key !== "wechat" || c.id === "wechat"));
      if (ch) used.add(ch.id); return ch; };
    $("#ch-list").replaceChildren(...PLATFORMS.map((p) => { const ch = pick(p); return p.key === "wechat" ? wechatCard(p, ch || { id: "wechat", enabled: true, status: { state: "stopped" } }) : botCard(p, ch); }));
    const extra = data.channels.filter((c) => !used.has(c.id));
    $("#ch-extra").replaceChildren(...extra.map(extraCard));
    if (extra.length) $("#ch-more").open = true;
    const sel = $("#ch-proactive");
    sel.replaceChildren(h("option", { value: "auto", text: "自动（你最近聊天的渠道）" }),
      ...data.channels.map((c) => h("option", { value: c.id, text: c.label })));
    sel.value = data.proactive_channel || "auto";
    const typeSel = $("#ch-add-type");
    if (!typeSel.options.length) {
      typeSel.replaceChildren(...data.types.map((t) => h("option", { value: t.type, text: t.label })));
      typeSel.value = data.types.some((t) => t.type === "onebot11") ? "onebot11" : typeSel.value;
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
})();
