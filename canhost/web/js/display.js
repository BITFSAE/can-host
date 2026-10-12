/* 车手屏联调页面模块：模拟 CANB 节点与方向盘，经 PCAN 驱动 HMI_Display。
 * 数值表、报文目录和场景由后端 bootstrap.display_bench 提供，本模块只负责渲染与操作。 */

const DB_PREFS_KEY = "canHostDisplayBenchPreferences";
const DB_WHEELS = [["fl", "左前 FL"], ["fr", "右前 FR"], ["rl", "左后 RL"], ["rr", "右后 RR"]];
// 0x509 的驾驶模式半字节：-1 为 ECU 上电默认，车手屏对默认和越界值保持上一个模式。
const DB_ECU_MODES = [[-1, "默认", "ECU 上电默认值，车手屏保持上一个模式"], [0, "S 直线", "直线加速"],
  [1, "Q 避障", "避障"], [2, "C 八字", "八字绕环"], [3, "E 耐久", "耐久"], [7, "无效", "越界值，车手屏忽略"]];
const DB_WHEEL_MODES = [[0, "S", "直线加速"], [1, "Q", "避障"], [2, "C", "八字绕环"], [3, "E", "耐久"]];
const DB_FRESH_FAST_S = 1.0;
const DB_FRESH_SLOW_S = 2.0;

const dbState = { tab: "vehicle", built: false, catalog: null, fieldNodes: [], rangeTimers: new Map() };

function dbDecimals(step) {
  const text = String(step ?? 1);
  return text.includes(".") ? text.split(".")[1].length : 0;
}

function dbField({ label, key, unit, step = 1, title = "" }) {
  const hint = title ? ` title="${escapeHtml(title)}"` : "";
  return `<label class="field"${hint}><span class="field-label">${label}</span>`
    + `<span class="field-input"><input type="number" inputmode="decimal" step="${step}" data-key="${key}" data-dec="${dbDecimals(step)}">`
    + `<em>${unit}</em></span></label>`;
}

function dbSlider({ label, key, unit, step = 1, extra = "", title = "" }) {
  const hint = title ? ` title="${escapeHtml(title)}"` : "";
  return `<div class="db-slider"${hint}><span>${label}</span>`
    + `<input class="db-range" type="range" step="${step}" data-key="${key}" data-dec="${dbDecimals(step)}" aria-label="${label}">`
    + `<span class="field-input"><input type="number" inputmode="decimal" step="${step}" data-key="${key}" data-dec="${dbDecimals(step)}">`
    + `<em>${unit}</em></span>${extra}</div>`;
}

function dbSwitch({ label, key, title = "" }) {
  const hint = title ? ` title="${escapeHtml(title)}"` : "";
  return `<label class="switch"${hint}><input type="checkbox" data-key="${key}">`
    + `<i class="switch-track" aria-hidden="true"></i><span>${label}</span></label>`;
}

function dbFrameChips(ids) {
  return ids.map(id => {
    const frame = dbState.catalog.frames.find(item => item.id === id);
    const timeout = frame?.timeout_ms ? `车手屏约 ${frame.timeout_ms / 1000} s 收不到就显示 --` : "车手屏对这帧没有超时，保持最后的值";
    return `<button type="button" class="db-frame" data-frame-chip="${id}" title="点击停发或恢复。${timeout}">`
      + `${id} ${escapeHtml(frame?.name || "")}</button>`;
  }).join("");
}

function dbNodeSwitch(node) {
  return `<label class="switch db-node-switch" title="一次开关本节点的全部报文；关闭后车手屏对应读数在各自超时后显示 --">`
    + `<input type="checkbox" data-node-switch="${node}"><i class="switch-track" aria-hidden="true"></i><span>整组发送</span></label>`;
}

function dbCard({ title, context, ids, node, body, wide = false }) {
  return `<article class="panel db-card${wide ? " db-wide" : ""}"><header class="panel-head">`
    + `<div class="db-head-title"><h3>${title}</h3><span class="panel-context">${context}</span></div>`
    + `${node ? dbNodeSwitch(node) : ""}</header>`
    + `<div class="db-frames"><div class="db-frame-list">${dbFrameChips(ids)}</div></div>`
    + `<div class="panel-body">${body}</div></article>`;
}

function dbWheelTable() {
  const columns = [["torque_nm", "扭矩 Nm", 1], ["rpm", "转速 rpm", 1], ["motor_c", "电机 ℃", 0.1],
    ["inverter_c", "逆变器 ℃", 0.1], ["igbt_c", "IGBT ℃", 0.1]];
  const cell = (wheel, field, step) => `<td><span class="field-input"><input type="number" step="${step}" `
    + `data-key="wheel.${wheel}.${field}" data-dec="${dbDecimals(step)}" aria-label="${wheel} ${field}"></span></td>`;
  const bulk = columns.map(([field, , step]) => `<td><span class="field-input"><input type="number" step="${step}" `
    + `data-bulk="${field}" data-dec="${dbDecimals(step)}" placeholder="—" aria-label="全部 ${field}"></span></td>`).join("");
  const rows = DB_WHEELS.map(([wheel, name]) => `<tr><th scope="row">${name}</th>`
    + `<td><label class="switch"><input type="checkbox" data-key="wheel.${wheel}.enabled" aria-label="${name} 使能">`
    + `<i class="switch-track" aria-hidden="true"></i></label></td>`
    + columns.map(([field, , step]) => cell(wheel, field, step)).join("")
    + `<td><span class="field-input"><input type="text" spellcheck="false" data-key="wheel.${wheel}.fault" data-format="hex" `
    + `maxlength="8" aria-label="${name} 故障码"></span></td>`
    + `<td class="db-power" data-derived-wheel="${wheel}">—</td></tr>`).join("");
  return `<div class="db-table-scroll"><table class="db-table db-wheel-table"><thead><tr><th>车轮</th><th>使能</th>`
    + columns.map(([, name]) => `<th>${name}</th>`).join("") + `<th title="十六进制，车手屏显示低 16 位">故障码</th>`
    + `<th title="扭矩 × 转速 ÷ 9549">功率 kW</th></tr></thead><tbody>`
    + `<tr class="db-bulk"><th scope="row">全部</th><td></td>${bulk}<td></td><td></td></tr>${rows}</tbody></table></div>`;
}

