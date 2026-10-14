/* DBC 工作台·曲线：实时信号曲线与记录文件曲线（uPlot）。
 * 实时数据由后端逐帧记录、前端按游标增量取回，所以界面刷新间隔不影响采样密度。 */

const DBC_PLOT_CAP = 240000;       // 每条曲线在前端最多保留的样本数
const DBC_PLOT_DRAW_MAX = 60000;   // 一次绘制的上限，超过则抽稀
const DBC_PLOT_SERIES_MAX = 24;

const dbcPlot = {
  mode: "live",
  series: [],
  data: new Map(),
  traceData: new Map(),
  chart: null,
  chartKey: "",
  drawKey: "",
  paused: false,
  windowSec: 30,
  normalize: false,
  origin: null,
  trace: null,
  busy: false,
  observer: null,
};

function dbcParseKey(key) {
  const [kind, number] = key.split(":");
  return { id: Number(number), extended: kind === "e" };
}

function dbcSid(source, key, signal) { return `${source}:${key}:${signal}`; }

function dbcPlotHas(source, key, signal) { return dbcPlot.series.some(item => item.sid === dbcSid(source, key, signal)); }

function dbcPlotSpecsForView() {
  return dbcPlot.series.map(({ source, key, signal }) => ({ source, key, signal }));
}

function dbcRestorePlot(entries) {
  dbcPlot.series = [];
  for (const entry of entries.slice(0, DBC_PLOT_SERIES_MAX)) dbcPlotAdd(entry.source, entry.key, entry.signal, false);
  dbcPlotPushSpecs();
}

function dbcPlotAdd(source, key, signal, push = true) {
  if (dbcPlotHas(source, key, signal)) return;
  const used = new Set(dbcPlot.series.map(item => item.color));
  let color = 0;
  while (used.has(color) && color < 7) color += 1;
  const { id, extended } = dbcParseKey(key);
  dbcPlot.series.push({ sid: dbcSid(source, key, signal), source, key, signal, id, extended, color, unit: "", title: "" });
  if (push) dbcPlotPushSpecs();
}

function dbcTogglePlot(source, key, signal) {
  const sid = dbcSid(source, key, signal);
  if (dbcPlotHas(source, key, signal)) {
    dbcPlot.series = dbcPlot.series.filter(item => item.sid !== sid);
    dbcPlot.data.delete(sid);
    dbcPlot.traceData.delete(sid);
  } else if (dbcPlot.series.length >= DBC_PLOT_SERIES_MAX) {
    return toast(`曲线最多 ${DBC_PLOT_SERIES_MAX} 条`, true);
  } else {
    dbcPlotAdd(source, key, signal);
  }
  dbcPlotPushSpecs();
  dbcPersistView();
  dbcPlot.drawKey = "";
  if (dbcPlot.mode === "trace") dbcPlotLoadTrace();
  renderDbc();
}

function dbcPlotPushSpecs() {
  if (!state.api?.dbc_series_set) return;
  for (const source of ["main", "vehicle"]) {
    const specs = dbcPlot.series.filter(item => item.source === source)
      .map(({ sid, id, extended, signal }) => ({ sid, id, extended, signal }));
    state.api.dbc_series_set(source, specs).then(result => {
      if (!result?.ok) toast(result?.error || "无法订阅曲线", true);
    });
  }
}

/** 轮询里调用：只在曲线页可见且为实时模式时取数据。 */
async function dbcPlotFetch() {
  if (dbcUi.tab !== "plot" || dbcPlot.mode !== "live" || dbcPlot.busy || !dbcPlot.series.length) return;
  dbcPlot.busy = true;
  try {
    for (const source of ["main", "vehicle"]) {
      const mine = dbcPlot.series.filter(item => item.source === source);
      if (!mine.length) continue;
      const cursors = {};
      for (const item of mine) cursors[item.sid] = dbcPlot.data.get(item.sid)?.cursor || 0;
      const reply = await state.api.dbc_series_fetch(source, cursors);
      if (!reply?.ok) continue;
      for (const item of mine) {
        const chunk = reply.series[item.sid];
        if (!chunk) continue;
        let entry = dbcPlot.data.get(item.sid);
        if (!entry || chunk.reset) { entry = { t: [], v: [], cursor: 0 }; dbcPlot.data.set(item.sid, entry); }
        if (dbcPlot.origin === null && chunk.t.length) dbcPlot.origin = chunk.t[0];
        for (let index = 0; index < chunk.t.length; index += 1) { entry.t.push(chunk.t[index]); entry.v.push(chunk.v[index]); }
        entry.cursor = chunk.cursor;
        if (entry.t.length > DBC_PLOT_CAP) {
          const drop = entry.t.length - DBC_PLOT_CAP;
          entry.t.splice(0, drop); entry.v.splice(0, drop);
        }
      }
    }
  } finally { dbcPlot.busy = false; }
}

