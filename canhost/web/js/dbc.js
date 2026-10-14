/* DBC 工作台：文件、报文列表、实时解码和观察表。
 * 曲线在 dbc-plot.js，编辑在 dbc-editor.js，发送 / 对账 / 记录文件在 dbc-tools.js。
 * DBC 解码只用于查看和调试，不驱动任何页面状态机：各协议页面仍只认 decoders.py。 */

const dbcUi = {
  ready: false,
  loading: false,
  source: "main",
  tab: "live",
  revision: -1,
  files: [],
  recent: [],
  suggestions: [],
  buses: [],
  fileId: null,
  details: new Map(),
  selected: { key: null, signal: null },
  filter: "",
  live: { main: null, vehicle: null },
  watch: [],
  signatures: {},
  viewTimer: null,
  lastData: new Map(),
};

const DBC_BUS_LABEL = { can1: "CAN1", canb: "CANB", all: "全部总线", none: "不解码" };
const DBC_TABS = ["live", "plot", "edit", "send", "audit"];
const DBC_TAB_HINT = {
  live: "每条报文取总线上最新一帧解码",
  plot: "每一帧都被记录，不受界面刷新间隔影响",
  edit: "改动立即用于实时解码；保存前可评审变更",
  send: "按信号填值生成数据，发送沿用原始帧发送的确认与保护",
  audit: "把 DBC 与总线上实际发生的情况逐项对照",
};

/* ---------- 小工具 ---------- */

function dbcApi() { return state.api && state.api.dbc_state ? state.api : null; }

/** 仅在内容变化时写 DOM，避免每次轮询重建表格。 */
function dbcPaint(key, node, html) {
  if (!node) return false;
  if (dbcUi.signatures[key] === html) return false;
  dbcUi.signatures[key] = html;
  node.innerHTML = html;
  return true;
}

function dbcNumber(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  if (!Number.isFinite(value)) return String(value);
  if (Number.isInteger(value)) return String(value);
  const magnitude = Math.abs(value);
  if (magnitude !== 0 && (magnitude >= 1e9 || magnitude < 1e-4)) return value.toExponential(4);
  return String(Number(value.toPrecision(10)));
}