function dbTireBlock(wheel, name, frameId, order) {
  const points = [1, 2, 3, 4].map(point => `<span class="field-input"><input type="number" step="0.1" `
    + `data-key="tire.${wheel}.t${point}" data-dec="1" aria-label="${name} 测点 ${point}"><em>℃</em></span>`).join("");
  return `<div class="db-tire"><header><b>${name}</b><small>${frameId} · 4 个测点 ${order}</small></header>`
    + `<div class="db-tire-bulk"><span>整轮</span><span class="field-input"><input type="number" step="0.1" `
    + `data-bulk-tire="${wheel}" data-dec="1" placeholder="—" aria-label="${name} 整轮"><em>℃</em></span></div>`
    + `<div class="db-tire-points">${points}</div></div>`;
}

function dbSegmented(attribute, items, label) {
  return `<div class="segmented small" role="group" aria-label="${label}">`
    + items.map(([value, text, title]) => `<button type="button" ${attribute}="${value}"${title ? ` title="${title}"` : ""}>${text}</button>`).join("")
    + `</div>`;
}

function buildDisplayBenchStatics() {
  const catalog = state.bootstrap?.display_bench;
  const root = $("#dbCards");
  if (!catalog || !root || dbState.built) return;
  dbState.catalog = catalog;
  $("#dbScenarios").innerHTML = catalog.scenarios.map(item =>
    `<button type="button" class="db-chip" data-db-command="scenario" data-db-args='${JSON.stringify({ name: item.key })}' `
    + `title="${escapeHtml(item.description)}">${escapeHtml(item.label)}</button>`).join("");

  root.innerHTML = [
    dbCard({
      title: "BMS 包状态", context: "车手屏右栏：SOC、HV 电压 / 电流、PWR 放电功率", ids: ["0x4B0"],
      body: `<div class="db-fields">${dbField({ label: "总压", key: "bms.voltage_v", unit: "V", step: 0.1 })}`
        + dbField({ label: "电流（充电为正）", key: "bms.current_a", unit: "A", step: 0.1 }) + `</div>`
        + dbSlider({ label: "SOC", key: "bms.soc_pct", unit: "%", title: "大于 100 时车手屏判无效" })
        + `<div class="db-flags">${dbSwitch({ label: "电压有效", key: "bms.voltage_valid" })}`
        + dbSwitch({ label: "电流有效", key: "bms.current_valid" }) + dbSwitch({ label: "SOC 有效", key: "bms.soc_valid" })
        + `</div><p class="db-hint">有效位是 BMS 给每项标的“可信”标志，关闭后车手屏该项显示 --。</p>`
        + `<p class="db-hint">车手屏 PWR（放电功率）<b data-derived="hv_power_kw">—</b> kW</p>`,
    }),
    dbCard({
      title: "BMS 功率限值（SOP）", context: "车手屏中栏车速下方：DISCH 放电 / REGEN 回收 两行", ids: ["0x4A0", "0x4A3"], node: "sop",
      body: `<div class="db-fields">${dbField({ label: "放电功率上限", key: "sop.discharge_kw", unit: "kW", step: 0.1 })}`
        + dbField({ label: "回收功率上限", key: "sop.regen_kw", unit: "kW", step: 0.1 })
        + dbField({ label: "允许放电电流", key: "sop.discharge_a", unit: "A", step: 0.1 })
        + dbField({ label: "允许回充电流", key: "sop.regen_a", unit: "A", step: 0.1 }) + `</div>`
        + `<div class="db-flags">${dbSwitch({ label: "功率限值有效", key: "sop.limits_valid", title: "0x4A0 的 LimitsValid 位" })}`
        + dbSwitch({ label: "电流限值有效", key: "sop.inputs_valid", title: "0x4A3 的 InputHealth 最高位" }) + `</div>`
        + `<p class="db-hint">SOP 是电池此刻允许的最大放电/回收功率与电流。关闭有效位后车手屏对应数值显示 --；放电上限为 0 时 DISCH 行变红。</p>`,
    }),
    dbCard({
      title: "ECU 踏板与油压", context: "车手屏中栏：油门条、刹车条", ids: ["0x305"],
      body: dbSlider({ label: "油门", key: "pedal.throttle_pct", unit: "%", step: 0.1 })
        + dbSlider({ label: "刹车油压", key: "pedal.oil_kpa", unit: "kPa", step: 0.1,
          extra: `<small class="db-slider-note">刹车条 <b data-derived="brake_pct">—</b> %</small>` })
        + `<div class="db-fields">${dbField({ label: "转向角", key: "pedal.steering_deg", unit: "°", step: 0.1, title: "车手屏收下但界面不使用" })}</div>`
        + `<p class="db-hint">刹车条暂由油压推算：2.1 kPa 为 0%，4.9 kPa 为 90%</p>`,
    }),
    dbCard({
      title: "PDM 低压", context: "车手屏右栏：LV 低压电压 / 电流 / 功率", ids: ["0x5A0", "0x5A1"], node: "pdm",
      body: `<div class="db-fields">${dbField({ label: "母线电压", key: "pdm.bus_v", unit: "V", step: 0.1 })}`
        + dbField({ label: "母线电流", key: "pdm.bus_a", unit: "A", step: 0.1 })
        + dbField({ label: "功率", key: "pdm.bus_w", unit: "W", step: 0.1 })
        + dbField({ label: "累计电量", key: "pdm.energy_wh", unit: "Wh", step: 0.01 }) + `</div>`
        + `<div class="db-flags">${dbSwitch({ label: "功率按 V×I", key: "pdm.power_auto" })}`
        + dbSwitch({ label: "电流传感器离线", key: "pdm.offline", title: "PDM 读不到 INA226 时，四个字段同时发无效占位值" }) + `</div>`
        + `<p class="db-hint">传感器离线时车手屏低压行显示 --；功率勾选“按 V×I”时由电压乘电流算出。</p>`,
    }),
    dbCard({
      title: "风扇控制器", context: "车手屏右栏：三路风扇转速与占空比", ids: ["0x5A2"], wide: true,
      body: `<div class="db-fields tight">${dbField({ label: "风扇 1 转速", key: "fan.rpm1", unit: "rpm" })}`
        + dbField({ label: "风扇 2 转速", key: "fan.rpm2", unit: "rpm" })
        + dbField({ label: "风扇 3 转速", key: "fan.rpm3", unit: "rpm", title: "第三路没有占空比，车手屏只显示转速" })
        + dbField({ label: "风扇 1 占空比", key: "fan.duty1", unit: "%" })
        + dbField({ label: "风扇 2 占空比", key: "fan.duty2", unit: "%" }) + `</div>`,
    }),
    dbCard({
      title: "ECU 四轮电机", context: "车手屏底栏：电机使能、故障码、TQ 扭矩 / RPM 转速、功率、MOT 电机 / INV 逆变器温度；右栏 TMAX 最高温",
      ids: ["0x502", "0x503", "0x504", "0x505", "0x506", "0x507", "0x508", "0x509"], node: "ecu", wide: true,
      body: `<div class="db-ecu-bar"><div class="db-ecu-group"><span class="db-label" title="0x509 里 ECU 回报的当前驾驶模式，车手屏左栏模式色块以它为准">ECU 回报的驾驶模式</span>`
        + dbSegmented("data-ecu-mode", DB_ECU_MODES, "ECU 回报的驾驶模式")
        + `</div><div class="db-ecu-group"><span class="db-label" title="本卡片各帧的发送间隔；10 ms 是中央库暂定的标称值">发送间隔</span><div id="dbEcuPeriod"></div></div>`
        + `<label class="switch" title="收到车手屏转发的方向盘驾驶模式（0x310）后，模拟 ECU 在 0x509 回报同一模式；关闭则模式保持不变"><input type="checkbox" id="dbEcuFollow">`
        + `<i class="switch-track" aria-hidden="true"></i><span>回报模式跟随方向盘</span></label></div>`
        + dbWheelTable() + `<p class="db-hint">最高温 TMAX <b data-derived="tmax_c">—</b> ℃ · 70 ℃ 起琥珀、85 ℃ 起红</p>`,
    }),
    dbCard({
      title: "胎温模块", context: "车手屏左栏：四轮胎面温度图", ids: ["0x071", "0x072", "0x073", "0x074"], node: "tire", wide: true,
      body: `<div class="db-tires">${dbTireBlock("fl", "左前 FL", "0x072", "内→外")}${dbTireBlock("fr", "右前 FR", "0x071", "外→内")}`
        + `${dbTireBlock("rl", "左后 RL", "0x073", "外→内")}${dbTireBlock("rr", "右后 RR", "0x074", "内→外")}</div>`
        + `<p class="db-hint">每个轮胎 4 个测点，测点次序按帧内排列（标注的是胎面从外到内或从内到外）；车手屏最高显示 200 ℃。</p>`,
    }),
  ].join("");

  $("#dbEcuPeriod").innerHTML = dbSegmented("data-ecu-period", catalog.ecu_periods_ms.map(ms => [ms, `${ms} ms`]), "ECU 报文周期");
  $("#dbFrameRows").innerHTML = catalog.frames.map(frame => {
    const defects = frame.defects.map(key => `<option value="${key}">${escapeHtml(catalog.defects[key] || key)}</option>`).join("");
    const timeout = frame.timeout_ms ? `${(frame.timeout_ms / 1000).toFixed(1)} s` : "无";
    return `<tr data-frame-row="${frame.id}"><td><label class="switch"><input type="checkbox" data-frame-switch="${frame.id}" `
      + `aria-label="${frame.id} 发送开关"><i class="switch-track" aria-hidden="true"></i></label></td>`
      + `<td class="db-id"><i class="db-dot" aria-hidden="true"></i>${frame.id}</td><td class="db-name">${escapeHtml(frame.name)}<small>${escapeHtml(frame.note)}</small></td>`
      + `<td class="db-num"><span data-cell="rate">—</span><small data-cell="count">累计 0</small></td><td class="db-hex" data-cell="data">—</td>`
      + `<td class="db-num">${timeout}</td><td><select data-defect-select="${frame.id}" aria-label="${frame.id} 异常注入">${defects}</select></td></tr>`;
  }).join("");
  $("#dbWheelModes").innerHTML = DB_WHEEL_MODES.map(([mode, letter, name]) =>
    `<button type="button" class="db-big-button" data-wheel-mode="${mode}"><b>${letter}</b><small>${name}</small></button>`).join("");
  $("#dbSlipLevels").innerHTML = Array.from({ length: 8 }, (_, level) =>
    `<button type="button" data-wheel-slip="${level}">${level}</button>`).join("");

  dbState.fieldNodes = $$("#page-display [data-key]");
  dbState.built = true;
  applyDisplayBenchMeta();
  populateDisplayBenchHardware();
}

