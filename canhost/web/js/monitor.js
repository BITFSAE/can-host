/* CAN 监视器页面：ID 汇总、内容变体、自动留档、历史回放与受控发送工作区。 */

const MONITOR_TX_KEY = "canHostMonitorTransmitRowsV1";
const MONITOR_AUTO_RECORD_KEY = "canHostMonitorAutoRecordV1";
const monitorUi = {
  expanded: new Set(),
  frozenGroups: [],
  sort: "id",
  txRows: [],
  lastFrameSignature: "",
  lastTxSignature: "",
};

function monitorAutoRecordEnabled() {
  try {
    const saved = localStorage.getItem(MONITOR_AUTO_RECORD_KEY);
    return saved === null ? true : saved === "true";
  } catch { return true; }
}

function monitorConnection(source = state.frameSource) {
  return source === "vehicle"
    ? (state.vehicleSnapshot?.connection || state.quickSnapshot?.vehicle?.connection || {})
    : (state.snapshot?.connection || {});
}

function monitorSnapshot(source = state.frameSource) {
  return source === "vehicle"
    ? (state.vehicleSnapshot?.monitor || {})
    : (state.snapshot?.monitor || {});
}

function monitorGroups() {
  if (state.framePaused) return monitorUi.frozenGroups;
  return monitorSnapshot().groups || [];
}

