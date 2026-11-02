// 2026-11-02 (user: Eve 原来进去就有 Intent 分析、Chat 生成这种页面，做一个适配我们 UI 的，放在概览旁边，叫“流程”).
// The last 20 runs (your messages, proactive messages, QQ groups / other people) from GET /api/control/flow; the
// selected run is drawn as a row of steps, a click on a step shows what went in and what came out. While the page is
// open it looks again every 2 seconds. Nothing here is stored: the server keeps the runs in memory only.
"use strict";
(() => {
  const TYPE = { chat: "你的消息", proactive: "主动联系", social: "QQ 群聊 / 别人" };
  const NODE = {
    Intent: ["意图识别", "判断这句话要做什么（默认关闭）"], Performer: ["执行任务", "用会改动东西的工具"],
    Chat: ["生成回复", "写出要发的话，需要时查工具和记忆"], Memory: ["整理记忆", "从这一轮里记下该记的"],
    Proactive: ["主动决策", "要不要主动找你、说什么"], Social: ["群聊决定", "说不说、说什么、发不发表情包"],
    Act: ["发出去", "发消息、语音、表情包，记下印象"],
  };
  const STATUS = { pending: ["等待", ""], queued: ["后台排队", "warn"], running: ["进行中", "warn"], done: ["完成", "ok"], failed: ["失败", "bad"], skipped: ["跳过", ""] };
  const ROLE = { system: "规则 / 资料", user: "对方 / 提示", assistant: "她", tool: "工具结果" };
  const state = { runs: [], selected: null, node: null, timer: null, detail: null, listSig: "", runSig: "" };

  const secs = (a, b) => (a && b ? ((b - a) < 1 ? Math.round((b - a) * 1000) + " 毫秒" : (b - a).toFixed(1) + " 秒") : "");
  const clock = (t) => (t ? new Date(t * 1000).toLocaleTimeString() : "—");

  async function refresh() {
    let d;
    try { d = await api("GET", "/api/control/flow"); } catch (e) { $("#flow-list").replaceChildren(h("p", { class: "err", text: e.message })); return; }
    // looking at the newest run: follow the next one when it starts (live); an older one stays selected
    const following = !state.runs.length || state.selected === state.runs[0].id;
    state.runs = d.runs || [];
    if (state.selected === null || following || !state.runs.some((r) => r.id === state.selected)) {
      const next = state.runs.length ? state.runs[0].id : null;
      if (next !== state.selected) state.node = null;
      state.selected = next;
    }
    renderList();
    await renderRun();
  }

  // 2026-11-02 (user: 生成回复会一跳一跳的): the page looked again every 2 seconds and drew everything anew -- open
  // parts closed, the page jumped. Now nothing is redrawn unless it changed, and a redraw keeps what was open and
  // where the page was scrolled.
  function keepView(box, draw) {
    const open = new Set([...box.querySelectorAll("details[open]")].map((d) => d.dataset.key));
    const y = window.scrollY;
    box.style.minHeight = box.offsetHeight + "px";
    draw();
    box.querySelectorAll("details").forEach((d) => { if (open.has(d.dataset.key)) d.open = true; });
    box.style.minHeight = "";
    window.scrollTo(0, y);
  }

  function renderList() {
    const list = $("#flow-list");
    const sig = JSON.stringify([state.selected, state.runs.map((r) => [r.id, r.running, r.error, r.title, r.trigger])]);
    if (sig === state.listSig) return;
    state.listSig = sig;
    if (!state.runs.length) {
      list.replaceChildren(h("p", { class: "note", text: "Violet 启动以来还没有处理过消息。给她发一句话，这里就会出现。" }));
      return;
    }
    list.replaceChildren(...state.runs.map((r) => {
      const [label, kind] = r.running ? ["进行中", "warn"] : r.error ? ["出错", "bad"] : ["完成", "ok"];
      return h("button", { type: "button", class: "flow-item" + (r.id === state.selected ? " active" : ""), onclick: () => { state.selected = r.id; state.node = null; renderList(); renderRun(); } },
        h("div", { class: "row" }, h("strong", { text: TYPE[r.pipeline_type] || r.pipeline_type }), pill(label, kind),
          h("span", { class: "note", text: clock(r.started_at) })),
        h("div", { class: "flow-trigger", text: (r.title ? r.title + " · " : "") + (r.trigger || "") }));
    }));
  }

  async function renderRun() {
    const box = $("#flow-run");
    if (state.selected === null) { box.replaceChildren(); return; }
    let run;
    try { run = await api("GET", "/api/control/flow/" + state.selected); } catch (e) { box.replaceChildren(h("p", { class: "err", text: e.message })); return; }
    state.detail = run;
    const order = run.order.filter((n) => run.nodes[n]);
    if (!state.node || !run.nodes[state.node]) {
      state.node = run.active_node || [...order].reverse().find((n) => ["done", "failed"].includes(run.nodes[n].status)) || order[0];
    }
    const sig = JSON.stringify([state.selected, state.node, run]);
    if (sig === state.runSig) return;
    state.runSig = sig;
    const steps = h("div", { class: "flow-steps" }, ...order.flatMap((n, i) => {
      const node = run.nodes[n];
      const [label, kind] = STATUS[node.status] || [node.status, ""];
      const [title, sub] = NODE[n] || [n, ""];
      const step = h("button", { type: "button", class: `flow-step ${node.status}${n === state.node ? " active" : ""}`,
        onclick: () => { state.node = n; renderRun(); } },
        h("div", { class: "flow-step-head" }, h("strong", { text: title }), pill(label, kind)),
        h("div", { class: "note", text: node.note || sub }),
        h("div", { class: "note", text: secs(node.started_at, node.finished_at) }));
      return i ? [h("div", { class: "flow-arrow", "aria-hidden": "true" }), step] : [step];
    }));
    const head = h("div", { class: "row" },
      h("h3", { text: TYPE[run.pipeline_type] || run.pipeline_type }),
      run.running ? pill("进行中", "warn") : run.error ? pill("出错", "bad") : pill("完成", "ok"),
      h("span", { class: "note", text: `${clock(run.started_at)}${run.finished_at ? " · 用时 " + secs(run.started_at, run.finished_at) : ""}` +
        (run.turn_id ? ` · 对话编号 ${run.turn_id}` : "") }));
    keepView(box, () => box.replaceChildren(...[head, h("p", { class: "flow-trigger", text: (run.title ? run.title + " · " : "") + (run.trigger || "") }),
      run.error ? h("p", { class: "err", text: run.error }) : null, steps, nodeDetail(run, state.node)].filter(Boolean)));  // replaceChildren prints "null"
  }

  // what one step got and gave: the model conversation as blocks, everything else as readable JSON
  function nodeDetail(run, name) {
    const node = run.nodes[name];
    if (!node) return null;
    const [title] = NODE[name] || [name];
    const parts = [h("h3", { text: title })];
    if (node.status === "skipped") parts.push(h("p", { class: "note", text: "这一步跳过了" + (node.note ? "：" + node.note : "。") }));
    for (const [label, value] of [["输入", node.input], ["输出", node.output]]) {
      if (value === null || value === undefined) continue;
      parts.push(h("h4", { text: label }), ...valueView(value, name + ":" + label));
    }
    if (parts.length === 1) parts.push(h("p", { class: "note", text: node.status === "running" ? "正在进行，结果出来后会显示在这里。" : "这一步没有记录内容。" }));
    return h("div", { class: "card flow-detail" }, ...parts);
  }

  function valueView(value, key) {
    if (typeof value !== "object" || value === null) return [h("pre", { class: "prompt-part", text: String(value) })];
    const out = [];
    const rest = {};
    for (const [k, v] of Object.entries(value)) {
      if (k === "messages" && Array.isArray(v)) out.push(messagesView(v, key));
      else if (k === "content" && typeof v === "string") out.push(h("div", { class: "flow-content" }, h("div", { class: "note", text: "结果" }), h("pre", { class: "prompt-part", text: v || "（空）" })));
      else if (k === "usage" && v) out.push(h("p", { class: "note", text: `token：输入 ${v.prompt_tokens ?? "—"}，输出 ${v.completion_tokens ?? "—"}` +
        ((v.prompt_tokens_details || {}).cached_tokens ? `，命中缓存 ${v.prompt_tokens_details.cached_tokens}` : "") }));
      else if (v !== null && v !== undefined && v !== "") rest[k] = v;
    }
    if (Object.keys(rest).length) out.unshift(h("pre", { class: "prompt-part", text: JSON.stringify(rest, null, 2) }));
    return out;
  }

  function messagesView(messages, key) {
    const chars = messages.reduce((n, m) => n + String(m.content || "").length, 0);
    const d = h("details", { class: "flow-messages" }, h("summary", { text: `发给模型的对话：${messages.length} 条 · ${chars} 字` }),
      ...messages.map((m, i) => {
        const calls = (m.tool_calls || []).map((c) => `调用 ${(c.function || {}).name || "?"}(${(c.function || {}).arguments || ""})`).join("\n");
        const text = [m.content, calls].filter(Boolean).join("\n");
        const one = h("details", {}, h("summary", { text: `${ROLE[m.role] || m.role} · ${String(text).length} 字 · ${String(text).slice(0, 40).replace(/\s+/g, " ")}` }),
          h("pre", { class: "prompt-part", text: text || "（空）" }));
        one.dataset.key = key + ":" + i;
        return one;
      }));
    d.dataset.key = key;
    return d;
  }

  function watch() {
    clearTimeout(state.timer);
    state.timer = setTimeout(async () => {
      if ($("#tab-flow").classList.contains("active") && !document.hidden && $("#flow-live").checked) {
        await refresh();          // redraws only what changed, keeping open parts and the scroll position
      }
      if ($("#tab-flow").classList.contains("active")) watch();
    }, 2000);
  }

  loaders.flow = async () => { await refresh(); watch(); };
  $("#flow-refresh").addEventListener("click", refresh);
})();