function applyDisplayBenchMeta() {
  const values = dbState.catalog?.values || {};
  dbState.fieldNodes.forEach(node => {
    const meta = values[node.dataset.key];
    if (!meta || node.type === "checkbox" || node.dataset.format === "hex") return;
    if (meta.min != null) node.min = meta.min;
    if (meta.max != null) node.max = meta.max;
  });
}

/* ---------- 操作 ---------- */

function bindDisplayBenchControls() {
  const page = $("#page-display");
  if (!page) return;
  $("#dbConnectButton").addEventListener("click", openDisplayBenchDialog);
  $("#dbDisconnectButton").addEventListener("click", disconnectDisplayBench);
  $("#dbStartButton").addEventListener("click", connectDisplayBench);
  $("#dbPreview").addEventListener("change", syncDisplayBenchDialog);
  $("#dbPauseButton").addEventListener("click", () => {
    const paused = state.toolSnapshots.display?.controls?.paused === true;
    dbCommand("pause", { paused: !paused });
  });
  $("#dbResumeButton").addEventListener("click", () => dbCommand("resume"));
  $("#dbObserveClear").addEventListener("click", () => dbCommand("wheel_log_clear"));
  $("#dbResetButton").addEventListener("click", () => dbCommand("reset", {}, true));
  $("#dbTabs").addEventListener("click", event => {
    const button = event.target.closest("[data-tab]");
    if (button) setDisplayBenchTab(button.dataset.tab);
  });
  page.addEventListener("click", event => {
    const command = event.target.closest("[data-db-command]");
    if (command && !command.disabled) {
      const args = command.dataset.dbArgs ? JSON.parse(command.dataset.dbArgs) : {};
      return dbCommand(command.dataset.dbCommand, args, command.dataset.dbCommand === "scenario");
    }
    const run = event.target.closest("[data-run]");
    if (run) return dbCommand("run", { mode: run.dataset.run });
    const chip = event.target.closest("[data-frame-chip]");
    if (chip) return dbToggleFrame(chip.dataset.frameChip);
    const ecuMode = event.target.closest("[data-ecu-mode]");
    if (ecuMode) return dbSet({ "ecu.mode": Number(ecuMode.dataset.ecuMode) });
    const period = event.target.closest("[data-ecu-period]");
    if (period) return dbCommand("ecu_period", { ms: Number(period.dataset.ecuPeriod) });
    const wheelMode = event.target.closest("[data-wheel-mode]");
    if (wheelMode) return dbCommand("wheel_mode", { mode: Number(wheelMode.dataset.wheelMode) });
    const slip = event.target.closest("[data-wheel-slip]");
    if (slip) return dbCommand("wheel_slip", { level: Number(slip.dataset.wheelSlip) });
  });
  page.addEventListener("change", event => {
    const node = event.target;
    if (node.matches("[data-key]")) return dbCommitField(node);
    if (node.matches("[data-bulk]")) return dbCommitBulk(node);
    if (node.matches("[data-bulk-tire]")) return dbCommitBulkTire(node);
    if (node.matches("[data-node-switch]")) return dbCommand("node", { node: node.dataset.nodeSwitch, enabled: node.checked });
    if (node.matches("[data-frame-switch]")) return dbCommand("frame", { frame_id: node.dataset.frameSwitch, enabled: node.checked });
    if (node.matches("[data-defect-select]")) return dbCommand("defect", { frame_id: node.dataset.defectSelect, defect: node.value });
    if (node.id === "dbEcuFollow") return dbCommand("ecu_follow", { enabled: node.checked });
  });
  page.addEventListener("input", event => {
    const node = event.target;
    if (!node.matches('input[type="range"][data-key]')) return;
    // 拖动滑块时合并写入，避免每个像素都过一次 PyWebView 桥。
    dbState.rangeTimers.set(node.dataset.key, node);
    node.dataset.dragging = "1";
    if (dbState.rangeFlush) return;
    dbState.rangeFlush = setTimeout(() => {
      dbState.rangeFlush = null;
      const patch = {};
      dbState.rangeTimers.forEach((range, key) => { patch[key] = Number(range.value); });
      dbState.rangeTimers.clear();
      dbSet(patch);
    }, 80);
  });
  page.addEventListener("pointerup", event => {
    const node = event.target;
    if (node.matches?.('input[type="range"][data-key]')) delete node.dataset.dragging;
  });
}

