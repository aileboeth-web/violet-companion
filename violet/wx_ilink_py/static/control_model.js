// Violet Control Center: the model page (Violet stage 3; was part of the start page and the settings page). The API
// address and key, the models the API offers (fetched from it and picked from a list -- typing stays possible), and
// per agent the model, fallback model and temperature. Saves through PUT /api/control/settings (validated, recorded,
// reversible), then tests with one short real request. Built with DOM nodes only.
"use strict";
(() => {
  const ROLES = ["CHAT", "INTENT", "MEMORY", "PROACTIVE", "PERFORMER"];
  const NAMES = { CHAT: ["Chat", "写回复"], INTENT: ["Intent", "理解你说的每句话"], MEMORY: ["Memory", "整理记忆"],
    PROACTIVE: ["Proactive", "决定要不要主动找你"], PERFORMER: ["Performer", "执行任务（查资料、用工具）"] };
  const MANUAL = "__manual__";
  const ADVANCED = ["LLM_REQUEST_TIMEOUT_SECONDS", "LLM_CONNECT_TIMEOUT_SECONDS", "LLM_TOTAL_TIMEOUT_SECONDS",
    "LLM_MAX_RETRY_ATTEMPTS", "LLM_RETRY_BASE_DELAY"];
  let settings = null;
  let models = [];                 // from the API (this page visit)
  const advanced = {};
  const byKey = (k) => (settings.settings || []).find((s) => s.key === k);

  // a model picker: the API's list (+ "手动输入…"), or only a text box while there is no list
  function picker(s) {
    const input = h("input", { "data-model": s.key, maxlength: 120, autocomplete: "off",
      placeholder: s.optional ? "不用备用模型" : "模型名", "aria-label": s.label });
    input.value = s.value || "";
    if (!models.length) return input;
    const sel = h("select", { "aria-label": s.label },
      h("option", { value: "", text: s.optional ? "不用备用模型" : "选一个模型…" }),
      ...models.map((m) => h("option", { value: m, text: m })),
      h("option", { value: MANUAL, text: "手动输入…" }));
    const listed = s.value ? models.includes(s.value) : true;      // nothing saved yet: "选一个模型…"
    sel.value = listed ? (s.value || "") : MANUAL;
    input.hidden = listed;
    sel.addEventListener("change", () => {
      input.hidden = sel.value !== MANUAL;
      if (sel.value === MANUAL) input.focus(); else input.value = sel.value;
    });
    return h("span", { class: "picker" }, sel, input);
  }

  function renderRows() {
    $("#model-rows").replaceChildren(...ROLES.map((r) => {
      const main = byKey(`${r}_LLM_MODEL`), fb = byKey(`${r}_LLM_FALLBACK_MODEL`), temp = byKey(`${r}_LLM_MODEL_TEMPERATURE`);
      if (!main) return null;
      const [name, what] = NAMES[r];
      const t = temp ? h("input", { type: "number", min: 0, max: 2, step: "0.05", "data-temp": temp.key, "aria-label": temp.label,
        placeholder: "默认" }) : null;
      if (t) { t.value = temp.value ?? ""; t.dataset.initial = t.value; }
      const warn = main.source === "default" ? "还在用代码里的默认模型，建议换成服务上真实存在的模型"
        : models.length && main.value && !models.includes(main.value) ? `服务返回的模型列表里没有“${main.value}”` : "";
      return h("div", { class: "model-row" },
        h("div", { class: "model-name" }, h("strong", { text: name }), h("span", { class: "note", text: what })),
        h("label", {}, h("span", { class: "note", text: "模型" }), picker(main)),
        fb ? h("label", {}, h("span", { class: "note", text: "备用模型" }), picker(fb)) : null,
        t ? h("label", { class: "temp" }, h("span", { class: "note", text: "温度" }), t) : null,
        warn ? h("p", { class: "err", text: warn }) : null);
    }).filter(Boolean));
    // choosing here fills every Agent at once (saved with the button below)
    const all = models.length ? h("select", { id: "model-all", onchange: applyAll },
      h("option", { value: "", text: "选一个模型…" }), ...models.map((m) => h("option", { value: m, text: m })))
      : h("input", { id: "model-all", maxlength: 120, placeholder: "模型名，回车填到全部", onkeydown: (ev) => {
        if (ev.key === "Enter") { ev.preventDefault(); applyAll(); } } });
    $("#model-all-slot").replaceChildren(all);
  }

  function applyAll() {
    const value = ($("#model-all").value || "").trim();
    if (!value) return;
    document.querySelectorAll("#model-rows .model-row").forEach((row) => {
      const main = row.querySelector("[data-model$='_LLM_MODEL']:not([data-model*='FALLBACK'])");
      if (!main) return;
      main.value = value;
      const sel = main.previousElementSibling;
      if (sel && sel.tagName === "SELECT") { sel.value = models.includes(value) ? value : MANUAL; main.hidden = sel.value !== MANUAL; }
    });
    toast("已填到每个 Agent，点“保存模型并测试对话”生效");
  }

  function showTest(box, r) {
    box.replaceChildren(pill(r.ok ? "测试通过" : "测试失败", r.ok ? "ok" : "bad"),
      h("span", { class: "note", text: ` ${r.message}（模型 ${fmt(r.model)}，${r.latency_ms} 毫秒）` }));
  }
  const problems = (e) => { const p = e.data && e.data.problems; return p ? Object.values(p).join("；") : e.message; };

  // GET /models of the API: the list to pick from, and (step 1) the connection test -- it uses no model at all
  async function fetchModels(quiet) {
    const note = $("#model-fetch-note");
    note.textContent = "正在从上游获取模型…";
    try {
      const r = await api("POST", "/api/control/setup/models", {});
      if (!r.ok) { note.textContent = "获取失败：" + r.message + "。可以直接手动填写模型名。"; if (!quiet) toast(r.message, false); return r; }
      models = r.models;
      note.textContent = `上游有 ${models.length} 个模型，直接在下面选；列表里没有的可以选“手动输入…”。`;
      renderRows();
      return r;
    } catch (e) { note.textContent = "获取失败：" + e.message; return { ok: false, message: e.message }; }
  }

  // afterSave: step 1 just saved the API -- it tests the connection itself, so no automatic listing here
  loaders.model = async (afterSave) => {
    try {
      const [s, setup] = await Promise.all([api("GET", "/api/control/settings"), api("GET", "/api/control/setup")]);
      settings = s;
      const url = byKey("LLM_API_URL"), key = byKey("LLM_API_KEY");
      $("#model-url").value = (url && url.value) || "";
      $("#model-key").value = "";
      $("#model-key").placeholder = key && key.is_set ? `已设置（指纹 ${key.fingerprint}），留空 = 不修改` : "API Key";
      if (setup.model_test) showTest($("#model-save-test"), setup.model_test);     // the last chat test (step 2)
      renderRows();
      for (const k of Object.keys(advanced)) delete advanced[k];
      $("#model-adv-save").disabled = true;
      $("#model-adv-rows").replaceChildren(...ADVANCED.map(byKey).filter(Boolean).map((row) =>
        settingInput(row, { pending: advanced, changed: () => { $("#model-adv-save").disabled = false; } })));
      // the list comes from the API itself: fetch it once the API is set (quietly, it may not support listing)
      if (afterSave !== true && !models.length && url && url.value && key && key.is_set) fetchModels(true);
    } catch (e) { toast(e.message, false); }
  };

  $("#model-fetch").addEventListener("click", () => fetchModels(false));

  $("#model-api-form").addEventListener("submit", async (ev) => {
    ev.preventDefault(); $("#model-api-error").textContent = "";
    const changes = {};
    const url = $("#model-url").value.trim(), key = $("#model-key").value.trim(), current = byKey("LLM_API_URL");
    if (url && (!current || url !== current.value)) changes.LLM_API_URL = url;
    if (key) changes.LLM_API_KEY = key;
    try {
      if (Object.keys(changes).length) await api("PUT", "/api/control/settings", { changes, reason: "模型服务：接口" });
      const box = $("#model-test");
      box.textContent = "正在测试连接…";
      models = [];
      await loaders.model(true);
      const r = await fetchModels(true);
      if (r.ok) {
        box.replaceChildren(pill("连接正常", "ok"), h("span", { class: "note", text: ` 上游有 ${models.length} 个模型。下一步：在下面选模型。` }));
        $("#model-agents").scrollIntoView({ behavior: "smooth", block: "start" });
      } else if (r.kind === "unsupported" || r.kind === "not_found") {
        box.replaceChildren(pill("无法列出模型", "warn"), h("span", { class: "note", text: ` ${r.message}。在下面手动填模型名后点“保存模型并测试对话”。` }));
      } else {
        box.replaceChildren(pill("连接失败", "bad"), h("span", { class: "note", text: " " + r.message }));
      }
    } catch (e) { $("#model-api-error").textContent = problems(e); }
  });

  $("#model-save").addEventListener("click", async () => {
    $("#model-error").textContent = "";
    const changes = {};
    document.querySelectorAll("#model-rows [data-model]").forEach((i) => {
      const s = byKey(i.dataset.model), v = i.value.trim();
      if (!s) return;
      if (s.optional) { if (v !== (s.value || "")) changes[s.key] = v; }
      else if (v && (v !== s.value || s.source === "default")) changes[s.key] = v;
    });
    document.querySelectorAll("#model-rows [data-temp]").forEach((i) => {
      if (i.value !== i.dataset.initial) changes[i.dataset.temp] = i.value === "" ? null : Number(i.value);
    });
    try {
      if (Object.keys(changes).length) await api("PUT", "/api/control/settings", { changes, reason: "模型服务：各 Agent 的模型" });
      $("#model-save-test").textContent = "正在测试…";
      showTest($("#model-save-test"), await api("POST", "/api/control/setup/test-model", {}));
      const list = models; await loaders.model(); models = list; renderRows();
      toast(Object.keys(changes).length ? `已保存 ${Object.keys(changes).length} 项` : "没有改动，已重新测试");
    } catch (e) { $("#model-error").textContent = problems(e); }
  });

  $("#model-adv-save").addEventListener("click", async () => {
    try {
      await api("PUT", "/api/control/settings", { changes: advanced, reason: "模型服务：超时与重试" });
      toast("已保存"); loaders.model();
    } catch (e) { toast(problems(e), false); }
  });
})();