function dbcAgeText(ms) {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.round(ms / 1000)} s`;
}

function dbcIdText(id, extended) {
  const hex = Number(id).toString(16).toUpperCase();
  return extended ? `0x${hex.padStart(8, "0")}` : `0x${hex.padStart(3, "0")}`;
}

function dbcKey(id, extended) { return `${extended ? "e" : "s"}:${id}`; }

function dbcSourceLabel(source = dbcUi.source) {
  const connection = dbcUi.live[source]?.connection;
  if (source === "main") return connection?.bus_profile === "canb" ? "CANB(主连接)" : "CAN1";
  return "CANB";
}

/* ---------- 状态与数据加载 ---------- */

function dbcCurrentFile() { return dbcUi.files.find(file => file.id === dbcUi.fileId) || null; }

function dbcDetail(fileId = dbcUi.fileId) { return fileId ? dbcUi.details.get(fileId) || null : null; }

function dbcCatalog(fileId = dbcUi.fileId) { return dbcDetail(fileId)?.catalog || null; }

function dbcMessageEntry(key = dbcUi.selected.key, fileId = dbcUi.fileId) {
  return dbcCatalog(fileId)?.messages.find(message => message.key === key) || null;
}

async function dbcRefreshState() {
  const api = dbcApi();
  if (!api) return;
  const reply = await api.dbc_state();
  if (!reply?.ok) throw new Error(reply?.error || "无法读取 DBC 状态");
  const first = !dbcUi.ready;
  dbcUi.files = reply.files;
  dbcUi.recent = reply.recent || [];
  dbcUi.suggestions = reply.suggestions || [];
  dbcUi.buses = reply.buses || [];
  dbcUi.revision = reply.revision;
  if (!dbcUi.files.some(file => file.id === dbcUi.fileId)) dbcUi.fileId = dbcUi.files[0]?.id || null;
  for (const id of [...dbcUi.details.keys()]) {
    if (!dbcUi.files.some(file => file.id === id)) dbcUi.details.delete(id);
  }
  if (first) {
    dbcUi.ready = true;
    dbcRestoreView(reply.view || {});
    for (const problem of reply.problems || []) toast(problem, true);
  }
  await dbcEnsureDetail();
  dbcUi.signatures = {};
  dbcSelectDefault();
}

async function dbcEnsureDetail(fileId = dbcUi.fileId) {
  const api = dbcApi();
  const file = dbcUi.files.find(item => item.id === fileId);
  if (!api || !file) return;
  const cached = dbcUi.details.get(fileId);
  if (cached && cached.fileRevision === file.revision) return;
  const reply = await api.dbc_detail(fileId);
  if (!reply?.ok) throw new Error(reply?.error || "无法读取 DBC 内容");
  dbcUi.details.set(fileId, { fileRevision: reply.file.revision, catalog: reply.catalog, issues: reply.issues });
}

function dbcSelectDefault() {
  const catalog = dbcCatalog();
  if (!catalog) { dbcUi.selected = { key: null, signal: null }; return; }
  const messages = catalog.messages;
  if (!messages.some(message => message.key === dbcUi.selected.key)) {
    const first = messages.find(message => !message.virtual) || messages[0];
    dbcUi.selected = { key: first?.key || null, signal: null };
  } else if (dbcUi.selected.signal
             && !dbcMessageEntry()?.signals.some(signal => signal.name === dbcUi.selected.signal)) {
    dbcUi.selected.signal = null;
  }
}

async function dbcSync() {
  try { await dbcRefreshState(); } catch (error) { toast(String(error.message || error), true); }
  renderDbc();
}

function dbcSelectFile(fileId) {
  if (dbcUi.fileId === fileId) return;
  dbcUi.fileId = fileId;
  dbcUi.selected = { key: null, signal: null };
  dbcUi.signatures = {};
  dbcEnsureDetail().then(() => { dbcSelectDefault(); renderDbc(); }).catch(error => toast(String(error), true));
}

function dbcSelectMessage(key, signal = null) {
  dbcUi.selected = { key, signal };
  dbcUi.signatures.editor = "";
  renderDbc();
}

/* ---------- 观察表与视图记忆 ---------- */

function dbcRestoreView(view) {
  if (view.source === "main" || view.source === "vehicle") dbcUi.source = view.source;
  dbcUi.watch = Array.isArray(view.watch) ? view.watch.slice(0, 64) : [];
  if (typeof dbcRestorePlot === "function") dbcRestorePlot(Array.isArray(view.plot) ? view.plot : []);
  $$("#dbcSource button").forEach(button => button.classList.toggle("active", button.dataset.source === dbcUi.source));
}

function dbcPersistView() {
  clearTimeout(dbcUi.viewTimer);
  dbcUi.viewTimer = setTimeout(() => {
    const plot = typeof dbcPlotSpecsForView === "function" ? dbcPlotSpecsForView() : [];
    state.api?.dbc_set_view?.({ watch: dbcUi.watch, plot, source: dbcUi.source });
  }, 400);
}

function dbcWatchIndex(source, key, signal) {
  return dbcUi.watch.findIndex(entry => entry.source === source && entry.key === key && entry.signal === signal);
}

function dbcToggleWatch(source, key, signal) {
  const index = dbcWatchIndex(source, key, signal);
  if (index >= 0) dbcUi.watch.splice(index, 1);
  else if (dbcUi.watch.length >= 64) return toast("观察表最多 64 个信号", true);
  else dbcUi.watch.push({ source, key, signal });
  dbcPersistView();
  renderDbc();
}

/* ---------- 轮询 ---------- */

/** core.js 的轮询在 DBC 页和监视器页调用：返回本次要解码的报文。 */
function dbcLiveRequest() {
  const onFrames = state.page === "frames";
  const source = onFrames ? state.frameSource : dbcUi.source;
  const keys = new Set();
  if (onFrames) {
    for (const entry of monitorUi.dbcExpanded) {
      const [entrySource, key] = entry.split("|");
      if (entrySource === source) keys.add(key);
    }
  } else {
    if (dbcUi.selected.key && (dbcUi.tab === "live" || dbcUi.tab === "edit")) keys.add(dbcUi.selected.key);
    for (const entry of dbcUi.watch) if (entry.source === source) keys.add(entry.key);
    if (dbcUi.tab === "plot" && typeof dbcPlot !== "undefined") {
      for (const item of dbcPlot.series) if (item.source === source) keys.add(item.key);
    }
    const sending = typeof dbcSendKey === "function" ? dbcSendKey() : null;
    if (sending && dbcUi.tab === "send") keys.add(sending);
  }
  return { source, options: { keys: [...keys].slice(0, 64), messages: !onFrames, unknown: !onFrames && dbcUi.tab === "live" } };
}

async function dbcFetchLive() {
  const request = dbcLiveRequest();
  const reply = await state.api.dbc_live(request.source, request.options);
  return { request, reply };
}

function dbcStoreLive({ request, reply }) {
  if (!reply?.ok) return;
  const previous = dbcUi.live[request.source];
  dbcUi.live[request.source] = reply;
  if (previous && previous.revision !== reply.revision && state.page === "dbc") dbcSync();
  else if (!dbcUi.ready && state.page === "dbc" && !dbcUi.loading) dbcSync();
}

/* ---------- 渲染 ---------- */

function dbcLiveFor(source = dbcUi.source) { return dbcUi.live[source]; }

function dbcMessageSummaries() {
  const live = dbcLiveFor();
  const map = new Map();
  for (const message of live?.messages || []) map.set(`${message.file}|${message.key}`, message);
  return map;
}

function dbcStateInfo(summary) {
  if (!summary) return { cls: "neutral", label: "未绑定", dot: "" };
  if (summary.issue === "error") return { cls: "bad", label: "解码失败", dot: "error" };
  if (summary.issue === "dlc_short") return { cls: "bad", label: "DLC 不符", dot: "issue" };
  if (summary.state === "waiting") return { cls: "neutral", label: "等待数据", dot: "" };
  if (summary.state === "stale") return { cls: "warn", label: `已过期 ${dbcAgeText(summary.age_ms)}`, dot: "stale" };
  return { cls: "ok", label: "实时", dot: summary.issue === "dlc_long" ? "issue" : "live" };
}

function renderDbc() {
  if (state.page !== "dbc") return;
  if (!dbcUi.ready) {
    if (!dbcUi.loading && dbcApi()) {
      dbcUi.loading = true;
      dbcSync().finally(() => { dbcUi.loading = false; });
    }
    return;
  }
  renderDbcHeader();
  renderDbcFiles();
  renderDbcTree();
  $$("[data-dbc-panel]").forEach(panel => panel.classList.toggle("hidden", panel.dataset.dbcPanel !== dbcUi.tab));
  $$("#dbcTabs button").forEach(button => button.classList.toggle("active", button.dataset.tab === dbcUi.tab));
  text("#dbcTabHint", DBC_TAB_HINT[dbcUi.tab] || "");
  if (dbcUi.tab === "live") renderDbcLive();
  else if (dbcUi.tab === "plot" && typeof renderDbcPlot === "function") renderDbcPlot();
  else if (dbcUi.tab === "edit" && typeof renderDbcEditor === "function") renderDbcEditor();
  else if (dbcUi.tab === "send" && typeof renderDbcSender === "function") renderDbcSender();
  else if (dbcUi.tab === "audit" && typeof renderDbcAudit === "function") renderDbcAudit();
}

function renderDbcHeader() {
  const live = dbcLiveFor();
  const connection = live?.connection || {};
  const tag = $("#dbcLinkTag");
  let label = "未连接", cls = "neutral";
  if (connection.connected) {
    const mode = connection.mode === "replay" ? "回放" : connection.mode === "simulation" ? "模拟" : "PCAN";
    label = `${dbcSourceLabel()} · ${mode}`;
    cls = connection.mode === "simulation" ? "warn" : "ok";
  }
  if (tag) {
    tag.textContent = label;
    tag.className = `tag ${cls}`;
  }
  $$("#dbcSource button").forEach(button => button.classList.toggle("active", button.dataset.source === dbcUi.source));
}

function renderDbcFiles() {
  const live = dbcLiveFor();
  const bound = new Set((live?.files || []).map(file => file.id));
  let html = dbcUi.files.map(file => {
    const issues = file.errors ? `<span class="dbc-count bad" title="${file.errors} 个错误">${file.errors}</span>`
      : file.warnings ? `<span class="dbc-count warn" title="${file.warnings} 个警告">${file.warnings}</span>` : "";
    const options = (dbcUi.buses.length ? dbcUi.buses : Object.entries(DBC_BUS_LABEL).map(([key, label]) => ({ key, label })))
      .map(bus => `<option value="${bus.key}" ${bus.key === file.bus ? "selected" : ""}>${escapeHtml(bus.label)}</option>`).join("");
    const flags = [file.readonly ? '<span title="中央库 DBC 默认只读；另存为后可编辑">只读</span>' : "",
      file.disk_changed ? '<button class="dbc-mini" data-file-act="reload" title="磁盘上的文件已被改动">重新载入</button>' : "",
      file.decode_error ? `<span class="dbc-count bad" title="${escapeHtml(file.decode_error)}">解析</span>` : "",
      bound.has(file.id) ? "" : (file.bus === "none" ? "" : '<span title="当前数据源的总线没有绑定到这个文件">非当前总线</span>')]
      .filter(Boolean).join("");
    return `<div class="dbc-file ${file.id === dbcUi.fileId ? "active" : ""}" data-file="${file.id}" title="${escapeHtml(file.path || "尚未保存")}">`
      + `<div class="dbc-file-head"><span class="dbc-file-name ${file.dirty ? "dirty" : ""}">${escapeHtml(file.name)}</span>${issues}`
      + `<button class="dbc-file-close" data-file-act="close" aria-label="关闭 ${escapeHtml(file.name)}">×</button></div>`
      + `<div class="dbc-file-meta"><select data-file-act="bus" aria-label="总线绑定">${options}</select>`
      + `<span>${file.messages} 报文 · ${file.signals} 信号 · ${escapeHtml(file.encoding_label)}</span>${flags}</div></div>`;
  }).join("");
  if (!dbcUi.files.length) {
    const suggestions = dbcUi.suggestions.map(item =>
      `<button class="outline-button" type="button" data-suggest="${escapeHtml(item.path)}">${escapeHtml(item.name)}</button>`).join("");
    html = `<div class="dbc-file-empty"><span class="note tight">还没有载入 DBC。</span>`
      + `<button class="action-button" type="button" data-file-act="open">打开 DBC…</button>`
      + (suggestions ? `<span class="note tight">团队中央库（只读）：</span>${suggestions}` : "") + "</div>";
  }
  const conflicts = live?.conflicts || [];
  if (conflicts.length) {
    html += `<div class="note tight" title="${escapeHtml(conflicts.slice(0, 8).map(item => `${item.key}：${item.files.join("、")}`).join("\n"))}">`
      + `${conflicts.length} 个 ID 同时出现在多个 DBC 中，先载入的文件生效。</div>`;
  }
  dbcPaint("files", $("#dbcFiles"), html);
}

function renderDbcTree() {
  const catalog = dbcCatalog();
  const container = $("#dbcTree");
  if (!catalog) { dbcPaint("tree", container, '<div class="empty-state">载入 DBC 后在这里选择报文。</div>'); return; }
  const summaries = dbcMessageSummaries();
  const query = dbcUi.filter.trim().toLowerCase();
  const groups = new Map();
  for (const message of catalog.messages) {
    if (query) {
      const haystack = `${message.name} ${message.id_text} ${message.signals.map(signal => signal.name).join(" ")}`.toLowerCase();
      if (!haystack.includes(query)) continue;
    }
    const group = message.virtual ? "未使用信号" : (message.senders[0] || "未指定发送节点");
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(message);
  }
  let html = "";
  for (const [group, messages] of [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0], "zh"))) {
    html += `<div class="dbc-group"><span>${escapeHtml(group)}</span><span>${messages.length}</span></div>`;
    for (const message of messages.sort((a, b) => a.id - b.id || Number(a.extended) - Number(b.extended))) {
      const info = dbcStateInfo(summaries.get(`${dbcUi.fileId}|${message.key}`));
      const active = message.key === dbcUi.selected.key;
      html += `<button class="dbc-msg ${active ? "active" : ""}" type="button" role="option" aria-selected="${active}" data-key="${message.key}">`
        + `<i class="dbc-dot ${info.dot}" title="${escapeHtml(info.label)}"></i>`
        + `<span class="dbc-msg-name" title="${escapeHtml(message.name)}">${escapeHtml(message.name)}</span>`
        + `<span class="dbc-msg-id">${message.virtual ? "—" : escapeHtml(message.id_text)}</span></button>`;
    }
  }
  if (!html) html = '<div class="empty-state">没有匹配的报文。</div>';
  dbcPaint("tree", container, html);
}

/** 实时页：选中报文的信号表、观察表和未识别帧。 */
function renderDbcLive() {
  const live = dbcLiveFor();
  const entry = dbcMessageEntry();
  const decoded = live?.decoded?.[dbcUi.selected.key];
  const fileMatches = decoded && decoded.file === dbcUi.fileId;
  const summary = fileMatches ? decoded : null;
  const info = dbcStateInfo(summary);
  text("#dbcLiveTitle", entry ? entry.name : "未选择报文");
  const tags = entry ? `<span class="tag ${info.cls}">${escapeHtml(info.label)}</span>` : "";
  dbcPaint("liveTags", $("#dbcLiveTags"), tags);

  let meta = "";
  if (entry) {
    const previous = dbcUi.lastData.get(entry.key) || "";
    const bytes = summary?.data ? summary.data.split(" ") : [];
    const before = previous ? previous.split(" ") : [];
    const cells = bytes.map((byte, index) => `<i class="${before[index] !== undefined && before[index] !== byte ? "changed" : ""}">${byte}</i>`).join("");
    if (summary?.data) dbcUi.lastData.set(entry.key, summary.data);
    const dlc = summary?.seen_dlc != null && summary.seen_dlc !== entry.dlc
      ? `<dd class="bad">${summary.seen_dlc} / DBC ${entry.dlc}</dd>` : `<dd>${entry.dlc}</dd>`;
    const cycle = summary?.cycle_ms != null ? `${dbcNumber(summary.cycle_ms)} ms` : "—";
    const expected = entry.cycle_ms ? `${entry.cycle_ms} ms` : "—";
    meta = `<dl class="dbc-fact"><dt>CAN ID</dt><dd>${entry.virtual ? "—" : escapeHtml(entry.id_text)} · ${entry.extended ? "扩展" : "标准"}</dd></dl>`
      + `<dl class="dbc-fact"><dt>DLC</dt>${dlc}</dl>`
      + `<dl class="dbc-fact"><dt>周期 实测 / DBC</dt><dd>${cycle} / ${expected}</dd></dl>`
      + `<dl class="dbc-fact"><dt>距今</dt><dd class="${summary?.state === "stale" ? "warn" : ""}">${dbcAgeText(summary?.age_ms)}</dd></dl>`
      + `<dl class="dbc-fact"><dt>帧数</dt><dd>${summary ? Number(summary.count || 0).toLocaleString() : "—"}</dd></dl>`
      + `<dl class="dbc-fact"><dt>最新数据</dt><dd><span class="dbc-bytes">${cells || "—"}</span></dd></dl>`
      + (entry.senders.length ? `<dl class="dbc-fact"><dt>发送节点</dt><dd>${escapeHtml(entry.senders.join(", "))}</dd></dl>` : "")
      + (entry.comment ? `<dl class="dbc-fact"><dt>说明</dt><dd>${escapeHtml(entry.comment)}</dd></dl>` : "");
  }
  dbcPaint("liveMeta", $("#dbcLiveMeta"), meta);

  const rows = $("#dbcSignalRows");
  if (!entry) {
    dbcPaint("signals", rows, "");
    $("#dbcLiveEmpty")?.classList.remove("hidden");
  } else {
    const rowsData = summary?.signals || entry.signals.map(signal => ({ name: signal.name, unit: signal.unit, state: "waiting", has_label: signal.choices.length > 0 }));
    dbcPaint("signals", rows, rowsData.map(row => dbcSignalRow(entry, row, summary)).join(""));
    const hint = $("#dbcLiveEmpty");
    hint?.classList.toggle("hidden", !!summary || entry.signals.length === 0);
    if (hint) hint.textContent = entry.virtual ? "Vector 的未使用信号容器不会出现在总线上。"
      : "这个 DBC 没有绑定到当前数据源的总线，先在左侧文件卡的下拉里选择 CAN1 或 CANB。";
  }
  renderDbcWatch();
  renderDbcUnknown();
}

function dbcRangeBar(signal, row) {
  if (!signal || row.state !== "ok" || typeof row.value !== "number") return "";
  const low = signal.minimum, high = signal.maximum;
  if (low === 0 && high === 0) return "";
  const span = high - low;
  if (!(span > 0)) return "";
  const fraction = Math.max(0, Math.min(1, (row.value - low) / span));
  return `<span class="dbc-range ${row.in_range === false ? "out" : ""}" title="范围 ${dbcNumber(low)} … ${dbcNumber(high)}"><i style="width:${(fraction * 100).toFixed(1)}%"></i></span>`;
}

function dbcValueCells(row, stale) {
  if (row.state === "waiting") return { value: "等待数据", raw: "—", label: "—", cls: "waiting" };
  if (row.state === "inactive") return { value: "未生效", raw: "—", label: "多路复用分支未选中", cls: "inactive" };
  if (row.state === "missing") return { value: "DLC 不足", raw: "—", label: "—", cls: "missing" };
  const unit = row.unit ? `<em>${escapeHtml(row.unit)}</em>` : "";
  let label = "—";
  if (row.has_label) label = row.label != null ? escapeHtml(row.label) : '<span class="tag warn">表外值</span>';
  const cls = [stale ? "stale" : "", row.in_range === false ? "out" : ""].filter(Boolean).join(" ");
  return { value: `${escapeHtml(dbcNumber(row.value))}${unit}`, raw: escapeHtml(String(row.raw_text ?? row.raw)), label, cls };
}

function dbcSignalRow(entry, row, summary) {
  const signal = entry.signals.find(item => item.name === row.name);
  const cells = dbcValueCells(row, summary?.state === "stale");
  const mux = signal?.mux?.role && signal.mux.role !== "none"
    ? `<small>${signal.mux.role === "multiplexor" ? "多路复用选择" : `复用值 ${signal.mux.value}`}</small>` : "";
  const watched = dbcWatchIndex(dbcUi.source, entry.key, row.name) >= 0;
  const plotted = typeof dbcPlotHas === "function" && dbcPlotHas(dbcUi.source, entry.key, row.name);
  return `<tr class="${cells.cls} ${row.state}" data-signal="${escapeHtml(row.name)}">`
    + `<td class="dbc-name" title="${escapeHtml(signal?.comment || "")}">${escapeHtml(row.name)}${mux}</td>`
    + `<td class="dbc-value">${cells.value}</td><td>${cells.raw}</td><td>${cells.label}</td>`
    + `<td>${dbcRangeBar(signal, row)}</td>`
    + `<td><div class="dbc-row-actions"><button data-act="watch" class="${watched ? "on" : ""}">${watched ? "已观察" : "观察"}</button>`
    + `<button data-act="plot" class="${plotted ? "on" : ""}">${plotted ? "已画" : "曲线"}</button></div></td></tr>`;
}

function renderDbcWatch() {
  const live = dbcLiveFor();
  const entries = dbcUi.watch.filter(entry => entry.source === dbcUi.source);
  const rows = entries.map(entry => {
    const decoded = live?.decoded?.[entry.key];
    const row = decoded?.signals.find(item => item.name === entry.signal);
    const info = dbcStateInfo(decoded);
    const cells = dbcValueCells(row || { state: "waiting" }, decoded?.state === "stale");
    const messageName = decoded?.name || live?.names?.[entry.key] || entry.key;
    return `<tr class="${cells.cls} ${row?.state || "waiting"}" data-watch="${escapeHtml(entry.key)}|${escapeHtml(entry.signal)}">`
      + `<td class="dbc-name">${escapeHtml(entry.signal)}<small>${escapeHtml(messageName)}</small></td>`
      + `<td class="dbc-value">${cells.value}</td><td>${cells.raw}</td><td>${cells.label}</td>`
      + `<td><span class="tag ${info.cls}">${escapeHtml(info.label)}</span></td>`
      + `<td><div class="dbc-row-actions"><button data-act="unwatch">移除</button></div></td></tr>`;
  }).join("");
  dbcPaint("watch", $("#dbcWatchRows"), rows);
  $("#dbcWatchEmpty")?.classList.toggle("hidden", entries.length > 0);
}

function renderDbcUnknown() {
  const live = dbcLiveFor();
  const unknown = live?.unknown || [];
  text("#dbcUnknownCount", String(unknown.length));
  const rows = unknown.map(frame => `<tr data-id="${frame.id}" data-ext="${frame.extended}">`
    + `<td>${escapeHtml(frame.id_text)} · ${frame.extended ? "扩展" : "标准"}</td><td>${frame.dlc}</td>`
    + `<td title="${escapeHtml(frame.data)}">${escapeHtml(frame.data || "无数据")}</td>`
    + `<td>${frame.cycle_ms != null ? `${dbcNumber(frame.cycle_ms)} ms` : "—"}</td>`
    + `<td>${Number(frame.count).toLocaleString()}</td>`
    + `<td><div class="dbc-row-actions"><button data-act="model">建模</button></div></td></tr>`).join("");
  dbcPaint("unknown", $("#dbcUnknownRows"), rows);
  $("#dbcUnknownEmpty")?.classList.toggle("hidden", unknown.length > 0);
}

/* ---------- 弹窗表单 ---------- */

/** 通用表单弹窗：fields = [{name,label,type,value,options,hint,required,min,max}]，取消时返回 null。 */
function dbcPrompt({ title, fields, ok = "确定", message = "", danger = false }) {
  const dialog = $("#dbcFormDialog");
  const body = $("#dbcFormBody");
  text("#dbcFormTitle", title);
  const okButton = $("#dbcFormOk");
  okButton.textContent = ok;
  okButton.className = danger ? "danger-button" : "action-button";
  const inputs = fields.map(field => {
    const id = `dbcField_${field.name}`;
    const label = `<span class="field-label">${escapeHtml(field.label)}${field.hint ? ` <small>${escapeHtml(field.hint)}</small>` : ""}</span>`;
    if (field.type === "select") {
      return `<label class="field">${label}<select id="${id}">${field.options.map(option =>
        `<option value="${escapeHtml(option.value)}" ${String(option.value) === String(field.value) ? "selected" : ""}>${escapeHtml(option.label)}</option>`).join("")}</select></label>`;
    }
    if (field.type === "checkbox") {
      return `<label class="checkbox"><input id="${id}" type="checkbox" ${field.value ? "checked" : ""}><span>${escapeHtml(field.label)}</span></label>`;
    }
    if (field.type === "textarea") {
      return `<label class="field">${label}<textarea id="${id}" rows="3">${escapeHtml(field.value ?? "")}</textarea></label>`;
    }
    return `<label class="field">${label}<input id="${id}" type="${field.type || "text"}" value="${escapeHtml(field.value ?? "")}"`
      + `${field.min != null ? ` min="${field.min}"` : ""}${field.max != null ? ` max="${field.max}"` : ""} autocomplete="off"></label>`;
  }).join("");
  body.innerHTML = (message ? `<p class="confirm-text">${escapeHtml(message)}</p>` : "") + inputs;
  return new Promise(resolve => {
    let settled = false;
    const finish = value => {
      if (settled) return;
      settled = true;
      okButton.removeEventListener("click", accept);
      dialog.removeEventListener("close", cancel);
      if (dialog.open) dialog.close("done");
      resolve(value);
    };
    const accept = () => {
      const values = {};
      for (const field of fields) {
        const node = $(`#dbcField_${field.name}`);
        values[field.name] = field.type === "checkbox" ? node.checked : field.type === "number" ? Number(node.value) : node.value.trim();
        if (field.required && (values[field.name] === "" || Number.isNaN(values[field.name]))) {
          node.classList.add("invalid");
          node.focus();
          return;
        }
      }
      finish(values);
    };
    const cancel = () => finish(null);
    okButton.addEventListener("click", accept);
    dialog.addEventListener("close", cancel);
    dialog.showModal();
    body.querySelector("input, select, textarea")?.focus();
  });
}