function dbRenderTabHint() {
  const cycling = state.toolSnapshots.display?.controls?.run === "cycle";
  text("#dbTabHint", dbState.tab === "vehicle" && cycling
    ? "行驶循环运行中：灰色数值由循环控制；使能、故障码、模式和有效位仍可手动改"
    : dbState.tabHints?.[dbState.tab] || "");
}

function setDisplayBenchTab(tab) {
  dbState.tab = tab;
  $$("#dbTabs [data-tab]").forEach(button => button.classList.toggle("active", button.dataset.tab === tab));
  $$("#page-display [data-tab-panel]").forEach(panel => panel.classList.toggle("hidden", panel.dataset.tabPanel !== tab));
  dbState.tabHints = {
    vehicle: "修改立即发送给车手屏，无需确认；上方场景按钮一次改多项",
    wheel: "实物方向盘的操作经车手屏转发到 CANB，在上方解读；模拟方向盘才需要第二个 PCAN 通道",
    frames: "逐帧停发可测车手屏的超时行为；异常注入测试丢弃路径",
  };
  dbRenderTabHint();
  try { localStorage.setItem(DB_PREFS_KEY + ".tab", tab); } catch { /* 本地存储可选 */ }
}

async function dbSet(patch) {
  if (!state.api) return toast("应用后端未就绪", true);
  const result = await state.api.display_bench_set(patch);
  if (!result?.ok) toast(result?.error || "写入失败", true);
  else if (state.toolSnapshots.display) Object.assign(state.toolSnapshots.display.values, result.values);
  await poll();
}