/* ---------- 绘制 ---------- */

function dbcPlotUnits() {
  const units = [];
  for (const item of dbcPlot.series) if (!units.includes(item.unit || "")) units.push(item.unit || "");
  return units;
}

function dbcPlotResolveMeta() {
  for (const item of dbcPlot.series) {
    if (item.unit && item.title) continue;
    const decoded = dbcUi.live[item.source]?.decoded?.[item.key];
    const row = decoded?.signals?.find(signal => signal.name === item.signal);
    if (row) { item.unit = row.unit || ""; item.title = decoded.name; }
  }
}

function dbcPlotColor(index) { return cssVar(`--series-${(index % 8) + 1}`, "#4ad6ec"); }

function dbcPlotSource(item) { return dbcPlot.mode === "trace" ? dbcPlot.traceData.get(item.sid) : dbcPlot.data.get(item.sid); }

function dbcBisect(times, value) {
  let low = 0, high = times.length;
  while (low < high) { const mid = (low + high) >> 1; if (times[mid] < value) low = mid + 1; else high = mid; }
  return low;
}

function dbcPlotTable() {
  const origin = dbcPlot.mode === "trace" ? 0 : (dbcPlot.origin ?? 0);
  let latest = -Infinity;
  for (const item of dbcPlot.series) {
    const source = dbcPlotSource(item);
    if (source?.t.length) latest = Math.max(latest, source.t[source.t.length - 1] - origin);
  }
  const windowed = dbcPlot.mode === "live" && dbcPlot.windowSec > 0 && Number.isFinite(latest);
  const from = windowed ? latest - dbcPlot.windowSec : -Infinity;
  const tables = [];
  const stats = [];
  for (const item of dbcPlot.series) {
    const source = dbcPlotSource(item);
    if (!source || !source.t.length) { tables.push([[], []]); stats.push(null); continue; }
    let start = windowed ? dbcBisect(source.t, from + origin) : 0;
    let times = source.t.slice(start).map(value => value - origin);
    let values = source.v.slice(start);
    if (times.length > DBC_PLOT_DRAW_MAX) {
      const stride = Math.ceil(times.length / DBC_PLOT_DRAW_MAX);
      times = times.filter((_, index) => index % stride === 0);
      values = values.filter((_, index) => index % stride === 0);
    }
    let low = Infinity, high = -Infinity;
    for (const value of source.v) { if (value < low) low = value; if (value > high) high = value; }
    stats.push({ low, high });
    if (dbcPlot.normalize && high > low) values = values.map(value => (value - low) / (high - low) * 100);
    else if (dbcPlot.normalize) values = values.map(() => 50);
    tables.push([times, values]);
  }
  return { tables, latest, from, windowed, stats };
}

