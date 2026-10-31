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
  const PEER = { bound: "主人", admin: "管理员", pending: "待批准", blocked: "已屏蔽" };
  const FIELD = { api_base: "接口地址", poll_timeout: "长轮询秒数", proxy: "代理（可选）", webhook_loopback_only: "只接受本机上报",
    check_interval: "检查间隔（秒）", bot_token: "Bot Token", access_token: "访问令牌", webhook_secret: "签名密钥",
    app_id: "AppID", app_secret: "AppSecret", sandbox: "沙箱环境（机器人还没审核上线时勾选）" };
  const QQ_URL = "https://q.qq.com/#/";
  const BOTFATHER_URL = "https://t.me/BotFather";
  const PLATFORMS = [
    { key: "wechat", type: "wechat_ilink", name: "微信", intro: "用要给 Violet 用的微信扫一下码就行，不用申请任何东西。" },
    { key: "napcat", type: "napcat", name: "QQ", intro: "用一个 QQ 号（建议小号）扫码登录。私聊和你聊天，还能进群、认识别人。在服务器上一键安装（SnowLuma），不用自己装。" },
    { key: "qq", type: "qqbot", name: "QQ（机器人）", intro: "在 QQ 开放平台免费创建一个官方机器人，填两项就能用。只能私聊，不会被封号。" },
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
    loaders.channels();                       // 0.7.2: the card showed "正在连接" until the page was opened again
  }
  // while a channel is still connecting, look again every few seconds (not while a form is open: it would be redrawn)
  let pending = null, pendingTries = 0;
  function watchConnecting(channels) {
    clearTimeout(pending);
    const busy = channels.some((c) => ["starting", "reconnecting"].includes(c.status.state));
    if (!busy) { pendingTries = 0; return; }
    if (pendingTries++ > 40 || opened.size || editing.size) return;
    pending = setTimeout(() => {
      if ($("#tab-channels").classList.contains("active") && !document.hidden) loaders.channels();
    }, 3000);
  }
  function statusLine(ch) {
    // an error from before the connection worked (e.g. QQ not logged in yet) is history once it is connected
    const error = ch.status.state !== "connected" && ch.status.last_error;
    return h("p", { class: "note", text: fmt(ch.status.detail) + (error ? `（最近错误：${error}）` : "") +
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
        // 2026-10-30: an admin is someone else the owner trusts (QQ only): own chat window, a few requests in groups
        ...["bound", ...(ch.type === "napcat" ? ["admin"] : []), "pending", "blocked"].filter((s) => s !== p.status).map((s) => h("button", { class: "ghost", text: { bound: "绑定为我", admin: "设为管理员", pending: "设为待批准", blocked: "屏蔽" }[s],
          onclick: async () => {
            if (s === "bound" && !(await confirmBox(`把 ${p.peer_id} 绑定为你的账号？之后它发来的消息会进入你和 Violet 的对话和记忆。`))) return;
            if (s === "admin" && !(await confirmBox(`把 ${p.peer_id} 设为管理员？管理员不是你：他私聊她时是他自己的聊天窗口，不共用你的记忆；在群里可以让她说话、回应、安静。他之前在你的聊天里说的话会搬到他自己的窗口。`))) return;
            try { await api("POST", `/api/control/channels/peers/${p.id}`, { status: s }); toast("已保存"); loaders.channels(); } catch (e) { toast(e.message, false); }
          } })))) : [h("p", { class: "note", text: "还没有人在这个渠道上发过消息。" })]));
    } catch (e) { box.replaceChildren(h("p", { class: "err", text: e.message })); }
  }
  function peersDetails(ch) {
    const box = h("div", { class: "stack" });
    return h("details", { ontoggle: (ev) => { if (ev.target.open) loadPeers(ch, box); } },
      h("summary", { text: `联系人（主人 ${ch.peers.bound}` + (ch.peers.admin ? ` · 管理员 ${ch.peers.admin}` : "") + ` · 待批准 ${ch.peers.pending} · 已屏蔽 ${ch.peers.blocked}）` }), box);
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

  // a bot channel in use (QQ, Telegram, and the extra ones under "更多设置"): {body, foot}
  function channelParts(ch, who) {
    const toggleEdit = () => { if (editing.has(ch.id)) editing.delete(ch.id); else editing.add(ch.id); loaders.channels(); };
    return {
      body: [
        statusLine(ch),
        ch.webhook_path ? h("p", { class: "note", text: `上报地址（填到 OneBot 实现里）：${location.origin}${ch.webhook_path}` }) : null,
        ch.enabled && !ch.peers.bound ? step("✓", "最后一步：绑定你的账号", bindBox(ch, who)) : null,
        editing.has(ch.id) ? editForm(ch) : null,
        peersDetails(ch),
        ch.peers.bound ? h("details", {}, h("summary", { text: "再绑定一个账号" }), bindBox(ch, who)) : null,
      ],
      foot: [
        h("button", { class: "ghost", type: "button", text: "测试连接", onclick: () => testChannel(ch) }),
        h("button", { class: "ghost", type: "button", text: ch.enabled ? "关闭" : "开启", onclick: () => setEnabled(ch, !ch.enabled) }),
        h("button", { class: "ghost", type: "button", text: editing.has(ch.id) ? "收起配置" : "修改配置", onclick: toggleEdit }),
      ],
    };
  }

  // ---------------------------------------------------------------- the three platform cards
  // 0.7.1 (user: "没有对齐一高一低的，这三个工具是平级的不要更优待微信"): every card has the same parts -- name and
  // state, one line of introduction, a body that fills the card, and the buttons in a row at the bottom; the cards in
  // a row are equally tall. A platform not in use shows one button "使用 X" in every case.
  function platformCard(p, ch, title, body, foot) {
    // WeChat waiting for a scan counts as "not used" until loadWechat knows better (it sets the state itself)
    const used = ch && ch.status.state !== "disabled" && !(p.key === "wechat" && ch.status.state === "needs_login");
    return h("div", { class: "card platform", id: "ch-card-" + (ch ? ch.id : p.key) },
      h("div", { class: "platform-head" }, h("strong", { text: title || p.name }), used ? statePill(ch) : pill("未使用", "")),
      p.intro ? h("p", { class: "note platform-intro", text: p.intro }) : null,
      h("div", { class: "platform-body" }, ...body),
      h("div", { class: "row platform-foot" }, ...foot));
  }

  function wechatCard(p, ch) {
    const body = h("div", { class: "stack" });
    const foot = h("div", { class: "row" });
    const card = platformCard(p, ch, null, [body], [foot]);
    loadWechat(body, foot, ch, card);
    return card;
  }

  function setupSteps(p) {
    const spec = (data.types || []).find((t) => t.type === p.type) || { defaults: {} };
    const form = h("div", { class: "stack" });
    const err = h("p", { class: "err" });
    if (p.type === "qqbot") {
      form.append(
        step(1, "打开 QQ 开放平台，创建一个机器人", h("p", { class: "note" }, "用手机 QQ 扫码登录 ", extLink(QQ_URL, "q.qq.com"),
          "，在“机器人”里新建一个（个人就可以注册）。")),
        step(2, "在机器人的“开发设置”里复制 AppID 和 AppSecret",
          h("label", {}, "AppID ", h("input", { "data-field": "app_id", maxlength: 32, autocomplete: "off", placeholder: "一串数字" })),
          secretInput("app_secret", "开发设置里的 AppSecret")),
        step(3, "在“沙箱配置”里把你自己的 QQ 加进去", h("p", { class: "note", text: "机器人审核上线之前，只有沙箱里的成员能和它聊天。" }),
          fieldInput("sandbox", !!spec.defaults.sandbox, false)),
        h("p", { class: "note", text: "注意：QQ 只允许机器人在你发消息后的一段时间内回复，Violet 主动发的消息可能被 QQ 拒绝。" }));
    } else {
      const adv = h("details", {}, h("summary", { text: "高级：代理、接口地址" }), h("div", { class: "stack" },
        fieldInput("proxy", spec.defaults.proxy ?? "", false), fieldInput("api_base", spec.defaults.api_base ?? "", false),
        fieldInput("poll_timeout", spec.defaults.poll_timeout ?? 25, false)));
      form.append(
        step(1, "在 Telegram 里打开 @BotFather", h("p", { class: "note" }, "点 ", extLink(BOTFATHER_URL, "t.me/BotFather"),
          " 打开，发送 /newbot，按提示给机器人起名字和用户名。")),
        step(2, "把 BotFather 回复的 Token 填到这里", secretInput("bot_token", "形如 123456789:AAE…"),
          h("p", { class: "note", text: "服务器在中国大陆时连不上 Telegram，需要在下面的“高级”里填代理。" }), adv));
    }
    return {
      body: [form, err],
      foot: [
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
        h("button", { class: "ghost", type: "button", text: "取消", onclick: () => { opened.delete(p.key); loaders.channels(); } }),
      ],
    };
  }

  function botCard(p, ch) {
    if (ch) {
      const parts = channelParts(ch, p.type === "qqbot" ? "你的 QQ" : "你自己的 Telegram 账号");
      return platformCard({ ...p, intro: "" }, ch, null, parts.body, parts.foot);
    }
    if (!opened.has(p.key)) {
      return platformCard(p, null, null, [],
        [h("button", { type: "button", text: `使用 ${p.name}`, onclick: () => { opened.add(p.key); loaders.channels(); } })]);
    }
    const parts = setupSteps(p);
    return platformCard({ ...p, intro: "" }, null, null, parts.body, parts.foot);
  }

  // ---------------------------------------------------------------- WeChat QR login (Violet stage 3; was the old page)
  let wxTimer = null;
  function scheduleWechat(body, foot, ch, card, ms) {
    clearTimeout(wxTimer);
    wxTimer = setTimeout(() => {
      if (body.isConnected && $("#tab-channels").classList.contains("active") && !document.hidden) loadWechat(body, foot, ch, card);
      else if (body.isConnected) scheduleWechat(body, foot, ch, card, 3000);
    }, ms);
  }
  function setHead(card, text, kind) {
    const old = card.querySelector(".platform-head .pill");
    if (old) old.replaceWith(pill(text, kind));
  }
  function setIntro(card, show) {
    const intro = card.querySelector(".platform-intro");
    if (intro) intro.hidden = !show;
  }
  async function loadWechat(body, foot, ch, card) {
    let w;
    try { w = await api("GET", "/api/control/wechat"); } catch (e) { body.replaceChildren(h("p", { class: "err", text: e.message })); return; }
    if (w.logged_in) {
      const wasLoggingIn = body.dataset.loggingIn === "1";
      body.dataset.loggingIn = ""; body.dataset.qr = "";
      setIntro(card, false);
      if (!ch.enabled) setHead(card, "未使用", "");
      body.replaceChildren(statusLine(ch), h("p", { class: "note", text: `已登录的微信：${w.account}` }));
      foot.replaceChildren(
        h("button", { class: "ghost", type: "button", text: "测试连接", onclick: () => testChannel(ch) }),
        h("button", { class: "ghost", type: "button", text: ch.enabled ? "关闭" : "开启", onclick: () => setEnabled(ch, !ch.enabled) }),
        h("button", { class: "ghost", type: "button", text: "退出登录", onclick: async () => {
          if (!(await confirmBox("退出微信登录？Violet 会停止收发微信消息，之后要重新扫码。"))) return;
          try { await api("POST", "/api/control/wechat/logout", { confirm: true }); toast("已退出微信登录"); loaders.channels(); }
          catch (e) { toast(e.message, false); }
        } }));
      if (wasLoggingIn) { toast("微信登录成功"); loaders.channels(); }
      return;
    }
    if (w.logging_in && w.qr) {
      body.dataset.loggingIn = "1";
      setIntro(card, false);
      setHead(card, w.qr.status === "scaned" ? "已扫码" : "等待扫码", "warn");
      const status = [pill(w.qr.status_text, w.qr.status === "scaned" ? "ok" : "warn"),
        h("p", { class: "note", text: "用要给 Violet 使用的微信扫码，并在手机上确认。二维码过期会自动换新" +
          (w.qr.refreshes ? `（已换 ${w.qr.refreshes} 次）` : "") + "。" })];
      const key = w.qr.image || w.qr.url || "";
      const slot = body.querySelector(".wx-status");
      if (slot && body.dataset.qr === key) {
        slot.replaceChildren(...status);        // same code: only the words change, the picture stays (no flicker)
      } else {
        body.dataset.qr = key;
        const img = w.qr.image ? h("img", { class: "wx-qr", src: w.qr.image, alt: "微信登录二维码" }) : h("div", { class: "wx-qr wx-qr-wait" });
        body.replaceChildren(step(1, "用微信扫一扫", img, h("div", { class: "stack wx-status" }, ...status)));
        foot.replaceChildren(w.qr.url ? h("a", { href: w.qr.url, target: "_blank", rel: "noopener", text: "二维码显示不出来？打开登录链接" }) : "");
      }
      scheduleWechat(body, foot, ch, card, 2000);
      return;
    }
    body.dataset.loggingIn = ""; body.dataset.qr = "";
    setIntro(card, true);
    setHead(card, ch.enabled && ch.status.state !== "needs_login" ? "未登录" : "未使用", "");
    body.replaceChildren(...(w.error ? [h("p", { class: "err", text: `上次登录失败：${w.error}` })] : []));
    foot.replaceChildren(h("button", { type: "button", text: "使用 微信", onclick: async () => {
      try {
        if (!ch.enabled) await api("POST", chUrl(ch), { enabled: true });       // scanning means "use WeChat"
        await api("POST", "/api/control/wechat/login", {}); body.dataset.loggingIn = "1";
        body.replaceChildren(h("p", { class: "note", text: "正在获取二维码…" }));
        scheduleWechat(body, foot, ch, card, 800);
      } catch (e) { toast(e.message, false); }
    } }));
  }

  // ---------------------------------------------------------------- 2026-10-29: "QQ" = a personal account via SnowLuma
  // Not installed: one-click install (root task, progress dialog). Installed: QQ's own window as a picture (SnowLuma
  // has no QR code API: the code is scanned in that window; a click on the picture clicks in QQ), then the usual
  // channel parts; the groups and other people are managed below the cards (control_social.js).
  let qqTimer = null;
  let qqView = null;
  function qqCard(p, ch) {
    const body = h("div", { class: "stack" });
    const foot = h("div", { class: "row" });
    const card = platformCard(p, ch, null, [body], [foot]);
    loadQQ(body, foot, ch, card);
    return card;
  }
  function watchQQ(title) {
    progressDialog({ title, url: "/api/control/progress/task", onFinish: (pr, extra) => {
      if (pr.notes && pr.notes.length) extra.replaceChildren(h("ul", { class: "note" }, ...pr.notes.map((n) => h("li", { text: n }))));
      loaders.channels();
    } });
  }
  async function qqTask(action, title) {
    try { await api("POST", `/api/control/qq/${action}`, action === "uninstall" ? { confirm: true } : {}); }
    catch (e) { toast(e.message, false); return; }
    watchQQ(title);
  }
  // QQ's window on the server. In the card: a preview that always fits (scan the QR code there; a click enlarges it).
  // Enlarged (a dialog): the picture as large as the screen allows, refreshed every 3 s; a click on it is a click in
  // QQ's window (the server maps it back to screen coordinates).
  async function qqShot(full) {
    return api("GET", "/api/control/qq/screen" + (full ? "?full=1" : ""));
  }
  function qqWindowDialog() {
    const img = h("img", { class: "qq-live", alt: "QQ 窗口" });
    const note = h("p", { class: "note", text: "正在读取 QQ 窗口…" });
    const whole = h("input", { type: "checkbox" });
    let shot = null, timer = null;
    async function refresh() {
      clearTimeout(timer);
      try {
        shot = await qqShot(whole.checked);
        img.src = shot.image;
        note.textContent = "点画面就等于在 QQ 窗口里点一下。画面每 3 秒刷新。";
      } catch (e) { note.textContent = e.message; }
      if (d.open) timer = setTimeout(refresh, 3000);
    }
    img.addEventListener("click", async (ev) => {
      if (!shot) return;
      const r = img.getBoundingClientRect();
      const x = Math.round(shot.x + (ev.clientX - r.left) * shot.width / r.width);
      const y = Math.round(shot.y + (ev.clientY - r.top) * shot.height / r.height);
      try { await api("POST", "/api/control/qq/click", { x, y }); setTimeout(refresh, 600); } catch (e) { toast(e.message, false); }
    });
    whole.addEventListener("change", () => refresh());
    const d = h("dialog", { class: "qq-window-dialog" }, h("div", { class: "stack" },
      h("div", { class: "row" }, h("h3", { text: "服务器上的 QQ 窗口" }), h("span", { class: "grow" }),
        h("label", { class: "check" }, whole, " 显示整个屏幕")),
      h("div", { class: "qq-live-box" }, img), note,
      h("div", { class: "row" },
        h("button", { class: "ghost", type: "button", text: "刷新", onclick: () => refresh() }),
        h("button", { type: "button", text: "关闭", onclick: () => d.close() }))));
    d.addEventListener("close", () => { clearTimeout(timer); d.remove(); });
    document.body.append(d);
    d.showModal();
    refresh();
  }
  function qqWindow(holder) {                // the preview in `holder`; refresh() takes a new picture
    const img = h("img", { class: "qq-preview", alt: "QQ 窗口（点击放大）", title: "点击放大" });
    const frame = h("button", { type: "button", class: "qq-preview-frame", onclick: () => qqWindowDialog() }, img);
    const note = h("p", { class: "note", text: "正在读取 QQ 窗口…" });
    async function refresh() {
      try {
        const shot = await qqShot(false);
        img.src = shot.image;
        note.textContent = "这是服务器上 QQ 的窗口。要在窗口里点按钮，点画面放大后再点。";
      } catch (e) { note.textContent = e.message; }
    }
    holder.replaceChildren(frame, note, h("div", { class: "row" },
      h("button", { class: "ghost", type: "button", text: "放大操作", onclick: () => qqWindowDialog() }),
      h("button", { class: "ghost", type: "button", text: "刷新画面", onclick: () => refresh() })));
    refresh();
    return { refresh };
  }
  async function loadQQ(body, foot, ch, card) {
    clearTimeout(qqTimer);
    let s;
    try { s = await api("GET", "/api/control/qq"); } catch (e) { body.replaceChildren(h("p", { class: "err", text: e.message })); return; }
    const running = s.progress && (s.progress.state === "queued" || s.progress.state === "running");
    const again = (ms) => { qqTimer = setTimeout(() => { if (body.isConnected && $("#tab-channels").classList.contains("active") && !document.hidden) loadQQ(body, foot, ch, card); }, ms); };
    if (!s.installed) {
      setIntro(card, true);
      const failed = s.progress && s.progress.state === "failed" ? h("p", { class: "err", text: "上次安装失败：" + (s.progress.message || "") }) : null;
      body.replaceChildren(...[failed, h("p", { class: "note", text: "会在服务器上用 Docker 安装 SnowLuma 和它自带的 Linux QQ（第一次下载约 740 MB，几分钟到十几分钟；没有 Docker 会先装上）。只在本机通信，不开放任何端口。这是非官方的接入方式，QQ 号有被风控的可能，建议用小号。" })].filter(Boolean));
      foot.replaceChildren(running ? h("button", { type: "button", text: "正在安装…查看进度", onclick: () => watchQQ("安装 QQ") })
        : s.available ? h("button", { type: "button", text: "一键安装 QQ", onclick: async () => {
          if (!(await confirmBox("在服务器上安装 SnowLuma 和 Linux QQ（Docker）？第一次要下载约 740 MB。"))) return;
          qqTask("install", "安装 QQ");
        } }) : h("code", { text: s.install_command }));
      if (!s.available) body.append(h("p", { class: "note", text: "这种安装方式不能在网页上安装，请在服务器上运行上面的命令。" }));
      return;
    }
    const login = s.login || {};
    if (!login.logged_in) {
      setIntro(card, false);
      setHead(card, login.desktop ? "等待扫码" : "正在启动", "warn");
      if (!body.dataset.window) {            // the picture stays; only the state is polled
        body.dataset.window = "1";
        const holder = h("div", { class: "stack" });
        body.replaceChildren(step(1, "用手机 QQ 扫 QQ 窗口里的二维码", holder,
          h("p", { class: "note", text: "用要给 Violet 用的 QQ 号（建议小号）扫码，并在手机上确认。QQ 刚启动时要等几十秒才有窗口。" })));
        qqView = qqWindow(holder);
      } else if (qqView) qqView.refresh();
      foot.replaceChildren(h("button", { class: "ghost", type: "button", text: "重启 QQ", onclick: () => qqTask("restart", "重启 QQ") }));
      again(4000);
      return;
    }
    body.dataset.window = "";
    if (!s.channel) { loaders.channels(); return; }        // logged in just now: the channel was created
    ch = s.channel;
    setIntro(card, false);
    const parts = channelParts(ch, "你自己的 QQ");
    const holder = h("div", { class: "stack" });
    body.replaceChildren(...parts.body.filter(Boolean), holder);
    foot.replaceChildren(...parts.foot,
      h("button", { class: "ghost", type: "button", text: "看 QQ 窗口", onclick: () => { qqView = qqWindow(holder); } }),
      h("button", { class: "ghost", type: "button", text: "重启 QQ", onclick: () => qqTask("restart", "重启 QQ") }),
      h("button", { class: "danger", type: "button", text: "卸载", onclick: async () => {
        if (!(await confirmBox("卸载 QQ（SnowLuma）？这个 QQ 号会退出登录，群聊、表情包和聊天记录保留。"))) return;
        qqTask("uninstall", "卸载 QQ");
      } }));
  }

  function extraCard(ch) {
    const parts = channelParts(ch, "你自己的账号");
    return platformCard({ key: ch.id, name: ch.label, intro: "" }, ch, `${ch.label}（${ch.type}）`, parts.body, parts.foot);
  }

  loaders.channels = async () => {
    try { data = await api("GET", "/api/control/channels"); } catch (e) { toast(e.message, false); return; }
    const used = new Set();
    const pick = (p) => { const ch = data.channels.find((c) => c.type === p.type && !used.has(c.id) && (p.key !== "wechat" || c.id === "wechat"));
      if (ch) used.add(ch.id); return ch; };
    $("#ch-list").replaceChildren(...PLATFORMS.map((p) => { const ch = pick(p);
      if (p.key === "wechat") return wechatCard(p, ch || { id: "wechat", enabled: true, status: { state: "stopped" } });
      return p.key === "napcat" ? qqCard(p, ch) : botCard(p, ch); }));
    watchConnecting(data.channels);
    // channels added by hand in older versions (OneBot, a second bot) stay usable as cards
    $("#ch-extra").replaceChildren(...data.channels.filter((c) => !used.has(c.id)).map(extraCard));
    const sel = $("#ch-proactive");
    sel.replaceChildren(h("option", { value: "auto", text: "自动（你最近聊天的渠道）" }),
      ...data.channels.map((c) => h("option", { value: c.id, text: c.label })));
    sel.value = data.proactive_channel || "auto";
    if (window.renderSocial) window.renderSocial($("#ch-social"), data.channels.some((c) => c.type === "napcat"));
  };

  $("#ch-refresh").addEventListener("click", () => loaders.channels());
  $("#ch-proactive").addEventListener("change", async (ev) => {
    try { await api("POST", "/api/control/channels/proactive", { channel: ev.target.value }); toast("已保存"); } catch (e) { toast(e.message, false); }
  });
})();