async function dbCommand(name, args = {}, announce = false) {
  if (!state.api) return toast("应用后端未就绪", true);
  const result = await state.api.display_bench_command(name, args);
  if (!result?.ok) toast(result?.error || "命令失败", true);
  else if (announce && result.message) toast(result.message);
  await poll();
  return result;
}

async function dbToggleFrame(id) {
  const enabled = state.toolSnapshots.display?.controls?.frames?.[id] !== false;
  return dbCommand("frame", { frame_id: id, enabled: !enabled });
}

function dbCommitField(node) {
  const key = node.dataset.key;
  if (node.type === "checkbox") return dbSet({ [key]: node.checked });
  if (node.dataset.format === "hex") return dbSet({ [key]: node.value.trim() || "0" });
  if (node.value.trim() === "") return poll();
  return dbSet({ [key]: Number(node.value) });
}

function dbCommitBulk(node) {
  if (node.value.trim() === "") return;
  const patch = {};
  DB_WHEELS.forEach(([wheel]) => { patch[`wheel.${wheel}.${node.dataset.bulk}`] = Number(node.value); });
  node.value = "";
  return dbSet(patch);
}

function dbCommitBulkTire(node) {
  if (node.value.trim() === "") return;
  const patch = {};
  [1, 2, 3, 4].forEach(point => { patch[`tire.${node.dataset.bulkTire}.t${point}`] = Number(node.value); });
  node.value = "";
  return dbSet(patch);
}

/* ---------- 连接 ---------- */

function populateDisplayBenchHardware() {
  if (!$("#dbChannel")) return;
  populatePcanSelect("#dbChannel", state.bootstrap || {});
  populatePcanSelect("#dbWheelChannel", state.bootstrap || {});
  const wheel = $("#dbWheelChannel");
  wheel.insertAdjacentHTML("afterbegin", '<option value="">不连接（只模拟 CANB）</option>');
  wheel.disabled = false;
  wheel.value = "";
  restoreDisplayBenchDialog();
}

function restoreDisplayBenchDialog() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(DB_PREFS_KEY) || "null"); } catch { /* 本地存储可选 */ }
  const channels = [...$("#dbChannel").options].map(option => option.value);
  if (saved?.channel && channels.includes(saved.channel)) $("#dbChannel").value = saved.channel;
  if (saved?.wheel_channel && channels.includes(saved.wheel_channel) && saved.wheel_channel !== $("#dbChannel").value) {
    $("#dbWheelChannel").value = saved.wheel_channel;
  }
  const nodes = Array.isArray(saved?.nodes) ? saved.nodes : null;
  $$("#dbNodeChoices input").forEach(input => { input.checked = nodes ? nodes.includes(input.value) : true; });
  $("#dbPreview").checked = state.bootstrap?.display_bench_preview === true && saved?.preview === true;
  $("#dbConfirm").checked = false;
  syncDisplayBenchDialog();
}

function syncDisplayBenchDialog() {
  const previewAllowed = state.bootstrap?.display_bench_preview === true;
  $("#dbPreviewRow").classList.toggle("hidden", !previewAllowed);
  const preview = previewAllowed && $("#dbPreview").checked;
  $("#dbHardwareFields").classList.toggle("disabled", preview);
  $$("#dbHardwareFields select").forEach(select => { select.disabled = preview; });
  $("#dbConfirmRow").classList.toggle("hidden", preview);
  $("#dbConfirm").required = !preview;
}

async function openDisplayBenchDialog() {
  if (!dbState.built) return toast("应用后端未就绪", true);
  await refreshPcanChannels(false);
  $("#dbConnectError").classList.add("hidden");
  restoreDisplayBenchDialog();
  $("#displayBenchDialog").showModal();
}

async function connectDisplayBench() {
  if (!state.api) return toast("应用后端未就绪", true);
  const form = $("#displayBenchDialog form");
  if (!form.reportValidity()) return;
  const nodes = $$("#dbNodeChoices input").filter(input => input.checked).map(input => input.value);
  const config = {
    channel: $("#dbChannel").value, wheel_channel: $("#dbWheelChannel").value, nodes,
    preview: state.bootstrap?.display_bench_preview === true && $("#dbPreview").checked,
    confirmed: $("#dbConfirm").checked,
  };
  const button = $("#dbStartButton");
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = "检测总线中…";
  let result;
  try { result = await state.api.connect_display_bench(config); }
  catch (error) { result = { ok: false, error: String(error) }; }
  button.disabled = false;
  button.removeAttribute("aria-busy");
  button.textContent = "连接并开始发送";
  if (!result?.ok) {
    text("#dbConnectError", result?.error || "连接失败");
    $("#dbConnectError").classList.remove("hidden");
    return;
  }
  try {
    localStorage.setItem(DB_PREFS_KEY, JSON.stringify({
      channel: config.channel, wheel_channel: config.wheel_channel, nodes, preview: config.preview }));
  } catch { /* 本地存储可选 */ }
  $("#displayBenchDialog").close();
  toast(result.message || "车手屏联调已连接");
  await poll();
}

async function disconnectDisplayBench() {
  if (!state.api) return;
  await state.api.disconnect_display_bench();
  toast("车手屏联调已断开");
  await poll();
}

/* ---------- 渲染 ---------- */