function renderDbcPlot() {
  dbcPlotResolveMeta();
  const empty = $("#dbcPlotEmpty");
  const chips = dbcPlot.series.map((item, index) =>
    `<span class="dbc-chip" data-sid="${escapeHtml(item.sid)}"><i style="background:${dbcPlotColor(item.color)}"></i>`
    + `${escapeHtml(item.signal)}${item.unit ? ` <small>${escapeHtml(item.unit)}</small>` : ""}`
    + `<small>${escapeHtml(item.title || item.key)} · ${item.source === "main" ? "CAN1" : "CANB"}</small>`
    + `<button type="button" data-act="remove" aria-label="移除 ${escapeHtml(item.signal)}">×</button></span>`).join("");
  dbcPaint("plotChips", $("#dbcPlotChips"), chips);
  $$("#dbcPlotMode button").forEach(button => button.classList.toggle("active", button.dataset.mode === dbcPlot.mode));
  $("#dbcPlotWindow").disabled = dbcPlot.mode === "trace";
  $("#dbcPlotPause").disabled = dbcPlot.mode === "trace";
  renderDbcTraceCard();
  if (!dbcPlot.series.length) {
    empty.textContent = "在“实时”页的信号行点“曲线”，把信号加入这里。";
    empty.classList.remove("hidden");
    dbcPlotDestroy();
    return;
  }
  const hasData = dbcPlot.series.some(item => dbcPlotSource(item)?.t.length);
  empty.classList.toggle("hidden", hasData);
  if (!hasData) {
    empty.textContent = dbcPlot.mode === "trace" ? "记录文件里没有这些信号的数据。"
      : (dbcUi.live[dbcUi.source]?.connection?.connected ? "等待所选信号的数据…" : "连接 CAN 或载入记录文件后才有曲线。");
    dbcPlotDestroy();
    return;
  }
  if (dbcPlot.paused && dbcPlot.chart && dbcPlot.mode === "live") return;
  dbcPlotDraw();
}

function dbcPlotDestroy() {
  if (dbcPlot.chart) { dbcPlot.chart.destroy(); dbcPlot.chart = null; }
  dbcPlot.chartKey = "";
  dbcPlot.drawKey = "";
}

function dbcPlotDraw() {
  const host = $("#dbcPlot");
  const { tables, latest, from, windowed, stats } = dbcPlotTable();
  const units = dbcPlotUnits();
  const normalize = dbcPlot.normalize || units.length > 3;
  const theme = document.documentElement.dataset.theme;
  const width = Math.max(320, host.clientWidth - 24);
  const structure = JSON.stringify([dbcPlot.series.map(item => [item.sid, item.color, item.unit]), normalize, theme, width, dbcPlot.mode]);
  const counts = tables.map(table => table[0].length).join(",");
  const drawKey = `${counts}|${windowed ? Math.round(latest * 4) : ""}|${dbcPlot.windowSec}`;
  if (structure !== dbcPlot.chartKey) {
    dbcPlotDestroy();
    const axisColor = cssVar("--chart-axis", "#87969c");
    const gridColor = cssVar("--chart-grid", "#2d2f31");
    const scaleOf = unit => (normalize ? "pct" : `u:${unit}`);
    const scales = { x: { time: false } };
    const axes = [{ stroke: axisColor, grid: { stroke: gridColor, width: 1 }, ticks: { stroke: gridColor }, label: "时间 s", labelSize: 22,
      font: "11px monospace", labelFont: "11px sans-serif", values: (_u, splits) => splits.map(value => dbcNumber(value)) }];
    if (normalize) {
      scales.pct = { range: [-2, 102] };
      axes.push({ scale: "pct", stroke: axisColor, grid: { stroke: gridColor, width: 1 }, ticks: { stroke: gridColor }, label: "%", font: "11px monospace" });
    } else {
      units.forEach((unit, index) => {
        scales[scaleOf(unit)] = {};
        axes.push({ scale: scaleOf(unit), side: index === 0 ? 3 : 1, stroke: axisColor,
          grid: index === 0 ? { stroke: gridColor, width: 1 } : { show: false }, ticks: { stroke: gridColor },
          label: unit || "无单位", font: "11px monospace", labelFont: "11px sans-serif" });
      });
    }
    const options = {
      width, height: 340, scales, axes, legend: { show: true },
      cursor: { drag: { x: true, y: false }, points: { size: 6 } },
      series: [{ label: "时间 s" }, ...dbcPlot.series.map(item => ({
        label: `${item.signal}${item.unit ? ` [${item.unit}]` : ""}`, stroke: dbcPlotColor(item.color), width: 1.6,
        scale: scaleOf(item.unit || ""), spanGaps: true, points: { show: false },
        value: (_u, value) => (value == null ? "—" : dbcNumber(value)),
      }))],
      hooks: { setSelect: [chart => { if (chart.select.width > 0 && dbcPlot.mode === "live") { dbcPlot.paused = true; $("#dbcPlotPause").checked = true; } }] },
    };
    host.innerHTML = "";
    const joined = uPlot.join(tables.map(([t, v]) => [t, v]));
    dbcPlot.chart = new uPlot(options, joined, host);
    dbcPlot.chartKey = structure;
    dbcPlot.drawKey = drawKey;
    if (windowed) dbcPlotScaleX(from, latest);
    return;
  }
  if (drawKey === dbcPlot.drawKey) return;
  dbcPlot.drawKey = drawKey;
  const joined = uPlot.join(tables.map(([t, v]) => [t, v]));
  dbcPlot.chart.setData(joined, !windowed);
  if (windowed) dbcPlotScaleX(from, latest);
}

