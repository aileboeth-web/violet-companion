// Violet Control Center: timetable page (phase 9.4.3). Uses control.js's helpers ($, h, api, toast, pill, fmt, when,
// confirmBox, loaders). Everything is built with DOM nodes and textContent; data is never parsed as HTML. The
// checks here are a convenience only -- the server validates every field and is what counts.
"use strict";
(() => {
  const S = { overview: null, termId: null, detail: null, week: null, weekData: null, view: "week", historyBefore: null, preview: null, previewFile: null };
  const WD = ["", "周一", "周二", "周三", "周四", "周五", "周六", "周日"];
  const TERM_STATE = { active: "进行中", not_started: "还没开始", ended: "已结束", archived: "已归档" };
  const COURSE_KIND = { lecture: "理论", lab: "实验", seminar: "研讨", pe: "体育", other: "其他" };
  const EXAM_KIND = { final: "期末", midterm: "期中", quiz: "小测", other: "其他" };
  const HOLIDAY_KIND = { holiday: "放假", no_class: "停课日", makeup: "调休补课日" };
  const EXC_KIND = { cancel: "停课", move: "调课", time: "改时间", room: "换教室", extra: "加课" };
  const OCC_STATUS = { normal: "照常", changed: "有变动", moved_in: "调入", moved_out: "已调走", cancelled: "停课", extra: "加课", exam: "考试", exam_cancelled: "考试已取消", invalid_time: "时间没设置好" };
  const ACTION = { create: "新增", update: "修改", delete: "删除 / 停用", restore: "恢复", import_skip: "导入时跳过（手动优先）" };
  const ENTITY = { term: "学期", period: "节次表", course: "课程", slot: "上课时间", exception: "变动", exam: "考试", holiday: "假期", import: "导入" };
  const IMPORT_ACTION = { created: ["新增", "ok"], updated: ["修改", "warn"], deleted: ["删除", "bad"], unchanged: ["不变", ""], skipped_manual: ["手动优先，不覆盖", "warn"], update: ["替换", "warn"] };
  const FIELD = { name: "名称", kind: "类型", code: "课程代码", teacher: "老师", notes: "备注", weekday: "星期", weeks: "周次", start: "开始", end: "结束",
    period_start: "开始节次", period_end: "结束节次", location: "地点", valid_from: "生效日期", valid_until: "失效日期", status: "状态", start_date: "开始日期",
    end_date: "结束日期", week1_monday: "第一周周一", timezone: "时区", exam_date: "日期", seat: "座位号", course_id: "课程", on_date: "日期",
    new_date: "新日期", new_start: "新开始", new_end: "新结束", new_start_time: "新开始", new_end_time: "新结束", new_period_start: "新开始节次",
    new_period_end: "新结束节次", new_location: "新地点", reason: "原因", follows_date: "按哪天的课表", start_time: "开始", end_time: "结束",
    weeks_text: "周次", parity: "单双周", week_from: "起始周", week_to: "结束周", skip_weeks: "跳过的周", slot_id: "上课时间" };
  const base = "/api/control/schedule";
  const hhmm = (iso) => (iso ? String(iso).slice(11, 16) : "");

  // ---------------------------------------------------------------- form dialog
  // fields: [{name, label, type: text|date|time|number|select|textarea, options: [[value, label]], value, help, required}]
  function formDialog(title, fields, onSubmit, intro) {
    const d = $("#sch-form"), box = $("#sch-form-fields"), err = $("#sch-form-error"), okBtn = $("#sch-form-ok");
    $("#sch-form-title").textContent = title; $("#sch-form-intro").textContent = intro || ""; err.textContent = "";
    const inputs = {};
    box.replaceChildren(...fields.map((f) => {
      let input;
      if (f.type === "select") {
        input = h("select", {}, ...(f.options || []).map(([v, l]) => h("option", { value: v, text: l })));
        input.value = f.value === null || f.value === undefined ? (f.options && f.options[0] ? f.options[0][0] : "") : String(f.value);
      } else if (f.type === "textarea") {
        input = h("textarea", { rows: 3, maxlength: f.max || 500 }); input.value = f.value ?? "";
      } else {
        input = h("input", { type: f.type || "text", maxlength: f.max || (f.type ? null : 120), min: f.min ?? null, max: f.type === "number" ? f.maxValue ?? null : null,
          placeholder: f.placeholder || null, required: f.required || null });
        input.value = f.value ?? "";
      }
      inputs[f.name] = { input, f };
      return h("label", { class: "field" }, h("span", { text: f.label + (f.required ? " *" : "") }), input,
        f.help ? h("span", { class: "help", text: f.help }) : null, h("span", { class: "err", "data-sch-err": f.name }));
    }));
    const collect = () => {
      const out = {};
      for (const [name, { input, f }] of Object.entries(inputs)) {
        const raw = input.value.trim();
        if (f.type === "number") out[name] = raw === "" ? null : Number(raw);
        else out[name] = raw === "" ? null : raw;
      }
      return out;
    };
    const submit = async (ev) => {
      ev.preventDefault();
      box.querySelectorAll("[data-sch-err]").forEach((e) => (e.textContent = "")); err.textContent = "";
      okBtn.disabled = true;
      try { await onSubmit(collect()); d.close("done"); }
      catch (e) {
        const problems = (e.data && e.data.problems) || {};
        const rest = [];
        for (const [k, v] of Object.entries(problems)) {
          const slot = box.querySelector(`[data-sch-err="${CSS.escape(k)}"]`);
          if (slot) slot.textContent = v; else rest.push(`${FIELD[k] || k}：${v}`);
        }
        err.textContent = e.message + (rest.length ? "：" + rest.join("；") : "");
      } finally { okBtn.disabled = false; }
    };
    $("#sch-form-form").onsubmit = submit;                 // the save button and Enter in a field
    $("#sch-form-cancel").onclick = () => d.close("cancel");
    d.returnValue = ""; d.showModal();
  }
  const opts = (map) => Object.entries(map);

  // ---------------------------------------------------------------- loading
  async function loadOverview() {
    S.overview = await api("GET", base);
    const o = S.overview, sel = $("#sch-term");
    const banner = [];
    if (!o.enabled) banner.push("Violet 现在不会查课表（.env 的 VIOLET_SCHEDULE_ENABLED 没有打开）；这里仍然可以录入和整理。");
    if (o.server_disabled) banner.push("工具页里停用了 violet-schedule，Violet 现在不会查课表。");
    if (o.tools_disabled && o.tools_disabled.length) banner.push("工具页里停用了：" + o.tools_disabled.join("、") + "。");
    if (!o.terms.length) banner.push("还没有课表：先新建一个学期，或在“导入 / 导出”里导入 JSON。");
    else if (!o.terms.some((t) => t.state === "active")) banner.push("今天不在任何进行中的学期里（Violet 会说课表还没开始或已经结束）。如果新学期开始了，请录入新学期。");
    $("#sch-banner").textContent = banner.join(" ");
    const keep = S.termId;
    sel.replaceChildren(...o.terms.map((t) => h("option", { value: t.id, text: `${t.name}（${TERM_STATE[t.state] || t.state}）` })));
    S.termId = o.terms.some((t) => t.id === keep) ? keep : o.current_term_id;
    if (S.termId) sel.value = String(S.termId);
    ["#sch-term-edit", "#sch-term-archive"].forEach((id) => ($(id).disabled = !S.termId));
  }
  async function loadTerm() {
    S.detail = S.termId ? await api("GET", `${base}/terms/${S.termId}`) : null;
    const info = $("#sch-term-info");
    if (!S.detail) { info.replaceChildren(); return; }
    const t = S.detail.term;
    info.replaceChildren(...[pill(TERM_STATE[t.state] || t.state, t.state === "active" ? "ok" : (t.state === "ended" || t.state === "archived" ? "warn" : "")),
      pill(`${t.start_date} – ${t.end_date}`), pill(`第一周周一 ${t.week1_monday}`), pill(`共 ${t.weeks_total} 周`),
      t.current_week ? pill(`今天第 ${t.current_week} 周（${t.current_week % 2 ? "单" : "双"}周）`) : null, pill(`时区 ${t.timezone}`),
      pill(`来源 ${t.source === "import" ? "导入" : "手动"}`), S.overview && S.overview.last_updated ? pill(`最后修改 ${when(S.overview.last_updated)}`) : null,
      S.detail.conflicts.length ? pill(`${S.detail.conflicts.length} 处时间冲突`, "bad") : null].filter(Boolean));   // replaceChildren would print "null"
    $("#sch-term-archive").textContent = t.status === "archived" ? "取消归档" : "归档";
  }
  async function refresh(keepWeek = true) {
    try {
      await loadOverview(); await loadTerm();
      if (!keepWeek) S.week = null;
      await showView(S.view);
    } catch (e) { toast("课表读取失败：" + e.message, false); }
  }
  loaders.schedule = () => refresh(true);

  async function showView(view) {
    S.view = view;
    document.querySelectorAll("#sch-views button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
    document.querySelectorAll(".sch-view").forEach((v) => v.classList.toggle("active", v.id === "sch-view-" + view));
    const render = { week: renderWeek, courses: renderCourses, periods: renderPeriods, events: renderEvents, import: renderImport, history: () => renderHistory(false), delete: renderDelete }[view];
    if (render) await render();
  }
  document.querySelectorAll("#sch-views button").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view).catch((e) => toast(e.message, false))));
  $("#sch-term").addEventListener("change", (ev) => { S.termId = Number(ev.target.value); S.week = null; refresh(true); });

  // ---------------------------------------------------------------- terms
  function termFields(t) {
    return [{ name: "name", label: "名称", value: t && t.name, required: true, max: 60, placeholder: "例如 2026 秋" },
      { name: "start_date", label: "开学日期", type: "date", value: t && t.start_date, required: true },
      { name: "end_date", label: "结束日期", type: "date", value: t && t.end_date, required: true },
      { name: "week1_monday", label: "第一周的星期一", type: "date", value: t && t.week1_monday, help: "留空 = 开学那一周的星期一；第几周、单双周都从这一天算" },
      { name: "timezone", label: "学期时区", value: (t && t.timezone) || "Asia/Shanghai", required: true, max: 64, help: "IANA 名称，例如 Asia/Shanghai；上课时间都是这个时区的钟点" }];
  }
  $("#sch-term-new").addEventListener("click", () => formDialog("新建学期", termFields(null), async (v) => {
    const r = await api("POST", `${base}/terms`, v); S.termId = r.id; toast("学期已创建"); refresh(false);
  }));
  $("#sch-term-edit").addEventListener("click", () => {
    if (!S.detail) return;
    const t = S.detail.term;
    formDialog("编辑学期", termFields(t), async (v) => {
      const changes = {}; for (const [k, val] of Object.entries(v)) if ((val || null) !== (t[k] || null)) changes[k] = val;
      if (!Object.keys(changes).length) return;
      await api("POST", `${base}/terms/${t.id}`, changes); toast("已保存"); refresh(true);
    });
  });
  $("#sch-term-archive").addEventListener("click", async () => {
    if (!S.detail) return;
    const t = S.detail.term;
    try {
      if (t.status === "archived") { await api("POST", `${base}/terms/${t.id}`, { status: "active" }); toast("已取消归档"); }
      else {
        if (!(await confirmBox(`归档“${t.name}”？归档后 Violet 查课表时不再用这个学期（数据保留，可以取消归档）。`))) return;
        await api("POST", `${base}/terms/${t.id}/archive`, {}); toast("已归档");
      }
      refresh(true);
    } catch (e) { toast("操作失败：" + e.message, false); }
  });

  // ---------------------------------------------------------------- week view
  async function renderWeek() {
    const grid = $("#sch-week-grid");
    if (!S.termId) { grid.replaceChildren(h("p", { class: "note", text: "还没有学期" })); $("#sch-conflicts").replaceChildren(); return; }
    const q = S.week ? `?week=${S.week}` : "";
    const w = S.weekData = await api("GET", `${base}/terms/${S.termId}/week${q}`);
    S.week = w.week;
    const sel = $("#sch-week");
    sel.replaceChildren(...Array.from({ length: w.weeks_total }, (_, i) => h("option", { value: i + 1, text: `第 ${i + 1} 周（${(i + 1) % 2 ? "单" : "双"}）` })));
    sel.value = String(w.week);
    $("#sch-prev").disabled = w.week <= 1; $("#sch-next").disabled = w.week >= w.weeks_total;
    const conflicts = (S.detail && S.detail.conflicts) || [];
    $("#sch-conflicts").replaceChildren(...(conflicts.length ? [h("div", { class: "card warnbox" }, h("strong", { text: "时间冲突（整个学期）" }),
      ...conflicts.slice(0, 8).map((c) => h("div", { class: "note", text: `${WD[c.weekday]} ${c.a}（${c.a_time}）和 ${c.b}（${c.b_time}）重叠，共 ${c.count} 次，最早 ${c.first_date}` })))] : []));
    const today = S.overview && S.overview.today;
    grid.replaceChildren(...w.days.map((d) => {
      const head = h("div", { class: "day-head" }, h("strong", { text: `${WD[d.weekday]} ${d.date.slice(5)}` }),
        d.date === today ? pill("今天", "ok") : null,
        d.data_state !== "active" ? pill("不在学期内") : null,
        d.day_kind !== "regular" ? pill(`${HOLIDAY_KIND[d.day_kind] || d.day_kind}${d.day_name ? "：" + d.day_name : ""}`, "warn") : null,
        d.follows_date ? pill(`按 ${d.follows_date} 的课表`) : null,
        d.data_state === "active" ? h("button", { class: "ghost mini", text: "加课", onclick: () => addExtra(d.date) }) : null);
      const items = d.items.map((i) => occCard(i));
      return h("div", { class: "day-col" + (d.date === today ? " today" : "") }, head,
        ...(items.length ? items : [h("div", { class: "note", text: d.data_state === "active" ? "没有课" : "" })]),
        ...(d.notes || []).map((n) => h("div", { class: "note", text: n })));
    }));
  }
  function occCard(i) {
    const cls = ["occ", i.status, i.kind === "exam" ? "exam" : "", i.conflict_with.length ? "conflict" : ""].join(" ");
    const time = i.start ? `${hhmm(i.start)}–${hhmm(i.end)}` : "时间未定";
    return h("button", { class: cls, type: "button", onclick: () => occActions(i) },
      h("span", { class: "occ-time", text: time }), h("span", { class: "occ-name", text: i.course }),
      i.location ? h("span", { class: "occ-place", text: i.location }) : null,
      i.status !== "normal" ? h("span", { class: "occ-status", text: OCC_STATUS[i.status] || i.status }) : null,
      i.change_note && i.status !== "cancelled" && i.change_note !== OCC_STATUS[i.status] ? h("span", { class: "occ-place", text: i.change_note }) : null,
      i.conflict_with.length ? h("span", { class: "occ-status", text: "冲突" }) : null);
  }
  function courseOf(id) { return ((S.detail && S.detail.courses) || []).find((c) => c.id === id); }
  function occActions(i) {
    if (i.kind === "exam") { const x = (S.detail.exams || []).find((e) => e.id === i.exam_id); if (x) examForm(x); return; }
    const course = courseOf(i.course_id);
    const choices = [];
    if (i.slot_id && !["cancelled", "moved_out"].includes(i.status)) {
      for (const k of ["cancel", "move", "time", "room"]) choices.push([k, EXC_KIND[k]]);
    }
    for (const id of i.exception_ids || []) choices.push(["revoke:" + id, "撤销变动 #" + id]);
    if (!choices.length) { toast("这一节没有可以做的操作", false); return; }
    formDialog(`${i.course} · ${i.date} ${i.start ? hhmm(i.start) : ""}`, [{ name: "what", label: "要做什么", type: "select", options: choices }], async (v) => {
      if (v.what.startsWith("revoke:")) { await api("POST", `${base}/exceptions/${v.what.slice(7)}/revoke`, {}); toast("已撤销"); refresh(true); return; }
      setTimeout(() => exceptionForm(course, v.what, i.nominal_date || i.date, i.slot_id), 0);
    }, OCC_STATUS[i.status] + (i.change_note ? "：" + i.change_note : ""));
  }
  function exceptionForm(course, kind, onDate, slotId) {
    const fields = [{ name: "reason", label: "原因（可选）", max: 200 }];
    if (kind === "move") fields.unshift({ name: "new_date", label: "调到哪天", type: "date", value: onDate, help: "只改时间、不改日期就保持原日期" },
      { name: "new_start", label: "新开始", type: "time" }, { name: "new_end", label: "新结束", type: "time" },
      { name: "new_period_start", label: "或：新开始节次", type: "number", min: 1, maxValue: 20 }, { name: "new_period_end", label: "新结束节次", type: "number", min: 1, maxValue: 20 },
      { name: "new_location", label: "新地点（可选）", max: 60 });
    if (kind === "time") fields.unshift({ name: "new_start", label: "新开始", type: "time" }, { name: "new_end", label: "新结束", type: "time" },
      { name: "new_period_start", label: "或：新开始节次", type: "number", min: 1, maxValue: 20 }, { name: "new_period_end", label: "新结束节次", type: "number", min: 1, maxValue: 20 });
    if (kind === "room") fields.unshift({ name: "new_location", label: "新地点", required: true, max: 60 });
    if (kind === "extra") fields.unshift({ name: "new_start", label: "开始", type: "time" }, { name: "new_end", label: "结束", type: "time" },
      { name: "new_period_start", label: "或：开始节次", type: "number", min: 1, maxValue: 20 }, { name: "new_period_end", label: "结束节次", type: "number", min: 1, maxValue: 20 },
      { name: "new_location", label: "地点（可选）", max: 60 });
    formDialog(`${EXC_KIND[kind]}：${course ? course.name : ""} ${onDate}`, fields, async (v) => {
      const bodyData = { kind, on_date: onDate, slot_id: kind === "extra" ? null : slotId, ...v };
      if (kind === "move" && bodyData.new_date === onDate && (bodyData.new_start || bodyData.new_period_start)) bodyData.new_date = null;
      await api("POST", `${base}/courses/${course.id}/exceptions`, bodyData);
      toast(`已记录${EXC_KIND[kind]}`); refresh(true);
    }, ["move", "time", "extra"].includes(kind) ? "时刻和节次只填一种。" : "");
  }
  function addExtra(day) {
    const courses = ((S.detail && S.detail.courses) || []).filter((c) => c.status === "active");
    if (!courses.length) return toast("先新增课程", false);
    formDialog(`加课：${day}`, [{ name: "course", label: "哪门课", type: "select", options: courses.map((c) => [c.id, c.name]) }], async (v) => {
      setTimeout(() => exceptionForm(courseOf(Number(v.course)), "extra", day, null), 0);
    });
  }
  $("#sch-prev").addEventListener("click", () => { S.week = Math.max(1, (S.week || 1) - 1); renderWeek().catch((e) => toast(e.message, false)); });
  $("#sch-next").addEventListener("click", () => { S.week = (S.week || 1) + 1; renderWeek().catch((e) => toast(e.message, false)); });
  $("#sch-this").addEventListener("click", () => { S.week = null; renderWeek().catch((e) => toast(e.message, false)); });
  $("#sch-week").addEventListener("change", (ev) => { S.week = Number(ev.target.value); renderWeek().catch((e) => toast(e.message, false)); });

  // ---------------------------------------------------------------- courses and slots
  function periodText(s) {
    if (!s.period_start) return `${s.start_time}–${s.end_time}`;
    const p = (S.detail.periods || []), a = p.find((x) => x.period_no === s.period_start), b = p.find((x) => x.period_no === s.period_end);
    return `第 ${s.period_start}–${s.period_end} 节` + (a && b ? `（${a.start_time}–${b.end_time}）` : "（节次时间没设置）");
  }
  function renderCourses() {
    const list = $("#sch-courses");
    if (!S.detail) { list.replaceChildren(h("p", { class: "note", text: "还没有学期" })); return; }
    const courses = S.detail.courses;
    list.replaceChildren(...(courses.length ? courses.map(courseCard) : [h("p", { class: "note", text: "这个学期还没有课程" })]));
  }
  function sourcePills(row) {
    return [pill(row.source === "import" ? "导入" : "手动"), row.manual_override ? pill("手动改过（再导入不覆盖）", "warn") : null];
  }
  function courseCard(c) {
    const active = c.status === "active";
    const slots = c.slots.map((s) => h("div", { class: "row slot-row" + (s.status !== "active" ? " muted" : "") },
      h("span", { text: `${WD[s.weekday]} ${periodText(s)} · ${s.weeks_text || ""}周${s.location ? " · " + s.location : ""}${s.valid_from || s.valid_until ? ` · 有效 ${fmt(s.valid_from)}–${fmt(s.valid_until)}` : ""}` }),
      h("span", { class: "badges" }, ...sourcePills(s), s.status !== "active" ? pill("已删除") : null,
        s.status === "active" ? h("button", { class: "ghost mini", text: "编辑", onclick: () => slotForm(c, s) }) : null,
        s.status === "active" ? h("button", { class: "ghost mini", text: "删除", onclick: () => deleteSlot(s) }) : null)));
    return h("div", { class: "card" + (active ? "" : " muted") },
      h("div", { class: "row" }, h("strong", { text: c.name }),
        h("span", { class: "badges" }, pill(COURSE_KIND[c.kind] || c.kind), c.code ? pill(c.code) : null, ...sourcePills(c), active ? null : pill("已停用", "warn"))),
      c.teacher || c.notes ? h("div", { class: "note", text: [c.teacher ? "老师：" + c.teacher : "", c.notes ? "备注：" + c.notes : ""].filter(Boolean).join("　") }) : null,
      ...slots,
      h("div", { class: "row", style: "justify-content:flex-end;margin-top:6px" },
        active ? h("button", { class: "ghost", text: "新增上课时间", onclick: () => slotForm(c, null) }) : null,
        h("button", { class: "ghost", text: "编辑", onclick: () => courseForm(c) }),
        h("button", { class: "ghost", text: active ? "停用" : "恢复", onclick: () => toggleCourse(c) }),
        h("button", { class: "danger", text: "彻底删除", onclick: () => purgeCourse(c) })));
  }
  function courseForm(c) {
    formDialog(c ? "编辑课程" : "新增课程", [
      { name: "name", label: "课程名", value: c && c.name, required: true, max: 60 },
      { name: "kind", label: "类型", type: "select", options: opts(COURSE_KIND), value: c ? c.kind : "lecture" },
      { name: "code", label: "课程代码", value: c && c.code, max: 40 },
      { name: "teacher", label: "老师", value: c && c.teacher, max: 40, help: "个人信息：只在对方直接问到时才会告诉 Violet，不会进联网搜索和日志" },
      { name: "notes", label: "备注", value: c && c.notes, max: 500, help: "备注不会提供给 Violet" }],
    async (v) => {
      if (c) { await api("POST", `${base}/courses/${c.id}`, v); toast("已保存"); }
      else { await api("POST", `${base}/terms/${S.termId}/courses`, v); toast("课程已新增，接着添加上课时间"); }
      refresh(true);
    }, c && c.source === "import" ? "这是导入的课程：手动修改后，以后再导入不会覆盖它。" : "");
  }
  $("#sch-course-new").addEventListener("click", () => (S.detail ? courseForm(null) : toast("先新建学期", false)));
  function slotForm(c, s) {
    formDialog(`${s ? "编辑" : "新增"}上课时间：${c.name}`, [
      { name: "weekday", label: "星期", type: "select", options: WD.slice(1).map((n, i) => [i + 1, n]), value: s ? s.weekday : 1 },
      { name: "period_start", label: "开始节次", type: "number", min: 1, maxValue: 20, value: s && s.period_start },
      { name: "period_end", label: "结束节次", type: "number", min: 1, maxValue: 20, value: s && s.period_end },
      { name: "start", label: "或：开始时刻", type: "time", value: s && s.start_time },
      { name: "end", label: "结束时刻", type: "time", value: s && s.end_time, help: "节次和时刻只填一种；同一天内，结束晚于开始" },
      { name: "weeks", label: "周次", value: s ? s.weeks_text : "1-16", required: true, max: 60, help: "例如 1-16、1-16单、2-16双、1-8,10-16（跳过第 9 周）、1,3,5" },
      { name: "location", label: "地点", value: s && s.location, max: 60 },
      { name: "valid_from", label: "从哪天起有效（可选）", type: "date", value: s && s.valid_from },
      { name: "valid_until", label: "到哪天为止（可选）", type: "date", value: s && s.valid_until }],
    async (v) => {
      const r = s ? await api("POST", `${base}/slots/${s.id}`, v) : await api("POST", `${base}/courses/${c.id}/slots`, v);
      await refresh(true);
      const clash = ((S.detail && S.detail.conflicts) || []).filter((x) => x.a === c.name || x.b === c.name);
      toast(clash.length ? `已保存，但和其他课时间冲突 ${clash.length} 处（见周视图上方）` : "已保存", !clash.length);
      return r;
    });
  }
  async function deleteSlot(s) {
    if (!(await confirmBox("删除这个上课时间？它的调课停课也不再生效（修改历史里保留记录）。"))) return;
    try { await api("POST", `${base}/slots/${s.id}/delete`, {}); toast("已删除"); refresh(true); } catch (e) { toast("删除失败：" + e.message, false); }
  }
  async function toggleCourse(c) {
    try {
      if (c.status === "active") {
        if (!(await confirmBox(`停用“${c.name}”？它不再出现在课表里，Violet 也查不到；可以随时恢复。`))) return;
        await api("POST", `${base}/courses/${c.id}/disable`, {}); toast("已停用");
      } else { await api("POST", `${base}/courses/${c.id}/restore`, {}); toast("已恢复"); }
      refresh(true);
    } catch (e) { toast("操作失败：" + e.message, false); }
  }
  async function purgeCourse(c) {
    if (!(await confirmBox(`彻底删除“${c.name}”？课程、它的 ${c.slots.length} 个上课时间、调课停课，以及修改历史里的旧副本（老师、地点、备注原文）都会删除，不能恢复。考试会保留但不再关联这门课。`))) return;
    try { const r = await api("POST", `${base}/courses/${c.id}/purge`, { confirm: true }); toast(`已彻底删除（${Object.values(r.deleted).reduce((a, b) => a + b, 0)} 行）`); refresh(true); }
    catch (e) { toast("删除失败：" + e.message, false); }
  }

  // ---------------------------------------------------------------- periods
  function renderPeriods() {
    const table = $("#sch-periods");
    const rows = S.detail ? S.detail.periods.map((p) => ({ ...p })) : [];
    S.periodRows = rows;
    const draw = () => table.replaceChildren(h("tr", {}, h("th", { text: "第几节" }), h("th", { text: "开始" }), h("th", { text: "结束" }), h("th", { text: "" })),
      ...S.periodRows.map((p, idx) => h("tr", {},
        h("td", {}, h("input", { type: "number", min: 1, max: 20, value: p.period_no ?? "", oninput: (e) => (p.period_no = Number(e.target.value)) })),
        h("td", {}, h("input", { type: "time", value: p.start_time || "", oninput: (e) => (p.start_time = e.target.value) })),
        h("td", {}, h("input", { type: "time", value: p.end_time || "", oninput: (e) => (p.end_time = e.target.value) })),
        h("td", {}, h("button", { class: "ghost mini", text: "删除", onclick: () => { S.periodRows.splice(idx, 1); draw(); } })))));
    S.drawPeriods = draw; draw();
  }
  $("#sch-period-add").addEventListener("click", () => {
    if (!S.detail) return toast("先新建学期", false);
    const last = S.periodRows[S.periodRows.length - 1];
    S.periodRows.push({ period_no: last ? last.period_no + 1 : 1, start_time: "", end_time: "" }); S.drawPeriods();
  });
  $("#sch-period-save").addEventListener("click", async () => {
    if (!S.detail) return;
    try { await api("PUT", `${base}/terms/${S.termId}/periods`, { periods: S.periodRows }); toast("节次表已保存"); refresh(true); }
    catch (e) { const p = (e.data && e.data.problems) || {}; toast(e.message + "：" + Object.values(p).join("；"), false); }
  });

  // ---------------------------------------------------------------- exams, holidays, exceptions
  function renderEvents() {
    const d = S.detail;
    if (!d) { ["#sch-exams", "#sch-holidays", "#sch-exceptions"].forEach((id) => $(id).replaceChildren()); return; }
    $("#sch-exams").replaceChildren(...(d.exams.length ? d.exams.map((x) => h("div", { class: "card" + (x.status === "cancelled" ? " muted" : "") },
      h("div", { class: "row" }, h("strong", { text: x.name }), h("span", { class: "badges" }, pill(EXAM_KIND[x.kind] || x.kind), ...sourcePills(x), x.status === "cancelled" ? pill("已取消", "warn") : null)),
      h("div", { class: "note", text: `${x.exam_date} ${x.start_time ? x.start_time + "–" + x.end_time : "时间未定"}${x.course_id && courseOf(x.course_id) ? " · " + courseOf(x.course_id).name : ""}${x.location ? " · " + x.location : ""}${x.seat ? " · 座位 " + x.seat : ""}` }),
      h("div", { class: "row", style: "justify-content:flex-end" }, h("button", { class: "ghost mini", text: "编辑", onclick: () => examForm(x) }),
        h("button", { class: "ghost mini", text: x.status === "cancelled" ? "恢复" : "取消考试", onclick: () => examStatus(x, x.status === "cancelled" ? "active" : "cancelled") }),
        h("button", { class: "ghost mini", text: "删除", onclick: () => examStatus(x, "deleted") }))))
      : [h("p", { class: "note", text: "没有考试" })]));
    $("#sch-holidays").replaceChildren(...(d.holidays.length ? d.holidays.map((x) => h("div", { class: "card row" },
      h("span", { text: `${HOLIDAY_KIND[x.kind] || x.kind}：${x.name} ${x.start_date}${x.end_date !== x.start_date ? " – " + x.end_date : ""}${x.follows_date ? `（按 ${x.follows_date} 的课表上课）` : ""}` }),
      h("span", { class: "badges" }, ...sourcePills(x), h("button", { class: "ghost mini", text: "删除", onclick: () => deleteHoliday(x) }))))
      : [h("p", { class: "note", text: "没有假期或补课日" })]));
    $("#sch-exceptions").replaceChildren(...(d.exceptions.length ? d.exceptions.map((x) => h("div", { class: "card row" + (x.status !== "active" ? " muted" : "") },
      h("span", { text: `${EXC_KIND[x.kind] || x.kind}：${x.course_name} ${x.on_date}${x.new_date ? " → " + x.new_date : ""}${x.new_start_time ? ` ${x.new_start_time}–${x.new_end_time}` : ""}${x.new_period_start ? ` 第 ${x.new_period_start}–${x.new_period_end} 节` : ""}${x.new_location ? " · " + x.new_location : ""}${x.reason ? "（" + x.reason + "）" : ""}` }),
      h("span", { class: "badges" }, ...sourcePills(x), x.status === "active" ? h("button", { class: "ghost mini", text: "撤销", onclick: () => revokeException(x) }) : pill("已撤销"))))
      : [h("p", { class: "note", text: "没有调课停课" })]));
  }
  function examForm(x) {
    const courses = (S.detail.courses || []).filter((c) => c.status === "active");
    formDialog(x ? "编辑考试" : "新增考试", [
      { name: "name", label: "名称", value: x && x.name, required: true, max: 60 },
      { name: "kind", label: "类型", type: "select", options: opts(EXAM_KIND), value: x ? x.kind : "final" },
      { name: "exam_date", label: "日期", type: "date", value: x && x.exam_date, required: true },
      { name: "start", label: "开始（可选）", type: "time", value: x && x.start_time },
      { name: "end", label: "结束（可选）", type: "time", value: x && x.end_time },
      { name: "course_id", label: "对应课程（可选）", type: "select", options: [["", "—"], ...courses.map((c) => [c.id, c.name])], value: x && x.course_id ? x.course_id : "" },
      { name: "location", label: "考场", value: x && x.location, max: 60 }, { name: "seat", label: "座位号", value: x && x.seat, max: 20 },
      { name: "notes", label: "备注", value: x && x.notes, max: 500 }],
    async (v) => {
      v.course_id = v.course_id ? Number(v.course_id) : null;
      if (x) await api("POST", `${base}/exams/${x.id}`, v); else await api("POST", `${base}/terms/${S.termId}/exams`, v);
      toast("已保存"); refresh(true);
    });
  }
  $("#sch-exam-new").addEventListener("click", () => (S.detail ? examForm(null) : toast("先新建学期", false)));
  async function examStatus(x, status) {
    if (status === "deleted" && !(await confirmBox(`删除考试“${x.name}”？`))) return;
    try { await api("POST", `${base}/exams/${x.id}/status`, { status }); toast("已保存"); refresh(true); } catch (e) { toast("操作失败：" + e.message, false); }
  }
  $("#sch-holiday-new").addEventListener("click", () => {
    if (!S.detail) return toast("先新建学期", false);
    formDialog("新增假期 / 补课日", [
      { name: "kind", label: "类型", type: "select", options: opts(HOLIDAY_KIND) },
      { name: "name", label: "名称", required: true, max: 60, placeholder: "例如 国庆" },
      { name: "start_date", label: "开始日期", type: "date", required: true },
      { name: "end_date", label: "结束日期（可选）", type: "date", help: "留空 = 一天；补课日只能是一天" },
      { name: "follows_date", label: "按哪一天的课表上课（补课日才填）", type: "date", help: "例如国庆调休周六上周三的课，就填被调走的那个周三" }],
    async (v) => { await api("POST", `${base}/terms/${S.termId}/holidays`, v); toast("已保存"); refresh(true); },
    "放假和停课日只停常规课；加课和考试照常显示。");
  });
  async function deleteHoliday(x) {
    if (!(await confirmBox(`删除“${x.name}”？`))) return;
    try { await api("POST", `${base}/holidays/${x.id}/delete`, {}); toast("已删除"); refresh(true); } catch (e) { toast("删除失败：" + e.message, false); }
  }
  async function revokeException(x) {
    try { await api("POST", `${base}/exceptions/${x.id}/revoke`, {}); toast("已撤销"); refresh(true); } catch (e) { toast("撤销失败：" + e.message, false); }
  }

  // ---------------------------------------------------------------- import / export
  function renderImport() {
    const list = $("#sch-imports");
    const rows = (S.detail && S.detail.imports) || [];
    list.replaceChildren(...(rows.length ? rows.map((r) => {
      let summary = {}; try { summary = JSON.parse(r.summary || "{}"); } catch (_) { /* keep empty */ }
      const counts = ["created", "updated", "deleted", "unchanged", "skipped_manual"].filter((k) => summary[k]).map((k) => `${IMPORT_ACTION[k][0]} ${summary[k]}`).join("，");
      return h("div", { class: "card row" }, h("span", { text: `${when(r.applied_at || r.created_at)} · ${r.file_name} · ${r.format.toUpperCase()} · ${r.actor}` }),
        h("span", { class: "badges" }, pill({ applied: "已导入", failed: "失败（课表没改）", previewed: "预览", discarded: "放弃" }[r.status] || r.status, r.status === "applied" ? "ok" : (r.status === "failed" ? "bad" : "")), counts ? pill(counts) : null));
    }) : [h("p", { class: "note", text: "这个学期还没有导入过" })]));
  }
  async function readUpload() {
    const file = $("#sch-file").files[0];
    if (!file) throw new Error("先选择一个 .csv 或 .json 文件");
    const max = (S.overview && S.overview.limits.max_bytes) || 262144;
    if (file.size > max) throw new Error(`文件太大（最大 ${Math.round(max / 1024)} KB）`);
    const content = await file.text();
    const target = $("#sch-import-target").value;
    const upload = { file_name: file.name, content, term_id: target === "selected" ? S.termId : null };
    if (isCsv(file.name)) upload.mode = importMode();          // JSON has its own rule (no mode)
    return upload;
  }
  const isCsv = (name) => /\.csv$/i.test(name || "");
  const importMode = () => (document.querySelector('input[name="sch-import-mode"]:checked') || { value: "merge" }).value;
  // a preview belongs to the mode it was made with: changing the mode asks for a new preview
  for (const r of document.querySelectorAll('input[name="sch-import-mode"]')) {
    r.addEventListener("change", () => {
      if (!S.preview) return;
      S.preview = null;
      $("#sch-preview-box").replaceChildren(h("p", { class: "note", text: "导入方式改了，请重新预览" }));
    });
  }
  const MODE_TEXT = { merge: "追加 / 更新：只新增、更新文件里的课程，这个学期的其他课程保持不变",
    replace: "整学期替换：这个学期以前导入的课程里，文件中没有的会被删除", json: "JSON：文件里写了的部分整体替换（没写的部分不变）" };
  $("#sch-preview").addEventListener("click", async () => {
    const box = $("#sch-preview-box");
    try {
      const upload = await readUpload();
      const p = await api("POST", `${base}/import/preview`, upload);
      S.preview = p; S.previewFile = upload;
      box.replaceChildren(previewView(p));
    } catch (e) { box.replaceChildren(h("p", { class: "err", text: e.message })); }
  });
  function previewView(p) {
    const parts = [h("div", { class: "row" }, h("strong", { text: `${p.file_name}（${p.format.toUpperCase()}${p.rows ? "，" + p.rows + " 行" : ""}）→ ${p.term || "?"}${p.new_term ? "（新学期）" : ""}` }),
      h("span", { class: "badges" }, ...Object.entries(p.counts || {}).map(([k, n]) => pill(`${IMPORT_ACTION[k][0]} ${n}`, n ? IMPORT_ACTION[k][1] : ""))))];
    parts.push(h("p", { class: "note", id: "sch-preview-mode", text: "导入方式：" + (MODE_TEXT[p.mode] || p.mode || "") }));
    const gone = p.deleted_courses || [];
    if (gone.length) parts.push(h("div", { class: "card warnbox", id: "sch-preview-deletions" },
      h("strong", { text: `将删除 ${gone.length} 门课程（连同它们的上课时间）` }), h("div", { class: "note", text: gone.join("、") })));
    for (const w of p.warnings || []) parts.push(h("p", { class: "note", text: (w.line ? `第 ${w.line} 行：` : "") + w.message }));
    if (p.errors && p.errors.length) {
      parts.push(h("p", { class: "err", text: `有 ${p.errors.length} 个问题，改好文件后重新预览（这次不能导入）：` }),
        h("div", { class: "table-wrap" }, h("table", {}, h("tr", {}, h("th", { text: "位置" }), h("th", { text: "字段" }), h("th", { text: "问题" })),
          ...p.errors.map((e) => h("tr", {}, h("td", { text: e.line ? `第 ${e.line} 行` : (e.where || "文件") }), h("td", { text: fmt(e.field) }), h("td", { text: e.message }))))));
    }
    const shown = (p.items || []).filter((i) => i.action !== "unchanged");
    if (shown.length) {
      parts.push(h("div", { class: "table-wrap" }, h("table", {}, h("tr", {}, h("th", { text: "操作" }), h("th", { text: "对象" }), h("th", { text: "内容" }), h("th", { text: "说明" })),
        ...shown.map((i) => h("tr", {}, h("td", {}, pill((IMPORT_ACTION[i.action] || [i.action])[0], (IMPORT_ACTION[i.action] || [])[1])), h("td", { text: ENTITY[i.entity] || i.entity }),
          h("td", { text: i.label }), h("td", { text: i.reason || (i.fields ? "改了：" + i.fields.map((f) => FIELD[f] || f).join("、") : "") }))))));
    }
    // only new / changed / deleted rows are a change; rows kept because they were edited by hand are not
    const changes = ["created", "updated", "deleted"].reduce((n, k) => n + ((p.counts || {})[k] || 0), 0);
    if (!p.errors.length && !changes) parts.push(h("p", { class: "note", text: "和现在的课表相比没有任何变化" + (shown.length ? "（上面手动优先的记录保持不变）。" : "。") }));
    if (p.conflicts && p.conflicts.length) parts.push(h("div", { class: "card warnbox" }, h("strong", { text: "导入后的时间冲突" }),
      ...p.conflicts.map((c) => h("div", { class: "note", text: `${c.new ? "【新】" : ""}${WD[c.weekday]} ${c.a}（${c.a_time}）和 ${c.b}（${c.b_time}）重叠 ${c.count} 次` }))));
    const canGo = p.ok && !p.errors.length && changes > 0;
    parts.push(h("div", { class: "row", style: "justify-content:flex-end" },
      h("button", { text: "确认导入", disabled: !canGo, onclick: confirmImport })));
    return h("div", { class: "card" }, ...parts);
  }
  async function confirmImport() {
    const p = S.preview, upload = S.previewFile;
    if (!p || !upload) return;
    const gone = p.deleted_courses || [];
    const text = `按预览导入“${p.file_name}”（${MODE_TEXT[p.mode] || p.mode}）？新增 ${p.counts.created}、修改 ${p.counts.updated}、删除 ${p.counts.deleted}，手动改过的 ${p.counts.skipped_manual} 条保持不变。`
      + (gone.length ? `\n\n会删除 ${gone.length} 门课程：${gone.join("、")}` : "");
    if (!(await confirmBox(text))) return;
    try {
      // the confirm carries the mode and the counts it was previewed with: anything else is refused, nothing changes
      const r = await api("POST", `${base}/import/confirm`, { ...upload, mode: p.mode === "json" ? undefined : p.mode,
        sha256: p.sha256, data_version: p.data_version, expected_counts: p.counts });
      toast(`已导入：新增 ${r.created}、修改 ${r.updated}、删除 ${r.deleted}`);
      S.preview = null; $("#sch-preview-box").replaceChildren(); $("#sch-file").value = "";
      if (r.term_id) S.termId = r.term_id;
      refresh(false);
    } catch (e) {
      const stale = e.data && e.data.stale;
      toast((stale ? "" : "导入失败：") + e.message, false);
      if (stale) $("#sch-preview-box").replaceChildren(h("p", { class: "err", text: e.message }));
    }
  }
  $("#sch-export").addEventListener("click", () => {
    if (!S.termId) return toast("先选择学期", false);
    location.href = `${base}/export?` + new URLSearchParams({ term_id: S.termId, format: $("#sch-export-format").value, personal: $("#sch-export-personal").checked ? "1" : "0" });
  });

  // ---------------------------------------------------------------- history
  function diffText(r) {
    let before = null, after = null;
    try { before = r.before_json ? JSON.parse(r.before_json) : null; after = r.after_json ? JSON.parse(r.after_json) : null; } catch (_) { return []; }
    if (before && before.purged) {
      const n = Object.values(before.rows || {}).reduce((a, b) => a + b, 0);
      return [`彻底删除（${{ course: "一门课程", term: "这个学期", all: "全部课表" }[before.scope] || before.scope}），共 ${n} 行，原内容不保留`];
    }
    if (before && before.periods) return ["节次表：" + (after.periods || []).map((p) => `${p.period_no} ${p.start_time}–${p.end_time}`).join("，")];
    if (!before && after) return [after.name ? `新增：${after.name}` : "新增"];
    if (!before || !after) return [];
    return Object.keys(after).filter((k) => !["updated_at", "created_at", "manual_override", "import_id"].includes(k) && JSON.stringify(before[k]) !== JSON.stringify(after[k]))
      .map((k) => `${FIELD[k] || k}：${fmt(before[k])} → ${fmt(after[k])}`);
  }
  async function renderHistory(more) {
    const list = $("#sch-history");
    if (!S.termId) { list.replaceChildren(h("p", { class: "note", text: "还没有学期" })); return; }
    const q = more && S.historyBefore ? `?before_id=${S.historyBefore}` : "";
    const rows = await api("GET", `${base}/terms/${S.termId}/revisions${q}`);
    const nodes = rows.map((r) => h("div", { class: "card" },
      h("div", { class: "row" }, h("strong", { text: `${when(r.created_at)} · ${ACTION[r.action] || r.action} · ${ENTITY[r.entity] || r.entity} #${r.entity_id}` }),
        h("span", { class: "badges" }, pill(r.source === "import" ? "导入" : "手动"), pill(r.actor))),
      ...diffText(r).map((t) => h("div", { class: "note", text: t }))));
    if (more) list.append(...nodes); else list.replaceChildren(...(nodes.length ? nodes : [h("p", { class: "note", text: "还没有修改记录" })]));
    S.historyBefore = rows.length ? rows[rows.length - 1].id : null;
    $("#sch-history-more").hidden = rows.length < 100;
  }
  $("#sch-history-more").addEventListener("click", () => renderHistory(true).catch((e) => toast(e.message, false)));

  // ---------------------------------------------------------------- delete
  const ROWS = { schedule_terms: "学期", schedule_courses: "课程", schedule_slots: "上课时间", schedule_exceptions: "调课停课", schedule_exams: "考试",
    schedule_holidays: "假期", schedule_periods: "节次", schedule_imports: "导入记录", schedule_revisions: "修改历史" };
  const scopeText = (rows) => Object.entries(rows).filter(([, n]) => n).map(([k, n]) => `${ROWS[k] || k} ${n}`).join("、") || "没有数据";
  async function renderDelete() {
    try {
      const all = await api("GET", `${base}/scope`);
      $("#sch-del-all-scope").textContent = "会删除：" + scopeText(all.rows) + "。";
      if (S.termId) {
        const one = await api("GET", `${base}/scope?term_id=${S.termId}`);
        $("#sch-del-term-scope").textContent = `“${S.detail.term.name}”会删除：学期本身、${scopeText(one.rows)}，以及它们的修改历史。其他学期不受影响。`;
      } else $("#sch-del-term-scope").textContent = "还没有学期";
      $("#sch-del-term").disabled = !S.termId;
    } catch (e) { toast(e.message, false); }
  }
  $("#sch-del-term").addEventListener("click", () => {
    if (!S.detail) return;
    const t = S.detail.term;
    formDialog("删除这个学期的全部课表", [{ name: "confirm_name", label: `输入学期名称“${t.name}”确认`, required: true, max: 60 }], async (v) => {
      if (v.confirm_name !== t.name) throw new Error("学期名称不一致，没有删除");
      const r = await api("POST", `${base}/terms/${t.id}/purge`, { confirm: true, confirm_name: v.confirm_name });
      toast(`已删除“${t.name}”（${Object.values(r.deleted).reduce((a, b) => a + b, 0)} 行）`); S.termId = null; refresh(false);
    }, $("#sch-del-term-scope").textContent + " 不能恢复。");
  });
  $("#sch-del-all").addEventListener("click", () => {
    const text = (S.overview && S.overview.purge_confirm_text) || "删除全部课表";
    formDialog("删除全部课表", [{ name: "confirm_text", label: `输入“${text}”确认`, required: true, max: 20 }], async (v) => {
      if (v.confirm_text !== text) throw new Error("输入的文字不一致，没有删除");
      await api("POST", `${base}/purge-all`, { confirm: true, confirm_text: v.confirm_text });
      toast("全部课表已删除"); S.termId = null; refresh(false);
    }, $("#sch-del-all-scope").textContent + " 所有学期都会删除，修改历史里的旧副本一并清除，不能恢复。");
  });
})();