function dbFormat(node, value) {
  if (node.dataset.format === "hex") return Number(value).toString(16).toUpperCase().padStart(4, "0");
  const digits = Number(node.dataset.dec ?? 0);
  return Number(value).toFixed(digits);
}

function dbRenderFields(snapshot) {
  const values = snapshot.values;
  const meta = dbState.catalog?.values || {};
  const cycling = snapshot.controls.run === "cycle";
  dbState.fieldNodes.forEach(node => {
    const key = node.dataset.key;
    const value = values[key];
    if (value === undefined) return;
    const locked = cycling && meta[key]?.driven === true;
    const autoPower = key === "pdm.bus_w" && values["pdm.power_auto"];
    node.disabled = locked || autoPower;
    node.title = locked ? "行驶循环运行中，该项由循环控制" : node.title;
    if (document.activeElement === node || node.dataset.dragging === "1") return;
    if (node.type === "checkbox") node.checked = !!value;
    else node.value = dbFormat(node, autoPower ? snapshot.derived.pdm_power_w : value);
  });
  $$("#page-display [data-bulk], #page-display [data-bulk-tire]").forEach(node => { node.disabled = cycling; });
  $$("[data-ecu-mode]").forEach(button => button.classList.toggle("active", Number(button.dataset.ecuMode) === values["ecu.mode"]));
  $$("[data-ecu-period]").forEach(button => button.classList.toggle("active", Number(button.dataset.ecuPeriod) === snapshot.controls.ecu_period_ms));
  if (document.activeElement?.id !== "dbEcuFollow") $("#dbEcuFollow").checked = snapshot.controls.ecu_follow_mode;
}

function dbFrameState(frame, connected, paused) {
  if (!frame.enabled) return "off";
  return connected && !paused ? "sending" : "armed";
}

function dbRenderFrames(snapshot) {
  const { connected } = snapshot.connection;
  const paused = snapshot.controls.paused;
  const byId = new Map(snapshot.frames.map(frame => [frame.id, frame]));
  $$("[data-frame-chip]").forEach(chip => {
    const frame = byId.get(chip.dataset.frameChip);
    if (!frame) return;
    const mode = dbFrameState(frame, connected, paused);
    chip.classList.toggle("sending", mode === "sending");
    chip.classList.toggle("armed", mode === "armed");
    chip.classList.toggle("off", mode === "off");
    chip.setAttribute("aria-pressed", String(frame.enabled));
  });
  $$("[data-node-switch]").forEach(input => {
    if (document.activeElement !== input) input.checked = snapshot.controls.nodes[input.dataset.nodeSwitch] === true;
  });
  $$("#dbFrameRows tr").forEach(row => {
    const frame = byId.get(row.dataset.frameRow);
    if (!frame) return;
    const mode = dbFrameState(frame, connected, paused);
    row.classList.toggle("sending", mode === "sending");
    row.classList.toggle("off", mode === "off");
    const input = row.querySelector("[data-frame-switch]");
    if (document.activeElement !== input) input.checked = snapshot.controls.frames[frame.id] === true;
    const select = row.querySelector("[data-defect-select]");
    if (document.activeElement !== select) select.value = frame.defect;
    select.classList.toggle("defective", frame.defect !== "none");
    row.querySelector('[data-cell="rate"]').textContent = mode === "sending" && connected
      ? `${frame.period_ms} ms · ${fmt(frame.hz, 0)} Hz` : `${frame.period_ms} ms`;
    row.querySelector('[data-cell="count"]').textContent = `累计 ${frame.count.toLocaleString()}`;
    row.querySelector('[data-cell="data"]').textContent = frame.data || "—";
  });
}

function dbRenderDerived(snapshot) {
  const derived = snapshot.derived;
  $$("#page-display [data-derived]").forEach(node => {
    const value = derived[node.dataset.derived];
    node.textContent = value == null ? "—" : String(value);
  });
  $$("#page-display [data-derived-wheel]").forEach(node => {
    node.textContent = fmt(derived.wheel_power_kw[node.dataset.derivedWheel], 1);
  });
}

function dbStatusTag(snapshot) {
  const { connection, controls, conflict, stats } = snapshot;
  if (connection.state === "error") return ["连接故障", "bad"];
  if (!connection.connected) return ["未连接", "neutral"];
  if (conflict) return ["总线冲突 · 已暂停", "bad"];
  if (controls.paused) return ["已暂停", "warn"];
  if (stats.tx_recent_errors > 0) return ["发送异常", "bad"];
  if (connection.mode === "preview") return ["预览 · 不发送", "warn"];
  return ["发送中", "ok"];
}