/** 窗口还没填满时从 0 开始，随后才随最新样本滚动。 */
function dbcPlotScaleX(from, latest) {
  const min = Math.max(from, 0);
  dbcPlot.chart.setScale("x", { min, max: Math.max(latest, min + 1) });
}

/* ---------- 记录文件 ---------- */

function renderDbcTraceCard() {
  const trace = dbcPlot.trace;
  $("#dbcTraceCard")?.classList.toggle("hidden", !trace);
  if (!trace) return;
  text("#dbcTraceTitle", `记录文件 · ${trace.summary.name}`);
  $$("#dbcTraceBus button").forEach(button => button.classList.toggle("active", button.dataset.bus === trace.bus));
  const summary = trace.summary;
  const meta = `<dl class="dbc-fact"><dt>帧数</dt><dd>${Number(summary.frames).toLocaleString()}</dd></dl>`
    + `<dl class="dbc-fact"><dt>时长</dt><dd>${dbcNumber(summary.duration)} s</dd></dl>`
    + `<dl class="dbc-fact"><dt>不同 ID</dt><dd>${summary.ids.length}</dd></dl>`
    + `<dl class="dbc-fact"><dt>记录总线</dt><dd>${summary.bus_profile ? summary.bus_profile.toUpperCase() : "未标注"}</dd></dl>`
    + `<dl class="dbc-fact"><dt>格式</dt><dd>${summary.kind === "bmslog" ? "原生 .bmslog" : "CSV"}</dd></dl>`;
  dbcPaint("traceMeta", $("#dbcTraceMeta"), meta);
}

async function dbcPlotLoadTrace() {
  const trace = dbcPlot.trace;
  if (!trace || !state.api?.dbc_trace_series) return;
  dbcPlot.traceData.clear();
  if (!dbcPlot.series.length) { dbcPlot.drawKey = ""; return; }
  const specs = dbcPlot.series.map(({ sid, id, extended, signal }) => ({ sid, id, extended, signal }));
  const reply = await state.api.dbc_trace_series(trace.summary.id, trace.bus, specs);
  if (!reply?.ok) return toast(reply?.error || "无法从记录文件解码", true);
  for (const item of dbcPlot.series) {
    const result = reply.series[item.sid];
    dbcPlot.traceData.set(item.sid, { t: result?.t || [], v: result?.v || [], samples: result?.samples || 0 });
  }
  dbcPlot.drawKey = "";
  dbcPlotDestroy();
  renderDbc();
}

async function dbcTraceOpen() {
  const reply = await state.api.dbc_trace_choose();
  if (reply?.cancelled) return;
  if (!reply?.ok) return toast(reply?.error || "无法打开记录文件", true);
  if (dbcPlot.trace) state.api.dbc_trace_close(dbcPlot.trace.summary.id);
  const summary = reply.trace;
  dbcPlot.trace = { summary, bus: summary.bus_profile || (dbcUi.live[dbcUi.source]?.bus === "canb" ? "canb" : "can1") };
  dbcPlot.mode = "trace";
  toast(`已载入 ${summary.name}：${Number(summary.frames).toLocaleString()} 帧`);
  await dbcPlotLoadTrace();
  renderDbc();
}

async function dbcTraceClose() {
  if (dbcPlot.trace) await state.api.dbc_trace_close(dbcPlot.trace.summary.id);
  dbcPlot.trace = null;
  dbcPlot.traceData.clear();
  dbcPlot.mode = "live";
  dbcPlotDestroy();
  renderDbc();
}

