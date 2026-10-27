// Violet Control Center: first-run setup page (phase 10.3). Uses control.js's helpers ($, h, api, toast, pill, fmt,
// loaders). Saves through PUT /api/control/settings (validated, recorded, reversible); the server decides everything.
"use strict";
(() => {
  const STATE = { ok: ["已完成", "ok"], todo: ["待完成", "bad"], warn: ["需要检查", "warn"], optional: ["可选", ""] };
  let settings = null;

  function goTab(tab) {
    const btn = document.querySelector(`#tabs button[data-tab="${CSS.escape(tab)}"]`);
    if (tab === "persona") { location.href = "/persona"; return; }
    if (btn) btn.click();
  }

  function renderChecklist(data) {
    $("#setup-summary").textContent = data.complete
      ? "必需的设置都完成了，Violet 可以正常聊天。下面的可选项可以随时调整。"
      : "还有必需的设置没有完成：" + (data.next ? data.next.title + "（" + data.next.detail + "）" : "");
    $("#setup-list").replaceChildren(...data.items.map((item) => {
      const [text, kind] = STATE[item.state] || [item.state, ""];
      return h("div", { class: "card" }, h("div", { class: "row" }, pill(text, kind), h("strong", { text: item.title }),
        item.tab !== "setup" ? h("button", { class: "ghost", text: "去设置", onclick: () => goTab(item.tab) }) : null),
        h("p", { class: "note", text: item.detail }));
    }));
  }

  function modelRows() {
    return (settings.settings || []).filter((s) => s.group === "models" && s.key.endsWith("_LLM_MODEL"));
  }

  function renderModels() {
    $("#setup-models").replaceChildren(...modelRows().map((s) => {
      const input = h("input", { "data-key": s.key, list: "setup-model-names", maxlength: 120, autocomplete: "off" });
      input.value = s.value || "";
      return h("label", {}, s.label + " ", input,
        h("span", { class: "note", text: s.source === "default" ? "（代码默认，建议改成服务上真实存在的模型）" : "" }));
    }));
  }

  async function save(changes, reason) {
    return api("PUT", "/api/control/settings", { changes, reason });
  }

  function showTest(r) {
    const box = $("#setup-test");
    box.replaceChildren(pill(r.ok ? "测试通过" : "测试失败", r.ok ? "ok" : "bad"),
      h("span", { text: ` ${r.message}（模型 ${fmt(r.model)}，${r.latency_ms} 毫秒）` }));
  }

  loaders.setup = async () => {
    try {
      const [data, s] = await Promise.all([api("GET", "/api/control/setup"), api("GET", "/api/control/settings")]);
      settings = s;
      renderChecklist(data);
      const url = s.settings.find((x) => x.key === "LLM_API_URL");
      const key = s.settings.find((x) => x.key === "LLM_API_KEY");
      $("#setup-url").value = (url && url.value) || "";
      $("#setup-key").placeholder = key && key.is_set ? `已设置（指纹 ${key.fingerprint}），留空 = 不修改` : "API Key";
      renderModels();
      if (data.model_test) showTest(data.model_test);
    } catch (e) { toast(e.message, false); }
  };

  $("#setup-api").addEventListener("submit", async (ev) => {
    ev.preventDefault(); $("#setup-api-error").textContent = "";
    const changes = {};
    const url = $("#setup-url").value.trim(); const key = $("#setup-key").value.trim();
    const current = settings.settings.find((x) => x.key === "LLM_API_URL");
    if (url && (!current || url !== current.value)) changes.LLM_API_URL = url;
    if (key) changes.LLM_API_KEY = key;
    try {
      if (Object.keys(changes).length) await save(changes, "初始化向导：模型服务");
      $("#setup-key").value = "";
      $("#setup-test").textContent = "正在测试…";
      showTest(await api("POST", "/api/control/setup/test-model", {}));
      loaders.setup();
    } catch (e) {
      const p = e.data && e.data.problems;
      $("#setup-api-error").textContent = p ? Object.values(p).join("；") : e.message;
    }
  });

  $("#setup-list-models").addEventListener("click", async () => {
    try {
      const r = await api("POST", "/api/control/setup/models", {});
      if (!r.ok) { toast(r.message, false); return; }
      $("#setup-model-names").replaceChildren(...r.models.map((m) => h("option", { value: m })));
      toast(`服务上有 ${r.models.length} 个模型，输入框里可以直接选`);
    } catch (e) { toast(e.message, false); }
  });

  $("#setup-same").addEventListener("click", () => {
    const value = $("#setup-all").value.trim();
    if (!value) { toast("先填一个模型名", false); return; }
    document.querySelectorAll("#setup-models input[data-key]").forEach((i) => { i.value = value; });
  });

  $("#setup-models-save").addEventListener("click", async () => {
    $("#setup-models-error").textContent = "";
    const changes = {};
    document.querySelectorAll("#setup-models input[data-key]").forEach((i) => {
      const row = modelRows().find((s) => s.key === i.dataset.key);
      if (i.value.trim() && (!row || i.value.trim() !== row.value || row.source === "default")) changes[i.dataset.key] = i.value.trim();
    });
    try {
      if (Object.keys(changes).length) await save(changes, "初始化向导：各 Agent 的模型");
      $("#setup-test").textContent = "正在测试…";
      showTest(await api("POST", "/api/control/setup/test-model", {}));
      loaders.setup();
    } catch (e) {
      const p = e.data && e.data.problems;
      $("#setup-models-error").textContent = p ? Object.values(p).join("；") : e.message;
    }
  });
})();