function dbRenderStatus(snapshot) {
  const { connection, controls, conflict, stats, display } = snapshot;
  const [label, tone] = dbStatusTag(snapshot);
  const tag = $("#dbStatus");
  tag.textContent = label;
  tag.className = `tag ${tone}`;
  const active = connection.active === true;
  $("#dbConnectButton").classList.toggle("hidden", active);
  $("#dbDisconnectButton").classList.toggle("hidden", !active);
  const live = connection.connected;
  $("#dbPauseButton").disabled = !live;
  $("#dbPauseButton").textContent = controls.paused ? "继续发送" : "暂停发送";
  $$("#dbRunMode [data-run]").forEach(button => button.classList.toggle("active", button.dataset.run === controls.run));

  const cycle = snapshot.cycle;
  const info = $("#dbCycleInfo");
  info.classList.toggle("hidden", !cycle);
  if (cycle) {
    info.textContent = `${cycle.phase} · 仿真车速 ${fmt(cycle.speed_kmh, 0)} km/h`;
    info.title = "仿真车速只用于推算转速；车手屏显示的车速来自它自己的 GNSS";
  }
  const sendingFrames = snapshot.frames.filter(frame => frame.sending).length;
  text("#dbTxSummary", !live
    ? (connection.state === "error" ? "连接已中断，请断开后重连" : "未连接：设置好数值后点击「连接车手屏」")
    : connection.mode === "preview" ? `预览中：${sendingFrames} 个报文就绪，未发送到总线`
      : controls.paused ? "全部报文已暂停" : `${sendingFrames} 个报文 · ${fmt(stats.tx_hz, 0)} 帧/s · 累计 ${stats.tx_total.toLocaleString()}`);

  const alive = $("#dbAliveTag");
  if (!live) { alive.textContent = "车手屏 —"; alive.className = "tag neutral"; }
  else if (connection.mode === "preview") { alive.textContent = "车手屏 预览无回传"; alive.className = "tag neutral"; }
  else if (display.online) { alive.textContent = `车手屏 在线 · ${fmt(display.age, 1)} s`; alive.className = "tag ok"; }
  else if (display.age != null) { alive.textContent = `车手屏 无应答 · ${fmt(display.age, 0)} s`; alive.className = "tag warn"; }
  else { alive.textContent = "车手屏 等待数据"; alive.className = connection.age > 3 ? "tag warn" : "tag neutral"; }

  const notice = $("#dbNotice");
  let message = "";
  let bad = false;
  if (conflict) {
    message = `检测到其他节点也在发送 ${conflict.id}（${conflict.name}），已暂停全部发送。确认总线上没有真实节点后再继续，或断开后在连接设置里取消勾选对应节点。`;
    bad = true;
  } else if (connection.state === "error") {
    message = connection.error || "PCAN 连接故障";
    bad = true;
  } else if (live && stats.tx_recent_errors > 0) {
    message = `发送失败：${stats.tx_last_error || "未知错误"}。检查车手屏供电、接线和终端电阻。`;
  }
  notice.classList.toggle("hidden", !message);
  notice.classList.toggle("bad", bad);
  text("#dbNoticeText", message);
  $("#dbResumeButton").classList.toggle("hidden", !conflict);
}

function dbRenderWheel(snapshot) {
  const { connection, values } = snapshot;
  const ready = connection.connected && connection.wheel_connected;
  $$("#page-display [data-wheel-mode], #page-display [data-wheel-slip], #dbWheelButtons button").forEach(button => { button.disabled = !ready; });
  $$("[data-wheel-mode]").forEach(button => button.classList.toggle("active", Number(button.dataset.wheelMode) === values["wheelsw.mode"]));
  $$("[data-wheel-slip]").forEach(button => button.classList.toggle("active", Number(button.dataset.wheelSlip) === values["wheelsw.slip"]));
  const status = $("#dbWheelStatus");
  if (!connection.connected) { status.textContent = "未连接"; status.className = "tag neutral"; }
  else if (connection.mode === "preview") { status.textContent = "预览 · 不发送"; status.className = "tag warn"; }
  else if (connection.wheel_connected) { status.textContent = `CAN2 · ${connection.wheel_channel}`; status.className = "tag ok"; }
  else { status.textContent = "未连接方向盘通道"; status.className = "tag neutral"; }
  text("#dbWheelHint", ready ? "点击即发送一帧，和实物方向盘按键一样只发一次"
    : "需要第二个 PCAN 通道接到车手屏 CAN2：断开后在连接设置里选择方向盘通道");
}

function dbRenderObserver(snapshot) {
  const observer = snapshot.wheel_observer;
  const { connection, display } = snapshot;
  const tag = $("#dbObserveTag");
  if (!connection.connected) { tag.textContent = "未连接"; tag.className = "tag neutral"; }
  else if (connection.mode === "preview") { tag.textContent = "预览 · 无回传"; tag.className = "tag warn"; }
  else if (observer.summary.bad > 0) { tag.textContent = `发现 ${observer.summary.bad} 处异常`; tag.className = "tag bad"; }
  else if (display.online) { tag.textContent = "观察中 · 车手屏在线"; tag.className = "tag ok"; }
  else { tag.textContent = "观察中 · 车手屏无应答"; tag.className = "tag warn"; }

  const ago = age => age == null ? "" : `${fmt(age, 1)} s 前`;
  dbSetRow("#dbKnobValue", observer.knob ? observer.knob.name : "—");
  dbSetRow("#dbKnobAge", observer.knob ? `0x310 · ${ago(observer.knob.age)}` : "尚未操作");
  dbSetRow("#dbSlipValue", observer.slip ? String(observer.slip.level) : "—");
  dbSetRow("#dbSlipAge", observer.slip ? `0x703 · ${ago(observer.slip.age)}` : "尚未收到");
  dbSetRow("#dbLapValue", observer.lap.count ? `${observer.lap.count} 次` : "—");
  dbSetRow("#dbLapAge", observer.lap.count ? `最近 ${ago(observer.lap.age)}` : "尚未按下");
  dbSetRow("#dbClearValue", observer.clear.count ? `${observer.clear.count} 次` : "—");
  dbSetRow("#dbClearAge", observer.clear.count ? `最近 ${ago(observer.clear.age)}` : "尚未按下");
  text("#dbObserveOk", String(observer.summary.ok));
  text("#dbObserveBad", String(observer.summary.bad));
  $("#dbObserveBad").classList.toggle("bad", observer.summary.bad > 0);

  const body = $("#dbObserveRows");
  const signature = observer.rows.map(row => row.id).join(",");
  if (body.dataset.signature !== signature) {
    body.dataset.signature = signature;
    const verdict = { ok: ["✓ 正确", "ok"], warn: ["⚠ 需核对", "warn"], bad: ["✗ 异常", "bad"] };
    body.innerHTML = observer.rows.length ? observer.rows.map(row => {
      const [label, tone] = verdict[row.status];
      const notes = row.notes.map(note => `<small>${escapeHtml(note)}</small>`).join("");
      return `<tr><td class="db-num">${row.time}</td><td class="db-name">${escapeHtml(row.action)}</td>`
        + `<td class="db-hex">${escapeHtml(row.frame)}</td><td class="db-verdict ${tone}">${label}${notes}</td></tr>`;
    }).join("") : '<tr><td colspan="4" class="db-empty">还没有记录：操作方向盘后，车手屏发出的帧会出现在这里。</td></tr>';
  }
}