/* ---------- 事件 ---------- */

function bindDbcControls() {
  // 拖入窗口的文件由后端接管（pywebview 提供完整路径）；这里只阻止浏览器把它当页面打开。
  for (const type of ["dragover", "drop"]) {
    window.addEventListener(type, event => {
      if (event.dataTransfer?.types?.includes("Files")) event.preventDefault();
    });
  }
  $("#dbcSource")?.addEventListener("click", event => {
    const button = event.target.closest("button"); if (!button) return;
    dbcUi.source = button.dataset.source;
    dbcPersistView();
    dbcUi.signatures = {};
    schedulePoll(0);
    renderDbc();
  });
  $("#dbcTabs")?.addEventListener("click", event => {
    const button = event.target.closest("button[data-tab]"); if (!button || !DBC_TABS.includes(button.dataset.tab)) return;
    dbcUi.tab = button.dataset.tab;
    dbcUi.signatures = {};
    schedulePoll(0);
    renderDbc();
  });
  $("#dbcOpenButton")?.addEventListener("click", dbcOpenDialog);
  $("#dbcNewButton")?.addEventListener("click", dbcNewFile);
  $("#dbcSearch")?.addEventListener("input", event => { dbcUi.filter = event.target.value; renderDbcTree(); });
  $("#dbcFiles")?.addEventListener("click", dbcFilesClick);
  $("#dbcFiles")?.addEventListener("change", event => {
    const select = event.target.closest('select[data-file-act="bus"]'); if (!select) return;
    const id = select.closest("[data-file]").dataset.file;
    state.api.dbc_set_bus(id, select.value).then(result => {
      if (!result?.ok) toast(result?.error || "无法更改总线绑定", true);
      return dbcSync();
    });
  });
  $("#dbcTree")?.addEventListener("click", event => {
    const button = event.target.closest(".dbc-msg"); if (button) dbcSelectMessage(button.dataset.key);
  });
  $("#dbcSignalRows")?.addEventListener("click", event => {
    const button = event.target.closest("button[data-act]"); const row = event.target.closest("tr[data-signal]");
    if (!row) return;
    dbcUi.selected.signal = row.dataset.signal;
    if (!button) return;
    if (button.dataset.act === "watch") dbcToggleWatch(dbcUi.source, dbcUi.selected.key, row.dataset.signal);
    if (button.dataset.act === "plot" && typeof dbcTogglePlot === "function") dbcTogglePlot(dbcUi.source, dbcUi.selected.key, row.dataset.signal);
  });
  $("#dbcWatchRows")?.addEventListener("click", event => {
    const button = event.target.closest('button[data-act="unwatch"]'); const row = event.target.closest("tr[data-watch]");
    if (!button || !row) return;
    const [key, signal] = row.dataset.watch.split("|");
    dbcToggleWatch(dbcUi.source, key, signal);
  });
  $("#dbcWatchClear")?.addEventListener("click", () => {
    dbcUi.watch = dbcUi.watch.filter(entry => entry.source !== dbcUi.source);
    dbcPersistView();
    renderDbc();
  });
  $("#dbcUnknownRows")?.addEventListener("click", event => {
    const button = event.target.closest('button[data-act="model"]'); const row = event.target.closest("tr[data-id]");
    if (!button || !row) return;
    const frame = dbcLiveFor()?.unknown?.find(item => String(item.id) === row.dataset.id && String(item.extended) === row.dataset.ext);
    if (frame && typeof dbcModelFromFrame === "function") dbcModelFromFrame(frame);
  });
  if (typeof bindDbcPlotControls === "function") bindDbcPlotControls();
  if (typeof bindDbcEditorControls === "function") bindDbcEditorControls();
  if (typeof bindDbcToolControls === "function") bindDbcToolControls();
}