function monitorUid() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `tx-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function defaultTxRow() {
  return {
    uid: monitorUid(), name: "未命名发送项", id: "0x000",
    extended: false, data: "00", cycle_ms: 200, count: 0,
  };
}

function restoreMonitorTxRows() {
  try {
    const parsed = JSON.parse(localStorage.getItem(MONITOR_TX_KEY) || "[]");
    if (Array.isArray(parsed)) {
      monitorUi.txRows = parsed.slice(0, 256).map(row => ({
        ...defaultTxRow(), ...row, uid: String(row.uid || monitorUid()), count: 0,
      }));
    }
  } catch { monitorUi.txRows = []; }
  if (!monitorUi.txRows.length) monitorUi.txRows = [defaultTxRow()];
}

function persistMonitorTxRows() {
  try {
    const rows = monitorUi.txRows.map(({ uid, name, id, extended, data, cycle_ms }) =>
      ({ uid, name, id, extended, data, cycle_ms }));
    localStorage.setItem(MONITOR_TX_KEY, JSON.stringify(rows));
  } catch { /* 本次会话仍可使用 */ }
}

function bindMonitorControls() {
  restoreMonitorTxRows();
  const autoRecord = $("#monitorAutoRecord");
  if (autoRecord) autoRecord.checked = monitorAutoRecordEnabled();

  $("#frameType")?.addEventListener("click", event => {
    const button = event.target.closest("button"); if (!button) return;
    state.frameKind = button.dataset.kind;
    $$("#frameType button").forEach(node => node.classList.toggle("active", node === button));
    monitorUi.lastFrameSignature = "";
    renderFrames();
  });
  $("#frameSource")?.addEventListener("click", event => {
    const button = event.target.closest("button"); if (!button || button.disabled) return;
    state.frameSource = button.dataset.source;
    state.framePaused = false;
    $("#pauseFrames").checked = false;
    $$("#frameSource button").forEach(node => node.classList.toggle("active", node === button));
    monitorUi.lastFrameSignature = "";
    schedulePoll(0);
    renderFrames();
    renderMonitorTransmitRows();
  });
  $("#frameSearch")?.addEventListener("input", () => {
    monitorUi.lastFrameSignature = "";
    renderFrames();
  });
  $("#frameSort")?.addEventListener("change", event => {
    monitorUi.sort = event.target.value;
    monitorUi.lastFrameSignature = "";
    renderFrames();
  });
  $("#pauseFrames")?.addEventListener("change", event => {
    state.framePaused = event.target.checked;
    if (state.framePaused) monitorUi.frozenGroups = structuredClone(monitorSnapshot().groups || []);
    monitorUi.lastFrameSignature = "";
    renderFrames();
  });
  $("#monitorAutoRecord")?.addEventListener("change", toggleMonitorAutoRecord);
  $("#recordButton")?.addEventListener("click", toggleRecording);
  $("#exportFramesButton")?.addEventListener("click", exportMonitorCsv);
  $("#replayButton")?.addEventListener("click", openReplay);
  $("#replayPlay")?.addEventListener("click", toggleReplay);
  $("#replaySpeed")?.addEventListener("change", event => state.api?.replay_control("speed", +event.target.value));
  $("#replaySeek")?.addEventListener("change", event => {
    const replay = state.snapshot?.connection?.replay; if (!replay) return;
    state.api?.replay_control("seek", replay.duration * (+event.target.value / 1000));
  });
  $("#frameRows")?.addEventListener("click", event => {
    const button = event.target.closest(".monitor-expand"); if (!button || button.disabled) return;
    const key = button.dataset.group;
    if (monitorUi.expanded.has(key)) monitorUi.expanded.delete(key); else monitorUi.expanded.add(key);
    monitorUi.lastFrameSignature = "";
    renderFrames();
  });
  $("#addTxRow")?.addEventListener("click", () => {
    monitorUi.txRows.push(defaultTxRow()); persistMonitorTxRows(); renderMonitorTransmitRows();
  });
  $("#monitorTxRows")?.addEventListener("input", updateMonitorTxInput);
  $("#monitorTxRows")?.addEventListener("change", changeMonitorTxControl);
  $("#monitorTxRows")?.addEventListener("click", clickMonitorTxAction);
  renderMonitorTransmitRows();
}

function monitorDirection(group) {
  if (group.rx_count && group.tx_count) {
    return '<span class="dir-tag rx">RX</span> <span class="dir-tag tx">TX</span>';
  }
  const direction = group.tx_count ? "tx" : "rx";
  return `<span class="dir-tag ${direction}">${direction.toUpperCase()}</span>`;
}

function monitorDataHtml(data, changed = []) {
  const changedSet = new Set(changed || []);
  const bytes = String(data || "").split(/\s+/).filter(Boolean);
  if (!bytes.length) return '<span class="muted">无数据</span>';
  return `<span class="monitor-data">${bytes.map((byte, index) =>
    `<i class="${changedSet.has(index) ? "changed" : ""}">${escapeHtml(byte)}</i>`).join("")}</span>`;
}

function monitorCycleLabel(value) {
  if (value == null) return '<span class="muted">—</span>';
  const number = Number(value);
  if (number >= 1000) return `<span class="monitor-cycle">${(number / 1000).toFixed(number >= 10000 ? 1 : 2)}<em>s</em></span>`;
  return `<span class="monitor-cycle">${number.toFixed(number >= 100 ? 1 : 2)}<em>ms</em></span>`;
}

function monitorVariantRows(group) {
  const variants = group.variants || [];
  if (!monitorUi.expanded.has(`${group.extended ? "e" : "s"}-${group.id}`)) return "";
  const cells = variants.map(variant =>
    `<span>${escapeHtml(variant.direction.toUpperCase())}</span>`
    + `<span>${variant.dlc}</span>`
    + `<span class="variant-data" title="${escapeHtml(variant.data)}">${escapeHtml(variant.data || "无数据")}</span>`
    + `<span>${variant.cycle_ms == null ? "—" : `${Number(variant.cycle_ms).toFixed(2)} ms`}</span>`
    + `<span>${Number(variant.count || 0).toLocaleString()}</span>`
    + `<span>${escapeHtml(variant.last_time)}</span>`).join("");
  return `<tr class="monitor-variant-row"><td colspan="9"><div class="variant-grid">${cells}</div></td></tr>`;
}

function renderFrames() {
  if (!state.snapshot) return;
  renderMonitorStatus();
  const query = $("#frameSearch")?.value.trim().toLowerCase() || "";
  let groups = monitorGroups().filter(group => {
    const directionMatch = state.frameKind === "all"
      || (state.frameKind === "rx" ? group.rx_count > 0 : group.tx_count > 0);
    return directionMatch && (!query || `${group.id} ${group.name} ${group.data}`.toLowerCase().includes(query));
  });
  groups = [...groups].sort((a, b) => {
    if (monitorUi.sort === "latest") return Number(a.age_ms ?? Infinity) - Number(b.age_ms ?? Infinity);
    if (monitorUi.sort === "count") return Number(b.count || 0) - Number(a.count || 0);
    if (monitorUi.sort === "cycle") return Number(a.cycle_ms ?? Infinity) - Number(b.cycle_ms ?? Infinity);
    return Number(a.arbitration_id) - Number(b.arbitration_id) || Number(a.extended) - Number(b.extended);
  });
  const signature = JSON.stringify([groups, [...monitorUi.expanded], state.framePaused]);
  if (signature === monitorUi.lastFrameSignature) return;
  monitorUi.lastFrameSignature = signature;
  const html = groups.map(group => {
    const key = `${group.extended ? "e" : "s"}-${group.id}`;
    const expandable = Number(group.variant_count || 0) > 1;
    const staleLimit = Math.max(2000, Number(group.cycle_ms || 0) * 3);
    const stale = group.age_ms != null && Number(group.age_ms) > staleLimit;
    const rowClass = ["monitor-group-row", stale ? "stale" : "", group.direction === "tx" ? "tx-only" : ""].filter(Boolean).join(" ");
    return `<tr class="${rowClass}">`
      + `<td><button class="monitor-expand" type="button" data-group="${key}" ${expandable ? "" : "disabled"} aria-expanded="${monitorUi.expanded.has(key)}" aria-label="${expandable ? "展开内容变体" : "没有其他内容变体"}">${monitorUi.expanded.has(key) ? "−" : "+"}</button></td>`
      + `<td><span class="monitor-id">${escapeHtml(group.id)}</span>${expandable ? `<span class="monitor-variant-badge">${group.variant_count} 种</span>` : ""}</td>`
      + `<td>${monitorDirection(group)}</td><td>${group.extended ? "扩展" : "标准"}</td><td>${group.dlc}</td>`
      + `<td title="${escapeHtml(group.data)}">${monitorDataHtml(group.data, group.changed)}</td>`
      + `<td>${monitorCycleLabel(group.cycle_ms)}</td><td class="monitor-count">${Number(group.count || 0).toLocaleString()}</td>`
      + `<td title="${escapeHtml(group.name)}">${escapeHtml(group.name || "未登记帧")}</td></tr>`
      + monitorVariantRows(group);
  }).join("");
  $("#frameRows").innerHTML = html;
  $("#frameEmpty").classList.toggle("hidden", groups.length > 0);
}

function renderMonitorStatus() {
  const connection = monitorConnection();
  const monitor = monitorSnapshot();
  text("#monitorIdCount", Number(monitor.id_count || 0).toLocaleString());
  text("#monitorRate", Number(monitor.frames_per_second || 0).toLocaleString());
  const recording = connection.recording;
  const recordButton = $("#recordButton");
  recordButton?.classList.toggle("active", !!recording);
  if (recordButton) recordButton.title = recording?.path || connection.last_recording?.path || "选择记录文件";
  const recordLabel = recordButton?.querySelector("span");
  if (recordLabel) recordLabel.textContent = recording ? "停止记录" : "记录";
  renderMonitorTransmitRows();
}

function timeLabel(seconds) {
  const value = Math.max(0, Math.floor(seconds || 0));
  const hours = Math.floor(value / 3600), minutes = Math.floor(value % 3600 / 60), secs = value % 60;
  return hours ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function renderReplay() {
  const connection = state.snapshot?.connection, replay = connection?.replay;
  $("#replayBar")?.classList.toggle("hidden", !replay);
  if (!replay) return;
  text("#replayFile", connection.channel);
  text("#replayPosition", `${timeLabel(replay.position)} / ${timeLabel(replay.duration)}`);
  $("#replaySeek").value = replay.duration ? Math.round(replay.position / replay.duration * 1000) : 0;
  $("#replayPlay").textContent = replay.paused ? "▶" : "Ⅱ";
  $("#replaySpeed").value = String(replay.speed);
}

async function toggleMonitorAutoRecord(event) {
  const enabled = event.target.checked;
  try { localStorage.setItem(MONITOR_AUTO_RECORD_KEY, String(enabled)); } catch { /* ignore */ }
  if (!state.api) return;
  const sources = ["main", "vehicle"].filter(source => monitorConnection(source).connected && monitorConnection(source).mode === "pcan");
  for (const source of sources) {
    const result = await state.api.set_monitor_auto_record(source, enabled);
    if (!result.ok) toast(result.error || "无法更新自动留档", true);
  }
  toast(enabled ? "真实 PCAN 连接将自动留档" : "已关闭自动留档");
  await poll();
}

async function toggleRecording() {
  if (!state.api) return;
  const connection = monitorConnection();
  if (connection.recording) {
    const result = await state.api.stop_recording(state.frameSource);
    if (result.ok) toast("CAN 数据留档已停止"); else toast(result.error || "停止留档失败", true);
  } else {
    const result = await state.api.choose_record_file(state.frameSource);
    if (result.ok) toast(`正在记录：${result.path}`);
    else if (!result.cancelled) toast(result.error || "无法开始记录", true);
  }
  await poll();
}

async function exportMonitorCsv() {
  if (!state.api) return;
  const result = await state.api.choose_export_monitor_csv(state.frameSource);
  if (result.ok) toast(`CSV 已导出：${result.path}`);
  else if (!result.cancelled) toast(result.error || "CSV 导出失败", true);
}

async function openReplay() {
  if (!state.api) return;
  if (state.snapshot?.connection?.recording) return toast("请先停止主连接的数据留档", true);
  const result = await state.api.choose_replay_file();
  if (result.ok) {
    state.frameSource = "main";
    toast(`已载入 ${result.frames.toLocaleString()} 帧历史记录`);
    showPage("frames");
    await poll();
  } else if (!result.cancelled) toast(result.error || "历史记录载入失败", true);
}

async function toggleReplay() {
  const replay = state.snapshot?.connection?.replay; if (!replay || !state.api) return;
  await state.api.replay_control(replay.paused ? "play" : "pause"); await poll();
}

function setMonitorTxControlsBusy(busy) {
  $$("#monitorTxRows input, #monitorTxRows select, #monitorTxRows button").forEach(control => {
    control.disabled = busy;
  });
}

function parseMonitorData(value) {
  const trimmed = String(value || "").trim();
  if (!trimmed) return [];
  let tokens = trimmed.split(/[\s,;:_-]+/).filter(Boolean);
  const compact = tokens.length === 1 ? tokens[0].replace(/^0x/i, "") : "";
  if (compact.length > 2) {
    if (compact.length % 2) throw new Error("连续数据必须是偶数字符");
    tokens = compact.match(/.{2}/g) || [];
  }
  if (tokens.length > 8 || tokens.some(token => !/^(?:0x)?[0-9a-f]{1,2}$/i.test(token))) {
    throw new Error("请输入最多 8 个 00..FF 字节");
  }
  return tokens.map(token => parseInt(token.replace(/^0x/i, ""), 16));
}

function monitorTxSpec(row) {
  return {
    name: row.name, id: row.id, extended: !!row.extended,
    data: row.data, cycle_ms: Number(row.cycle_ms),
  };
}

function monitorWritable() {
  const connection = monitorConnection();
  return connection.connected === true && connection.mode === "pcan";
}

function periodicTaskMap() {
  return new Map((monitorSnapshot().periodic || []).map(task => [task.id, task]));
}

function renderMonitorTransmitRows() {
  const tbody = $("#monitorTxRows"); if (!tbody) return;
  const writable = monitorWritable();
  const tasks = periodicTaskMap();
  const signature = JSON.stringify([writable, state.frameSource, monitorUi.txRows,
    [...tasks.values()].map(task => [task.id, task.active, task.count, task.last_error])]);
  if (signature === monitorUi.lastTxSignature) return;
  monitorUi.lastTxSignature = signature;
  text("#monitorTxGate", writable
    ? "可发送 · 操作前确认"
    : "不可发送");
  tbody.innerHTML = monitorUi.txRows.map(row => {
    const task = tasks.get(row.uid);
    const active = task?.active === true;
    const error = task?.last_error;
    if (task) row.count = Number(task.count || 0);
    let dlc = "—";
    try { dlc = parseMonitorData(row.data).length; } catch { /* invalid shown after edit */ }
    return `<tr data-tx-id="${escapeHtml(row.uid)}" class="${active ? "sending" : ""} ${error ? "send-error" : ""}" title="${escapeHtml(error || "")}">`
      + `<td class="tx-enable"><input type="checkbox" data-action="periodic" ${active ? "checked" : ""} ${writable ? "" : "disabled"} aria-label="周期发送 ${escapeHtml(row.name)}"></td>`
      + `<td><input data-field="name" value="${escapeHtml(row.name)}" ${active ? "disabled" : ""} aria-label="发送项名称"></td>`
      + `<td><input data-field="id" value="${escapeHtml(row.id)}" ${active ? "disabled" : ""} aria-label="CAN ID"></td>`
      + `<td><select data-field="extended" ${active ? "disabled" : ""} aria-label="帧类型"><option value="false" ${row.extended ? "" : "selected"}>标准</option><option value="true" ${row.extended ? "selected" : ""}>扩展</option></select></td>`
      + `<td class="tx-dlc">${dlc}</td>`
      + `<td><input data-field="data" value="${escapeHtml(row.data)}" ${active ? "disabled" : ""} placeholder="00 00 00 00" aria-label="十六进制数据"></td>`
      + `<td><input data-field="cycle_ms" type="number" min="20" max="60000" value="${Number(row.cycle_ms || 200)}" ${active ? "disabled" : ""} aria-label="周期毫秒"></td>`
      + `<td class="tx-count">${Number(row.count || 0).toLocaleString()}</td>`
      + `<td><div class="tx-row-actions"><button type="button" data-action="send" ${writable || active ? "" : "disabled"}>发送一次</button><button type="button" class="remove" data-action="remove">移除</button></div></td></tr>`;
  }).join("");
}

function txRowForElement(element) {
  const uid = element.closest("tr")?.dataset.txId;
  return monitorUi.txRows.find(row => row.uid === uid);
}

function updateMonitorTxInput(event) {
  const field = event.target.dataset.field; if (!field) return;
  const row = txRowForElement(event.target); if (!row) return;
  row[field] = field === "cycle_ms" ? Number(event.target.value) : event.target.value;
  if (field === "data") {
    const dlc = event.target.closest("tr").querySelector(".tx-dlc");
    try {
      dlc.textContent = parseMonitorData(row.data).length;
      event.target.classList.remove("invalid");
    } catch {
      dlc.textContent = "!";
      event.target.classList.add("invalid");
    }
  }
  persistMonitorTxRows();
  monitorUi.lastTxSignature = JSON.stringify([monitorWritable(), state.frameSource, monitorUi.txRows,
    [...periodicTaskMap().values()].map(task => [task.id, task.active, task.count, task.last_error])]);
}

function changeMonitorTxControl(event) {
  if (event.target.dataset.action === "periodic") {
    toggleMonitorPeriodic(event.target);
    return;
  }
  const field = event.target.dataset.field; if (!field) return;
  const row = txRowForElement(event.target); if (!row) return;
  row[field] = field === "extended" ? event.target.value === "true"
    : field === "cycle_ms" ? Number(event.target.value) : event.target.value;
  persistMonitorTxRows();
  monitorUi.lastTxSignature = "";
}

function clickMonitorTxAction(event) {
  const button = event.target.closest("button[data-action]"); if (!button) return;
  const row = txRowForElement(button); if (!row) return;
  if (button.dataset.action === "send") confirmMonitorTransmission(row, false);
  if (button.dataset.action === "remove") removeMonitorTxRow(row);
}

function validateMonitorTxRow(row) {
  parseMonitorData(row.data);
  const idText = String(row.id || "").trim().replace(/h$/i, "");
  const id = parseInt(idText.replace(/^0x/i, ""), 16);
  const max = row.extended ? 0x1FFFFFFF : 0x7FF;
  if (!Number.isInteger(id) || id < 0 || id > max) throw new Error(row.extended ? "扩展 ID 超出 29 位范围" : "标准 ID 超出 11 位范围");
  const cycle = Number(row.cycle_ms);
  if (!Number.isInteger(cycle) || cycle < 20 || cycle > 60000) throw new Error("周期必须是 20..60000 ms 的整数");
}

function setMonitorConfirm(row, periodic) {
  validateMonitorTxRow(row);
  const action = periodic ? `启用 ${row.cycle_ms} ms 周期发送` : "发送一次";
  state.pendingMonitorAction = {
    run: async () => {
      setMonitorTxControlsBusy(true);
      const result = periodic
        ? await state.api.configure_monitor_periodic(state.frameSource, row.uid, monitorTxSpec(row), true, true)
        : await state.api.send_monitor_frame(state.frameSource, monitorTxSpec(row), true);
      if (result.ok && !periodic) row.count = Number(row.count || 0) + 1;
      persistMonitorTxRows();
      return result;
    },
    success: periodic ? `${row.name} 已开始周期发送` : `${row.name} 已发送`,
  };
  text("#confirmTitle", periodic ? "确认启用周期发送" : "确认原始帧发送");
  setConfirmModeBadge("当前 PCAN 连接", "warn");
  text("#confirmMessage", `${action}。项目内受保护命令仍需从对应专用页面发送。`);
  text("#confirmPayload", `${row.id} · ${row.extended ? "扩展" : "标准"} · DLC ${parseMonitorData(row.data).length}\n${row.data || "无数据"}`);
  text("#confirmCheckLabel", "我已核对当前物理总线、帧 ID、数据和周期，本次操作由我确认。");
  $("#confirmCheck").checked = false;
  $("#doConfirm").disabled = true;
  $("#confirmDialog").showModal();
}

function confirmMonitorTransmission(row, periodic) {
  try { setMonitorConfirm(row, periodic); }
  catch (error) { toast(String(error.message || error), true); }
}

async function toggleMonitorPeriodic(input) {
  const row = txRowForElement(input); if (!row) return;
  if (input.checked) {
    input.checked = false;
    confirmMonitorTransmission(row, true);
    return;
  }
  setMonitorTxControlsBusy(true);
  try {
    const result = await state.api.configure_monitor_periodic(state.frameSource, row.uid, monitorTxSpec(row), false, true);
    if (!result.ok) toast(result.error || "停止周期发送失败", true);
    else toast(`${row.name} 已停止周期发送`);
    await poll();
  } finally {
    monitorUi.lastTxSignature = "";
    renderMonitorTransmitRows();
  }
}

async function removeMonitorTxRow(row) {
  for (const source of ["main", "vehicle"]) {
    const task = new Map((monitorSnapshot(source).periodic || []).map(item => [item.id, item])).get(row.uid);
    if (task?.active) await state.api.configure_monitor_periodic(source, row.uid, monitorTxSpec(row), false, true);
  }
  monitorUi.txRows = monitorUi.txRows.filter(item => item.uid !== row.uid);
  if (!monitorUi.txRows.length) monitorUi.txRows.push(defaultTxRow());
  persistMonitorTxRows(); renderMonitorTransmitRows();
}