function dbSetRow(id, content, stale = false) {
  const node = $(id);
  if (!node) return;
  node.textContent = content;
  node.classList.toggle("data-stale", stale);
}

function dbFresh(item, limit) {
  return item && isFresh(item.age, limit);
}

function dbRenderSide(snapshot) {
  const { display, connection } = snapshot;
  const preview = connection.connected && connection.mode === "preview";
  const waiting = preview ? "预览无回传" : "等待数据";
  const noEvent = preview ? "预览无回传" : "尚未收到";
  const row = (id, item, limit, format) => {
    if (!item) return dbSetRow(id, waiting);
    dbSetRow(id, dbFresh(item, limit) ? format(item.data) : `已过期 · ${fmt(item.age, 1)} s 前`, !dbFresh(item, limit));
  };
  row("#dbSpeed", display.speed, DB_FRESH_FAST_S, data => `${fmt(data.speed_kmh, 1)} km/h`);
  row("#dbPosition", display.position, DB_FRESH_FAST_S,
    data => `${fmt(Math.abs(data.latitude_deg), 6)}°${data.latitude_deg < 0 ? "S" : "N"} ${fmt(Math.abs(data.longitude_deg), 6)}°${data.longitude_deg < 0 ? "W" : "E"}`);
  row("#dbHeading", display.motion, DB_FRESH_FAST_S,
    data => `航向 ${fmt(data.heading_deg, 1)}° · 航迹 ${fmt(data.track_deg, 1)}° · ${fmt(data.altitude_m, 1)} m`);
  row("#dbGpsFlags", display.motion, DB_FRESH_FAST_S, data => [
    data.rmc_valid ? "车速有效" : "车速无效", data.position_valid ? "位置有效" : "位置无效",
    data.heading_valid ? "航向有效" : "航向无效", `信号 ${data.signal_level}/4`].join(" · "));
  row("#dbLapDiag", display.motion, DB_FRESH_FAST_S, data => {
    const names = state.bootstrap?.display_bench?.lap_diag || {};
    return `${data.lap_active ? "计时中" : "未计时"} · ${names[data.lap_diag_state] ?? `状态 ${data.lap_diag_state}`}`;
  });
  row("#dbOdometer", display.status, DB_FRESH_SLOW_S,
    data => `${fmt(data.odometer_km, 1)} km · ${data.satellites} 颗卫星 · 信噪比均/高 ${data.avg_snr}/${data.max_snr}`);
  row("#dbLapCount", display.status, DB_FRESH_SLOW_S, data => `${data.lap_count} 圈`);
  row("#dbLapTimes", display.lap, DB_FRESH_FAST_S,
    data => `当前 ${fmt(data.current_s, 2)} · 上圈 ${fmt(data.last_s, 2)} · 最快 ${fmt(data.best_s, 2)} · 差 ${fmt(data.delta_s, 2)} s`);
  const steering = display.steering;
  dbSetRow("#dbLastCommand", steering
    ? `滑级 ${steering.data.slip_level}${steering.data.lap_toggle ? " · 圈速计时键" : ""}${steering.data.error_clear ? " · 消错键" : ""} · ${fmt(steering.age, 1)} s 前`
    : noEvent);
  const request = display.mode_request;
  const modeNames = ["S 直线", "Q 避障", "C 八字", "E 耐久"];
  dbSetRow("#dbLastMode", request
    ? `${request.data.mode == null ? `无效码 0x${request.data.code.toString(16).toUpperCase()}` : modeNames[request.data.mode]} · ${fmt(request.age, 1)} s 前`
    : noEvent);

  const events = $("#dbEvents");
  const signature = JSON.stringify(snapshot.events.map(item => item.time + item.text));
  if (events.dataset.signature !== signature) {
    events.dataset.signature = signature;
    events.innerHTML = snapshot.events.length
      ? snapshot.events.slice(0, 12).map(item => `<li class="${item.kind}"><time>${item.time}</time><span>${escapeHtml(item.text)}</span></li>`).join("")
      : '<li class="empty">方向盘操作和车手屏的转发、合并指令会按时间列在这里。</li>';
  }
  const stats = snapshot.stats;
  text("#dbRxSummary", `接收 ${stats.rx_total.toLocaleString()} 帧 · 迟发 ${stats.late}`);
}

function renderDisplayBench() {
  const snapshot = state.toolSnapshots.display;
  if (!snapshot || !dbState.built) return;
  if (!dbState.tabRestored) {
    dbState.tabRestored = true;
    let tab = "vehicle";
    try { tab = localStorage.getItem(DB_PREFS_KEY + ".tab") || tab; } catch { /* 本地存储可选 */ }
    setDisplayBenchTab(["vehicle", "wheel", "frames"].includes(tab) ? tab : "vehicle");
  }
  dbRenderStatus(snapshot);
  dbRenderTabHint();
  dbRenderFields(snapshot);
  dbRenderFrames(snapshot);
  dbRenderDerived(snapshot);
  dbRenderWheel(snapshot);
  dbRenderObserver(snapshot);
  dbRenderSide(snapshot);
}
