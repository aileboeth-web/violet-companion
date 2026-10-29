// Violet Control Center: the "图片" page (0.7.2; the old /images page went with the old panel in 0.7.1). Switch the
// image features on, pick and test the recognition model and the drawing model, see and delete recent pictures.
// Settings are saved through PUT /api/control/settings (validated, recorded); GET /api/images/panel,
// POST /api/images/panel/test-vision, /test-generate. DOM nodes only.
"use strict";
(() => {
  const KIND = { inbound: "收到的", generated: "画的", reference: "参考图", outbound: "发出的" };
  const MAIN = ["VIOLET_IMAGE_ENABLED", "VISION_LLM_MODEL", "VIOLET_IMAGE_GEN_MODEL", "VIOLET_IMAGE_GEN_PROTOCOL",
    "VIOLET_IMAGE_DAILY_LIMIT", "VIOLET_IMAGE_API_URL", "VIOLET_IMAGE_API_KEY"];
  const MANUAL = "__manual__";
  let models = [];                 // from the API (GET /models of the model service), once per page visit
  let fetchNote = "";
  const more = {};

  async function save(changes, reason) {
    if (!Object.keys(changes).length) { toast("没有改动"); return false; }
    try { await api("PUT", "/api/control/settings", { changes, reason }); toast("已保存"); return true; }
    catch (e) {
      const p = e.data && e.data.problems;
      toast(p ? Object.values(p).join("；") : e.message, false);
      return false;
    }
  }
  // 0.7.6: like the model page -- the upstream list (+ "手动输入…"), or only a text box while there is no list.
  // picker.input always holds the chosen name ("" = emptyText).
  function modelPicker(value, placeholder, emptyText) {
    const input = h("input", { maxlength: 120, autocomplete: "off", placeholder });
    input.value = value || "";
    const fetchBtn = h("button", { class: "ghost", type: "button", text: models.length ? "重新获取模型" : "从上游获取模型",
      onclick: () => fetchModels(fetchBtn) });
    if (!models.length) return { node: h("span", { class: "row" }, input, fetchBtn), input };
    const sel = h("select", {}, h("option", { value: "", text: emptyText }),
      ...models.map((m) => h("option", { value: m, text: m })), h("option", { value: MANUAL, text: "手动输入…" }));
    const listed = !value || models.includes(value);
    sel.value = listed ? (value || "") : MANUAL;
    input.hidden = listed;
    sel.addEventListener("change", () => {
      input.hidden = sel.value !== MANUAL;
      if (sel.value === MANUAL) input.focus(); else input.value = sel.value;
    });
    return { node: h("span", { class: "row" }, h("span", { class: "picker" }, sel, input), fetchBtn), input };
  }

  async function fetchModels(btn) {
    if (btn) { btn.disabled = true; btn.textContent = "正在获取…"; }
    try {
      const r = await api("POST", "/api/control/setup/models", {});
      if (r.ok && r.models.length) { models = r.models; fetchNote = ""; }
      else fetchNote = "没能从上游获取模型：" + (r.message || "列表是空的") + "。可以直接手动填写模型名。";
    } catch (e) { fetchNote = "没能从上游获取模型：" + e.message + "。可以直接手动填写模型名。"; }
    if (btn) load();
    return models.length > 0;
  }
  const card = (title, ...body) => h("div", { class: "card" }, h("div", { class: "row" }, h("h3", { text: title })), ...body);

  function switchCard(st) {
    return card("图片功能",
      h("div", { class: "row" }, pill(st.enabled ? "已开启" : "已关闭", st.enabled ? "ok" : ""),
        h("button", { type: "button", class: st.enabled ? "ghost" : "", text: st.enabled ? "关闭图片功能" : "开启图片功能",
          onclick: async () => { if (await save({ VIOLET_IMAGE_ENABLED: !st.enabled }, "图片页：开关")) load(); } })),
      h("p", { class: "note", text: "开启后 Violet 在聊天里可以画图（要下面的画图模型测试通过才会用）。关闭时你发来的图片仍然会按“看图”的设置识别。" }));
  }

  function visionCard(st, settings) {
    const chosen = st.vision.model === "vision" ? "" : st.vision.model;     // the old placeholder = not chosen
    const pick = modelPicker(chosen, "留空 = 和 Chat 用同一个模型", "和 Chat 用同一个模型");
    const input = pick.input;
    const out = h("div", { class: "stack" });
    return card("看图（识别你发来的图片）",
      h("div", { class: "stack" }, h("label", {}, "识图模型 ", pick.node),
        fetchNote ? h("p", { class: "err", text: fetchNote }) : null,
        !chosen ? h("p", { class: "note", text: st.vision.effective ? `现在用 Chat 的模型：${st.vision.effective}` : "还没有设置 Chat 模型" }) : null),
      h("div", { class: "row" },
        h("button", { type: "button", text: "保存", onclick: async () => {
          if (await save({ VISION_LLM_MODEL: input.value.trim() }, "图片页：识图模型")) load();
        } }),
        h("button", { class: "ghost", type: "button", text: "测试识图", onclick: async (ev) => {
          ev.target.disabled = true;
          out.replaceChildren(h("p", { class: "note", text: "正在发一张红色方块给识图模型…" }));
          try {
            const r = await api("POST", "/api/images/panel/test-vision", {});
            out.replaceChildren(h("div", { class: "row" }, pill(r.ok ? "识图正常" : "识图失败", r.ok ? "ok" : "bad"),
              h("span", { class: "note", text: (r.ok ? `${r.model} 说：${r.text}` : r.error) + `（${r.latency_ms} 毫秒）` })));
          } catch (e) { out.replaceChildren(h("p", { class: "err", text: e.message })); }
          ev.target.disabled = false;
        } })),
      out,
      h("p", { class: "note", text: "用“模型服务”的接口地址和 Key。测试会真实调用一次（发一张很小的图）。识图模型要支持图片输入（多模态）。" }));
  }

  function genCard(st, byKey) {
    const g = st.gen;
    const pick = modelPicker(g ? g.model : "", "画图模型，例如 gpt-image-1、dall-e-3", "不画图");
    const model = pick.input;
    const protocol = h("select", {}, h("option", { value: "openai_images", text: "图片接口（/images/generations）：gpt-image-1、dall-e-3 等" }),
      h("option", { value: "responses", text: "Responses 画图工具（/responses）：gpt-5.x / Codex 这类对话模型" }),
      h("option", { value: "chat_completions", text: "对话接口返回图片" }));
    protocol.value = (byKey("VIOLET_IMAGE_GEN_PROTOCOL") || {}).value || "openai_images";
    const limit = h("input", { type: "number", min: 1, max: 200, step: 1 });
    limit.value = (byKey("VIOLET_IMAGE_DAILY_LIMIT") || {}).value ?? 10;
    const url = h("input", { maxlength: 300, autocomplete: "off", placeholder: "留空 = 和“模型服务”一样" });
    url.value = (byKey("VIOLET_IMAGE_API_URL") || {}).value || "";
    const keyRow = byKey("VIOLET_IMAGE_API_KEY") || {};
    const key = h("input", { type: "password", maxlength: 512, autocomplete: "new-password",
      placeholder: keyRow.is_set ? `已设置（指纹 ${keyRow.fingerprint}），留空 = 不修改` : "留空 = 用“模型服务”的 Key" });
    const out = h("div", { class: "stack" });
    let status = pill("还没选模型", "");
    if (g) {
      const last = g.last_check;
      status = g.verified ? pill("测试通过，可以使用", "ok")
        : last && last.current && !last.passed ? pill("测试没通过", "bad") : pill("还没测试", "warn");
    }
    return card("画图",
      h("div", { class: "row" }, status, g ? h("span", { class: "note", text: `今天画了 ${g.usage_today.images} 张 / 上限 ${g.daily_limit}` }) : null),
      g && g.last_check && g.last_check.current && !g.last_check.passed ? h("p", { class: "err", text: "上次测试：" + g.last_check.detail }) : null,
      h("div", { class: "stack" },
        h("label", {}, "画图模型 ", pick.node),
        h("label", {}, "接口方式 ", protocol),
        h("label", {}, "每天最多画几张（包括测试） ", limit),
        h("details", {}, h("summary", { text: "单独的 API 地址和 Key（一般不用填）" }),
          h("div", { class: "stack" }, h("label", {}, "API 地址 ", url), h("label", {}, "API Key ", key),
            keyRow.is_set ? h("button", { class: "ghost", type: "button", text: "不再单独设置 Key", onclick: async () => {
              if (await save({ VIOLET_IMAGE_API_KEY: null }, "图片页：画图 Key 改回模型服务")) load();
            } }) : null))),
      h("div", { class: "row" },
        h("button", { type: "button", text: "保存", onclick: async () => {
          const changes = {};
          const m = model.value.trim();
          if (m !== (g ? g.model : "")) changes.VIOLET_IMAGE_GEN_MODEL = m;
          if (protocol.value !== ((byKey("VIOLET_IMAGE_GEN_PROTOCOL") || {}).value || "openai_images")) changes.VIOLET_IMAGE_GEN_PROTOCOL = protocol.value;
          if (String(limit.value) !== String((byKey("VIOLET_IMAGE_DAILY_LIMIT") || {}).value ?? 10)) changes.VIOLET_IMAGE_DAILY_LIMIT = Number(limit.value);
          if (url.value.trim() !== ((byKey("VIOLET_IMAGE_API_URL") || {}).value || "")) changes.VIOLET_IMAGE_API_URL = url.value.trim() || null;
          if (key.value.trim()) changes.VIOLET_IMAGE_API_KEY = key.value.trim();
          if (await save(changes, "图片页：画图模型")) load();
        } }),
        g ? h("button", { class: "ghost", type: "button", text: "测试画图", onclick: async (ev) => {
          if (!(await confirmBox(`用 ${g.model} 真实画一张图（“一个简单的蓝色圆形”）。大多数画图服务按张收费，这一张也算在每天的上限里。继续？`))) return;
          ev.target.disabled = true;
          out.replaceChildren(h("p", { class: "note", text: "正在画图，可能要几十秒…" }));
          try {
            const r = await api("POST", "/api/images/panel/test-generate", { confirm_paid: true });
            const ids = (r.task && r.task.asset_ids) || [];
            out.replaceChildren(h("div", { class: "row" }, pill(r.passed ? "测试通过" : "测试失败", r.passed ? "ok" : "bad"),
              h("span", { class: "note", text: r.passed ? "这个模型可以用了"
                : `原因：${[(r.task && r.task.error_code) || "未知", r.task && r.task.message].filter(Boolean).join("：")}` })),
              ...ids.slice(0, 1).map((id) => h("img", { class: "img-thumb big", src: `/api/images/assets/${id}/file`, alt: "测试画的图" })));
            if (r.passed) setTimeout(load, 1500);
          } catch (e) { out.replaceChildren(h("p", { class: "err", text: e.message })); }
          ev.target.disabled = false;
        } }) : null),
      out,
      h("p", { class: "note", text: "改了模型、接口方式或地址以后要重新测试一次：只有测试通过的设置才会在聊天里使用。" }));
  }

  function galleryCard(assets) {
    const shown = assets.filter((a) => a.media_kind !== "probe");
    return card("最近的图片",
      shown.length ? h("div", { class: "img-grid" }, ...shown.map((a) => h("figure", { class: "img-item" },
        h("img", { class: "img-thumb", src: `/api/images/assets/${a.id}/file`, alt: a.caption || "图片", loading: "lazy" }),
        h("figcaption", { class: "note", text: `${KIND[a.direction] || a.direction} · ${when(a.created_at)}` }),
        h("button", { class: "ghost", type: "button", text: "删除", onclick: async () => {
          if (!(await confirmBox("删除这张图片的文件？（对它的文字描述另按保留天数处理）"))) return;
          try { await api("DELETE", `/api/images/assets/${a.id}`); toast("已删除"); load(); } catch (e) { toast(e.message, false); }
        } }))))
        : h("p", { class: "note", text: "还没有图片。" }));
  }

  function moreCard(settings) {
    for (const k of Object.keys(more)) delete more[k];
    const rows = settings.filter((s) => s.group === "images" && !MAIN.includes(s.key));
    const btn = h("button", { type: "button", text: "保存", disabled: true, onclick: async () => {
      if (await save({ ...more }, "图片页：更多设置")) load();
    } });
    return h("details", { class: "card" }, h("summary", { text: "更多设置（保留天数、识图次数上限……）" }),
      ...rows.map((s) => settingInput(s, { pending: more, changed: () => { btn.disabled = false; } })), h("div", { class: "row" }, btn));
  }

  async function load() {
    const root = $("#img-root");
    try {
      // first visit: the model list is fetched together with the page, so the page is drawn once (no flash)
      const [st, s] = await Promise.all([api("GET", "/api/images/panel"), api("GET", "/api/control/settings"),
        !models.length && !fetchNote ? fetchModels(null) : null]);
      const byKey = (k) => (s.settings || []).find((x) => x.key === k);
      root.replaceChildren(switchCard(st), visionCard(st, s.settings), genCard(st, byKey), galleryCard(st.assets || []),
        moreCard(s.settings || []));
    } catch (e) { root.replaceChildren(h("p", { class: "err", text: e.message })); }
  }

  loaders.images = load;
  $("#img-refresh").addEventListener("click", load);
})();