/* ---------- 导出 ---------- */

function dbcPlotCsv() {
  const tables = dbcPlot.series.map(item => {
    const source = dbcPlotSource(item);
    const origin = dbcPlot.mode === "trace" ? 0 : (dbcPlot.origin ?? 0);
    return [(source?.t || []).map(value => value - origin), source?.v || []];
  });
  const joined = uPlot.join(tables);
  const header = ["时间 s", ...dbcPlot.series.map(item => `${item.signal}${item.unit ? ` [${item.unit}]` : ""}`)];
  const lines = [header.join(",")];
  for (let row = 0; row < joined[0].length; row += 1) {
    lines.push([joined[0][row].toFixed(6), ...joined.slice(1).map(column => (column[row] == null ? "" : column[row]))].join(","));
  }
  return lines.join("\r\n");
}

async function dbcPlotExport() {
  if (!dbcPlot.series.length || !dbcPlot.series.some(item => dbcPlotSource(item)?.t.length)) return toast("还没有可导出的曲线数据", true);
  const result = await state.api.dbc_save_text(`DBC曲线_${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "")}.csv`, dbcPlotCsv());
  if (result?.ok) toast(`CSV 已导出：${result.path}`);
  else if (!result?.cancelled) toast(result?.error || "导出失败", true);
}

/* ---------- 事件 ---------- */

function bindDbcPlotControls() {
  $("#dbcPlotMode")?.addEventListener("click", event => {
    const button = event.target.closest("button"); if (!button) return;
    if (button.dataset.mode === "trace" && !dbcPlot.trace) return dbcTraceOpen();
    dbcPlot.mode = button.dataset.mode;
    dbcPlotDestroy();
    renderDbc();
  });
  $("#dbcPlotWindow")?.addEventListener("change", event => { dbcPlot.windowSec = Number(event.target.value); dbcPlot.drawKey = ""; renderDbc(); });
  $("#dbcPlotNormalize")?.addEventListener("change", event => { dbcPlot.normalize = event.target.checked; dbcPlotDestroy(); renderDbc(); });
  $("#dbcPlotPause")?.addEventListener("change", event => { dbcPlot.paused = event.target.checked; dbcPlot.drawKey = ""; renderDbc(); });
  $("#dbcPlotClear")?.addEventListener("click", async () => {
    dbcPlot.data.clear(); dbcPlot.origin = null;
    await state.api.dbc_series_clear("main"); await state.api.dbc_series_clear("vehicle");
    dbcPlotDestroy(); renderDbc();
  });
  $("#dbcPlotExport")?.addEventListener("click", dbcPlotExport);
  $("#dbcTraceOpen")?.addEventListener("click", dbcTraceOpen);
  $("#dbcTraceClose")?.addEventListener("click", dbcTraceClose);
  $("#dbcTraceBus")?.addEventListener("click", event => {
    const button = event.target.closest("button"); if (!button || !dbcPlot.trace) return;
    dbcPlot.trace.bus = button.dataset.bus;
    dbcPlotLoadTrace();
  });
  $("#dbcTraceExport")?.addEventListener("click", async () => {
    if (!dbcPlot.trace) return;
    const result = await state.api.dbc_trace_export(dbcPlot.trace.summary.id, dbcPlot.trace.bus);
    if (result?.ok) toast(`已导出 ${Number(result.rows).toLocaleString()} 行：${result.path}`);
    else if (!result?.cancelled) toast(result?.error || "导出失败", true);
  });
  $("#dbcPlotChips")?.addEventListener("click", event => {
    const button = event.target.closest('button[data-act="remove"]'); if (!button) return;
    const item = dbcPlot.series.find(series => series.sid === button.closest("[data-sid]").dataset.sid);
    if (item) dbcTogglePlot(item.source, item.key, item.signal);
  });
  if (typeof ResizeObserver === "function" && $("#dbcPlot")) {
    dbcPlot.observer = new ResizeObserver(() => {
      if (dbcPlot.chart && state.page === "dbc" && dbcUi.tab === "plot") { dbcPlot.chartKey = ""; dbcPlot.drawKey = ""; renderDbcPlot(); }
    });
    dbcPlot.observer.observe($("#dbcPlot"));
  }
}