async function dbcFilesClick(event) {
  const suggestion = event.target.closest("[data-suggest]");
  if (suggestion) return dbcOpenPath(suggestion.dataset.suggest);
  const actor = event.target.closest("[data-file-act]");
  const card = event.target.closest("[data-file]");
  if (actor?.dataset.fileAct === "open") return dbcOpenDialog();
  if (!card) return;
  const id = card.dataset.file;
  if (actor?.dataset.fileAct === "bus") return;
  if (actor?.dataset.fileAct === "close") return dbcCloseFile(id);
  if (actor?.dataset.fileAct === "reload") {
    const result = await state.api.dbc_reload(id);
    if (!result?.ok) toast(result?.error || "重新载入失败", true);
    return dbcSync();
  }
  dbcSelectFile(id);
}

async function dbcOpenDialog() {
  if (!dbcApi()) return;
  const result = await state.api.dbc_choose_open();
  if (result?.cancelled) return;
  if (!result?.ok) return toast(result?.error || "无法打开 DBC", true);
  if (result.error) toast(result.error, true);
  if (result.files?.[0]) dbcUi.fileId = result.files[0].id;
  await dbcSync();
}

async function dbcOpenPath(path) {
  const result = await state.api.dbc_open(path);
  if (!result?.ok) return toast(result?.error || "无法打开 DBC", true);
  dbcUi.fileId = result.file.id;
  await dbcSync();
}

