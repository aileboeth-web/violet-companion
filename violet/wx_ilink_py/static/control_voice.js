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

  function referenceBox(v) {
    const text = h("input", { maxlength: 300, placeholder: "录音里说的每一个字（照抄，越准越像）" });
    text.value = v.reference.text || "";
    const file = h("input", { type: "file", accept: "audio/*" });
    const save = h("button", { type: "button", text: "保存录音和文字", onclick: async () => {
      let data;
      const f = file.files[0];
      if (f) {
        if (f.size > MAX) return toast("录音太大了（最多 10 MB，3–20 秒最合适）", false);
        data = await new Promise((resolve) => { const r = new FileReader(); r.onload = () => resolve(r.result); r.readAsDataURL(f); });
      }
      if (!data && !v.reference.uploaded) return toast("先选一段录音", false);
      act("POST", "/api/control/voice/reference", { data, text: text.value }, "已保存");
    } });
    return h("div", { class: "stack" },
      h("strong", { text: "她的声音（参考录音）" }),
      h("p", { class: "note", text: v.reference.uploaded
        ? `已上传：${v.reference.file}（${Math.round(v.reference.size / 1024)} KB）。换一段就重新选文件。`
        : "上传一段 3–20 秒、只有一个人说话、没有背景音乐的录音，她就用这个声音说话。录音只存在这台服务器上。" }),
      h("label", { class: "row" }, h("span", { text: "录音" }), file),
      h("label", { class: "row grow" }, h("span", { text: "录音里说的话" }), text),
      h("div", { class: "row" }, save,
        v.reference.uploaded ? h("button", { type: "button", class: "ghost", text: "删除录音", onclick: async () => {
          if (await confirmBox("删除参考录音？删掉后她就不能发语音了。")) act("DELETE", "/api/control/voice/reference", null, "已删除");
        } }) : null));
  }

  function testBox(v) {
    const input = h("input", { maxlength: 120, value: "哼，谁要你夸啦，不过今天的白米饭确实好好吃。" });
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
    return h("div", { class: "stack" }, h("strong", { text: "试听" }), h("label", { class: "row grow" }, input, btn), out);
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
        + "没有显卡时一句话大约要 1 分钟，所以只在她情绪重或你想听她说话时才发语音，其余照常打字；语音没生成出来就改发文字。" }),
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
