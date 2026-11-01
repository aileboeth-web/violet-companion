// Violet Control Center: her voice (2026-10-31; optional install of CosyVoice on this server's CPU), on the tools page
// under 联网工具: the server requirements, one-click install / uninstall with progress, the reference recording and
// what is said in it, and 试听 (she says a sentence -- about a minute). DOM nodes only.
"use strict";
(() => {
  const MAX = 10 * 1024 * 1024;
  let timer = null;

  async function act(method, url, body, done) {
    try { const r = await api(method, url, body || {}); if (done) toast(done); load(); return r; }
    catch (e) { toast(e.message, false); return null; }
  }

  function watch(title) {
    progressDialog({ title, url: "/api/control/progress/task", onFinish: () => load() });
  }

  // 2026-11-01 (user's server: a 15.6 s recording with the text 我是大肥鱼呀 gave 0.1-2 s murmurs): the text must be
  // everything said in the recording; people say 3-5 characters a second (same rule as tools/voice_server.py)
  const spoken = (s) => (s.match(/[\p{L}\p{N}]/gu) || []).length;
  const tooShort = (seconds, s) => seconds >= 2 && spoken(s) < seconds * 1.2;
  const shortNote = (seconds, s) => `录音有 ${seconds.toFixed(1)} 秒，“录音的原文”只有 ${spoken(s)} 个字。`
    + "要把录音里说的每一个字都照抄进去（不是一句介绍），不然生成出来只有一两秒的哼声。";

  function duration(url) {
    return new Promise((resolve) => {
      const a = new Audio();
      a.onloadedmetadata = () => resolve(Number.isFinite(a.duration) ? a.duration : 0);
      a.onerror = () => resolve(0);
      a.src = url;
    });
  }

  function referenceBox(v) {
    // 2026-11-01 (user: 录音里说的话和试听有点歧义，不知道该填哪个): name each box by what it is for
    const text = h("input", { maxlength: 300, placeholder: "例：傲娇怎么了？我就傲娇，再说了……（录音里的原话，一字不差）" });
    text.value = v.reference.text || "";
    const file = h("input", { type: "file", accept: "audio/*" });
    const warn = h("p", { class: "err" });
    let seconds = 0;
    const recheck = () => { warn.textContent = tooShort(seconds, text.value) ? shortNote(seconds, text.value) : ""; };
    text.addEventListener("input", recheck);
    const player = h("div", { class: "row" });
    if (v.reference.uploaded) {
      fetch("/api/control/voice/reference/audio", { credentials: "same-origin" }).then((r) => (r.ok ? r.blob() : null)).then(async (b) => {
        if (!b) return;
        const url = URL.createObjectURL(b);
        seconds = await duration(url);
        player.replaceChildren(h("audio", { controls: true, src: url }),
          h("span", { class: "note", text: seconds ? `${seconds.toFixed(1)} 秒` : "" }));
        recheck();
      }).catch(() => {});
    }
    file.addEventListener("change", async () => {
      const f = file.files[0];
      if (!f) return;
      const url = URL.createObjectURL(f);
      seconds = await duration(url);
      URL.revokeObjectURL(url);
      recheck();
    });
    const save = h("button", { type: "button", text: "保存录音和文字", onclick: async () => {
      let data;
      const f = file.files[0];
      if (f) {
        if (f.size > MAX) return toast("录音太大了（最多 10 MB，3–20 秒最合适）", false);
        data = await new Promise((resolve) => { const r = new FileReader(); r.onload = () => resolve(r.result); r.readAsDataURL(f); });
      }
      if (!data && !v.reference.uploaded) return toast("先选一段录音", false);
      if (tooShort(seconds, text.value) && !(await confirmBox(shortNote(seconds, text.value) + "\n还是照这样保存？"))) return;
      act("POST", "/api/control/voice/reference", { data, text: text.value }, "已保存");
    } });
    return h("div", { class: "stack" },
      h("strong", { text: "她的声音（参考录音）" }),
      h("p", { class: "note", text: v.reference.uploaded
        ? `✔ 服务器上已经存好：${v.reference.file}（${Math.round(v.reference.size / 1024)} KB），可以在下面播放。`
          + "刷新后“选择文件”是空的是正常的；要换一段才重新选文件。"
        : "上传一段 3–20 秒、只有一个人说话、没有背景音乐的录音，她就用这个声音说话。录音只存在这台服务器上。" }),
      player,
      h("label", { class: "row" }, h("span", { text: v.reference.uploaded ? "第 1 步：换一段录音" : "第 1 步：选录音" }), file),
      h("label", { class: "row grow" }, h("span", { text: "第 2 步：录音的原文" }), text),
      h("p", { class: "note", text: "上面那段录音里一字不差说了什么，就照抄什么（只用来让模型学声音，她不会说这段话）。"
        + "想让她说的话写在下面的“试听”里。" }),
      warn,
      h("div", { class: "row" }, save,
        v.reference.uploaded ? h("button", { type: "button", class: "ghost", text: "删除录音", onclick: async () => {
          if (await confirmBox("删除参考录音？删掉后她就不能发语音了。")) act("DELETE", "/api/control/voice/reference", null, "已删除");
        } }) : null));
  }

  function testBox(v) {
    const input = h("input", { maxlength: 120, value: "谁要你夸啦，不过今天的白米饭确实好好吃。" });  // a leading 哼 comes out as 空
    const out = h("div", { class: "row" });
    const btn = h("button", { type: "button", text: "试听", onclick: async () => {
      btn.disabled = true; btn.textContent = "生成中…（约 1 分钟）";
      try {
        const resp = await fetch("/api/control/voice/test", { method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json", Accept: "audio/wav, application/json" }, body: JSON.stringify({ text: input.value }) });
        if (!resp.ok) { let m = "试听失败 " + resp.status; try { m = (await resp.json()).error || m; } catch (_) { /* audio */ } throw new Error(m); }
        const url = URL.createObjectURL(await resp.blob());
        out.replaceChildren(h("audio", { controls: true, autoplay: true, src: url }));
      } catch (e) { toast(e.message, false); }
      btn.disabled = false; btn.textContent = "试听";
    } });
    btn.disabled = !(v.health && v.reference.uploaded);
    return h("div", { class: "stack" }, h("strong", { text: "试听：想让她说的话" }),
      h("p", { class: "note", text: "随便写一句（120 字以内），点试听，她就用上面录音的声音把这句说出来。和录音原文无关，不用一样。" }),
      h("label", { class: "row grow" }, input, btn), out);
  }

  async function load() {
    const box = $("#tools-voice");
    if (!box) return;
    let v;
    try { v = await api("GET", "/api/control/voice"); } catch (e) { box.replaceChildren(h("p", { class: "err", text: e.message })); return; }
    clearTimeout(timer);
    const running = v.progress && ["queued", "running"].includes(v.progress.state);
    if (running) timer = setTimeout(load, 8000);
    const status = v.installed
      ? (v.health ? pill(v.health.busy ? "已安装 · 正在说话" : "已安装 · 正常", "ok") : pill("已安装 · 语音服务没有响应（可能还在加载模型）", "warn"))
      : pill("未安装", "warn");
    const buttons = [];
    if (running) buttons.push(h("button", { type: "button", text: "正在执行…查看进度", onclick: () => watch("语音") }));
    else if (!v.installed) buttons.push(h("button", { type: "button", text: "安装语音", disabled: !v.can_install, onclick: async () => {
      if (!(await confirmBox("在这台服务器上安装语音（CosyVoice）？要下载约 3 GB 依赖和 5 GB 模型，通常 20–40 分钟，期间聊天照常。"))) return;
      if (await act("POST", "/api/control/voice/install", { confirm: true })) watch("安装语音");
    } }));
    else buttons.push(h("button", { type: "button", class: "danger", text: "卸载语音", onclick: async () => {
      if (!(await confirmBox("卸载语音？会删掉模型和语音服务（约 8 GB），上传的参考录音保留。"))) return;
      if (await act("POST", "/api/control/voice/uninstall", { confirm: true })) watch("卸载语音");
    } }));
    box.replaceChildren(h("div", { class: "card", id: "voice-card" },
      h("div", { class: "row" }, h("h3", { text: "语音（可选）" }), status),
      h("p", { class: "note", text: "让她用一段录音里的声音发 QQ 语音条（CosyVoice，在这台服务器上运行，不用联网服务）。"
        + "没有显卡时一秒语音大约要算 30 秒，所以文字先发，语音几分钟后补上；私聊里每次回复都会挑一句发成语音，"
        + "让她念歌词、故事时会拆成几条语音一条条发。她只能念，不能按旋律唱。" }),
      h("details", { open: !v.installed }, h("summary", { text: "服务器需要的条件" }),
        ...v.requirements.map((r) => h("div", { class: "note", text: "· " + r }))),
      h("div", { class: "row" }, ...buttons,
        v.can_install ? null : h("span", { class: "note", text: `这种安装方式不能在网页上安装，请在服务器上运行：${v.command}` })),
      v.installed ? referenceBox(v) : null,
      v.installed ? testBox(v) : null,
      v.installed && !v.enabled ? h("p", { class: "note", text: "“设置 → 她的语音”关着：她现在不会发语音。" }) : null));
  }

  const previous = loaders.tools;
  loaders.tools = async () => { if (previous) await previous(); load(); };
})();