async function dbcNewFile() {
  if (!dbcApi()) return;
  const values = await dbcPrompt({
    title: "新建 DBC",
    fields: [{ name: "name", label: "文件名", value: "未命名.dbc", required: true },
      { name: "bus", label: "绑定总线", type: "select", value: "none",
        options: Object.entries(DBC_BUS_LABEL).map(([value, label]) => ({ value, label })) }],
    ok: "新建",
  });
  if (!values) return;
  const result = await state.api.dbc_new(values.name);
  if (!result?.ok) return toast(result?.error || "无法新建 DBC", true);
  await state.api.dbc_set_bus(result.file.id, values.bus);
  dbcUi.fileId = result.file.id;
  dbcUi.tab = "edit";
  await dbcSync();
}

async function dbcCloseFile(id) {
  const file = dbcUi.files.find(item => item.id === id);
  if (!file) return;
  if (file.dirty) {
    const answer = await dbcPrompt({
      title: "关闭 DBC", message: `${file.name} 有未保存的修改，关闭后会丢失。`, fields: [], ok: "放弃修改并关闭", danger: true });
    if (!answer) return;
  }
  const result = await state.api.dbc_close(id);
  if (!result?.ok) toast(result?.error || "无法关闭", true);
  if (dbcUi.fileId === id) dbcUi.fileId = null;
  await dbcSync();
}

/** 编辑器等模块提交一组编辑；成功后刷新目录。`key` 用于改 ID 后跟随新的报文标识。 */
async function dbcCommit(ops, { key = null } = {}) {
  const file = dbcCurrentFile();
  if (!file) return false;
  const result = await state.api.dbc_edit(file.id, ops);
  if (!result?.ok) { toast(result?.error || "编辑失败", true); return false; }
  if (key) dbcUi.selected.key = key;
  await dbcRefreshState();
  dbcUi.signatures = {};
  renderDbc();
  return true;
}
