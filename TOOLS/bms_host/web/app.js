const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const state = {
  api: null,
  bootstrap: null,
  snapshot: null,
  toolSnapshots: { bench: null, ivt: null, fan: null },
  page: "overview",
  cellMode: "voltage",
  frameKind: "all",
  framePaused: false,
  pausedFrames: [],
  pollTimer: null,
  pollInFlight: false,
  pendingCommand: null,
  pendingIvtAction: null,
  pendingFanCommand: null,
  inputsInitialized: { thresholds: false, switches: false, charge: false },
  dirty: { thresholds: false, switches: false, charge: false, direction: false, chargerType: false, fan: false },
  recording: false,
  onlyActiveAlarms: false,
  uiScale: 1,
  lastZoomWheelAt: 0,
  chargeTiming: { active: false, elapsedMs: 0, lastTickMs: null, averageCurrentA: null, currentSumA: 0, currentSamples: 0, connectionKey: null },
  saveWatch: null,
};

const PACK_CAPACITY_AH = 16.2;
const UI_SCALE_STEPS = [0.8, 0.9, 1, 1.1, 1.2, 1.3];
const UI_SCALE_DEFAULT = 1.1;
const MACHINE_STATE_CLASSES = { 2: "self-test", 3: "standby", 4: "precharge", 5: "hv-on", 7: "fault" };
const SAVE_KINDS = {
  alarm_thresholds: "config", alarm_switches: "config", charge_config: "config",
  charger_type: "config", current_direction: "direction",
};
const SAVE_LABELS = {
  alarm_thresholds: "告警阈值", alarm_switches: "告警开关", charge_config: "充电请求",
  charger_type: "充电机类型", current_direction: "电流方向",
};
const IMD_FREQUENCY_NOTES = {
  0: "无有效 PWM", 1: "DCP", 2: "DCP", 3: "SST", 4: "设备错误", 5: "接地线错误", 15: "未知频率",
};

const ALARM_RULE_TEXTS = [
  null, null, null, null,
  "采样线开路 · 下一处理周期", "温度线开路 · 下一处理周期",
  "压差 ≥800 mV · 即时", "温差 ≥30 °C · 即时",
  "总压 ≥578.2 V · 480 ms", "总压 ≤415.4 V · 480 ms",
  "风扇/UART/RTC/Flash异常 · 二级", "≤10% 二级 / ≤5% 一级",
  "充电电流 >15.0 A · 270 ms", "放电电流 <−180.0 A · 270 ms",
  "保留位 · 算法未启用", "保留位 · 算法未启用",
  "任一从控数据未就绪 · 即时", "预充超时或失败 · 复位前锁存",
  "CAN/TIM启动失败 · 本次上电锁存", "单体累加与U1差 >100 V · 约1.5 s",
  "离线约360 ms / 状态异常270 ms", "HAL错误或Bus-off · 约3 s无新错误后清除",
  "高压中HV_ACC请求消失 · 事件记录", "充电机反馈 >500 ms · 按在线源定级",
  "20 ms重试25次失败 · 复位前锁存",
  "BMU 1 通信离线", "BMU 2 通信离线", "BMU 3 通信离线",
  "BMU 4 通信离线", "BMU 5 通信离线", "BMU 6 通信离线",
  "IVT U1失联约360 ms",
];

const PAGE_ORDER = ["overview", "cells", "alarms", "frames", "control", "bench", "ivt", "fan"];
const DATA_FRESH_MAX_S = 1.5;
const SLOW_DATA_FRESH_MAX_S = 2.5;
const RTC_REPLY_FRESH_MAX_S = 5;

function fmt(value, digits = 1, fallback = "—") {
  return value === null || value === undefined || Number.isNaN(value) ? fallback : Number(value).toFixed(digits);
}
function isFresh(age, limit = DATA_FRESH_MAX_S) { return age != null && age <= limit; }
function text(id, value) { const node = $(id); if (node) node.textContent = value; }
function setClass(id, className, enabled) { const node = $(id); if (node) node.classList.toggle(className, !!enabled); }
function liveFaultState(snapshot) {
  const fault = snapshot?.fault || {};
  const faultFresh = fault.received === true && isFresh(fault.age);
  const alarmLevelsFresh = (snapshot?.alarms || []).some(item => item.received === true && isFresh(item.age, SLOW_DATA_FRESH_MAX_S));
  return { fault, faultFresh, alarmLevelsFresh, completeFresh: faultFresh && alarmLevelsFresh,
    anyFresh: faultFresh || alarmLevelsFresh };
}

function resetChargeTiming() {
  state.chargeTiming = { active: false, elapsedMs: 0, lastTickMs: null, averageCurrentA: null,
    currentSumA: 0, currentSamples: 0, connectionKey: null };
}
function toast(message, error = false) {
  const node = document.createElement("div");
  node.className = `toast${error ? " error" : ""}`;
  node.textContent = message;
  $("#toastStack").append(node);
  setTimeout(() => node.remove(), 3600);
}

function applyUiScale(scale, announce = true) {
  const nearest = UI_SCALE_STEPS.reduce((best, value) =>
    Math.abs(value - scale) < Math.abs(best - scale) ? value : best
  );
  state.uiScale = nearest;
  // CSS zoom is supported by the Edge WebView2 engine and scales the complete
  // interface while recalculating its available layout width and height.
  document.documentElement.style.zoom = String(nearest);
  document.documentElement.style.setProperty("--app-height", `${100 / nearest}vh`);
  try { localStorage.setItem("bmsUiScaleV2", String(nearest)); } catch { /* storage may be disabled */ }
  if (state.page === "overview") setTimeout(drawTrend, 0);
  if (announce) toast(`界面缩放 ${Math.round(nearest * 100)}%`);
}

function restoreUiScale() {
  let saved = UI_SCALE_DEFAULT;
  try { saved = Number(localStorage.getItem("bmsUiScaleV2")) || UI_SCALE_DEFAULT; } catch { /* storage may be disabled */ }
  applyUiScale(saved, false);
}

function stepUiScale(direction) {
  const index = UI_SCALE_STEPS.indexOf(state.uiScale);
  const next = Math.max(0, Math.min(UI_SCALE_STEPS.length - 1, index + direction));
  applyUiScale(UI_SCALE_STEPS[next]);
}

async function waitForApi() {
  if (window.pywebview?.api) return window.pywebview.api;
  await new Promise(resolve => window.addEventListener("pywebviewready", resolve, { once: true }));
  return window.pywebview.api;
}

async function init() {
  restoreUiScale();
  bindNavigation();
  bindControls();
  buildAlarmMatrix();
  try {
    state.api = await waitForApi();
    state.bootstrap = await state.api.bootstrap();
    text("#appVersion", `v${state.bootstrap.version || "—"}`);
    text("#appVersionDate", state.bootstrap.version_date || "—");
    populateConnectionOptions();
    populateToolChannelOptions();
    buildSwitchList();
    await poll();
  } catch (error) {
    toast(`应用后端未就绪：${error}`, true);
    const fallback = { simulation_enabled: false, channels: ["PCAN_USBBUS1"], profiles: [
      { key: "can1", name: "CAN1 · F405 主控 / 从控 / 工具", bitrate: 500000 },
      { key: "canb", name: "CANB · IVT / ECU / Chroma · 500 kbit/s", bitrate: 500000 },
      { key: "canb_legacy", name: "CANB · Legacy / IVT · 250 kbit/s", bitrate: 250000 },
    ]};
    populateConnectionOptions(fallback);
    populateToolChannelOptions(fallback);
  }
}

function bindNavigation() {
  $("#nav").addEventListener("click", event => {
    const button = event.target.closest(".nav-item");
    if (button) showPage(button.dataset.page);
  });
  $$('[data-goto]').forEach(button => button.addEventListener("click", () => showPage(button.dataset.goto)));
}

function showPage(page) {
  if (!PAGE_ORDER.includes(page)) return;
  state.page = page;
  const isTool = ["bench", "ivt", "fan"].includes(page);
  if (isTool) $(".nav-tools")?.setAttribute("open", "");
  $$(".nav-item").forEach(node => node.classList.toggle("active", node.dataset.page === page));
  $$(".page").forEach(node => node.classList.toggle("active", node.id === `page-${page}`));
  document.body.classList.toggle("is-tool-page", isTool);
  // Every page owns a different information depth. Keeping the previous page's
  // scroll offset can make a short page appear blank after navigation.
  $("#main").scrollTop = 0;
  if (state.snapshot) render();
  schedulePoll(0);
}

function bindControls() {
  $("#connectButton").addEventListener("click", () => $("#connectDialog").showModal());
  $("#connectProfile").addEventListener("change", updateConnectionDialog);
  $("#doConnect").addEventListener("click", connectCan);
  $("#disconnectButton").addEventListener("click", disconnectCan);
  $("#connectBenchButton").addEventListener("click", connectBench);
  $("#disconnectBenchButton").addEventListener("click", disconnectBench);
  $("#connectIvtButton").addEventListener("click", connectIvt);
  $("#disconnectIvtButton").addEventListener("click", disconnectIvt);
  $("#connectFanButton").addEventListener("click", connectFan);
  $("#disconnectFanButton").addEventListener("click", disconnectFan);
  $("#fanModeSelect").addEventListener("change", renderFanControlFields);
  $("#sendFanControl").addEventListener("click", () => {
    const mode = $("#fanModeSelect").value;
    const values = { mode: +mode, duty1_pct: +$("#fanDuty1Input").value || 0, duty2_pct: +$("#fanDuty2Input").value || 0,
                     lease_s: +$("#fanLeaseInput").value || 10 };
    if (mode === "1") {
      const summary = `模式 手动 · PWM1 ${values.duty1_pct}% · PWM2 ${values.duty2_pct}% · 有效 ${values.lease_s} s`;
      confirmFanCommand("fan_control", values, "发送风扇手动模式", summary + "\n有效时间到期后自动回到温控模式；需要保持时请按短于有效时间的间隔重发。");
    } else if (mode === "2") {
      confirmFanCommand("fan_control", { mode: 2, duty1_pct: 0, duty2_pct: 0, lease_s: values.lease_s },
        "发送风扇关闭命令", `模式 关闭 · 两路 0% · 有效 ${values.lease_s} s\n到期自动回到温控模式。`, true);
    } else {
      confirmFanCommand("fan_control", { mode: 0, duty1_pct: 0, duty2_pct: 0, lease_s: 0 },
        "回到风扇自动温控", "模式 自动 · 由 FanController 按电机/控制器温度自动调速。");
    }
  });
  $("#sendFanCurve").addEventListener("click", () => {
    const values = { temp_off_c: +$("#fanTempOffInput").value, temp_on_c: +$("#fanTempOnInput").value,
                     temp_full_c: +$("#fanTempFullInput").value, min_duty_pct: +$("#fanMinDutyInput").value,
                     ramp_up_pct_per_s: +$("#fanRampUpInput").value };
    if ([values.temp_off_c, values.temp_on_c, values.temp_full_c, values.min_duty_pct, values.ramp_up_pct_per_s].some(value => !Number.isFinite(value)))
      return toast("请完整填写温控曲线五个参数", true);
    confirmFanCommand("fan_curve", values, "写入风扇温控曲线",
      `关闭 ${values.temp_off_c} °C · 启动 ${values.temp_on_c} °C · 全速 ${values.temp_full_c} °C\n`
      + `最低运行 ${values.min_duty_pct}% · 上升 ${values.ramp_up_pct_per_s}%/s\n策略只保存在 RAM，复位后恢复默认。`);
  });
  $("#sendFanFailsafe").addEventListener("click", () => {
    const values = { strategy: +$("#fanStrategySelect").value, fallback1_duty_pct: +$("#fanFallback1Input").value,
                     fallback2_duty_pct: +$("#fanFallback2Input").value, stale_hold_s: +$("#fanHoldInput").value,
                     ramp_down_pct_per_s: +$("#fanRampDownInput").value };
    if ([values.fallback1_duty_pct, values.fallback2_duty_pct, values.stale_hold_s, values.ramp_down_pct_per_s].some(value => !Number.isFinite(value)))
      return toast("请完整填写失联策略参数", true);
    const strategyNames = { 0: "保持最后目标", 1: "固定保底", 2: "全速" };
    confirmFanCommand("fan_failsafe", values, "写入风扇失联策略",
      `策略 ${strategyNames[values.strategy]} · 保底 ${values.fallback1_duty_pct}% / ${values.fallback2_duty_pct}%\n`
      + `保持 ${values.stale_hold_s} s · 下降 ${values.ramp_down_pct_per_s}%/s\n策略只保存在 RAM，复位后恢复默认。`);
  });
  $("#fanQueryButton").addEventListener("click", () => {
    confirmFanCommand("fan_query", {}, "查询风扇当前策略", "发送操作码 0x05；FanController 随后回报温控曲线 0x5A6 和失联策略 0x5A7。");
  });
  $("#fanRestoreButton").addEventListener("click", () => {
    confirmFanCommand("fan_restore_defaults", {}, "恢复风扇默认策略",
      "恢复默认温控曲线（35/40/60℃ · 30% · 20%/s）和失联策略（固定保底 50%/50% · 保持 5s），并回到自动温控模式。", true);
  });
  $("#onlyAbnormal").addEventListener("change", renderCells);
  $("#frameType").addEventListener("click", event => {
    const button = event.target.closest("button"); if (!button) return;
    state.frameKind = button.dataset.kind;
    $$("#frameType button").forEach(node => node.classList.toggle("active", node === button));
    renderFrames();
  });
  $("#frameSearch").addEventListener("input", renderFrames);
  $("#pauseFrames").addEventListener("change", event => {
    state.framePaused = event.target.checked;
    if (state.framePaused) state.pausedFrames = state.snapshot?.raw_frames || [];
    renderFrames();
  });
  $("#sendThresholds").addEventListener("click", () => confirmCommand(
    "alarm_thresholds",
    { ov_mv: +$("#ovInput").value, uv_mv: +$("#uvInput").value, ot_c: +$("#otInput").value, ut_c: +$("#utInput").value },
    "写入单体告警阈值", `OV ${$("#ovInput").value} mV\nUV ${$("#uvInput").value} mV\nOT ${$("#otInput").value} °C\nUT ${$("#utInput").value} °C`
  ));
  $("#sendSwitches").addEventListener("click", () => {
    const switches = {};
    $$("#switchList input").forEach(input => switches[input.dataset.key] = input.checked);
    confirmCommand("alarm_switches", { switches }, "写入告警开关", "将当前页面的 19 个开关作为一组写入主控。写入后以周期回报值为准。");
  });
  $("#sendChargeConfig").addEventListener("click", () => confirmCommand(
    "charge_config", { voltage_v: +$("#chargeVoltage").value, current_a: +$("#chargeCurrent").value },
    "写入充电请求", `${$("#chargeVoltage").value} V  /  ${$("#chargeCurrent").value} A`
  ));
  $("#syncRtc").addEventListener("click", () => {
    const now = new Date(); const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
    confirmCommand("rtc", { datetime: local }, "RTC 校时", `写入上位机本地时间：${local.replace("T", " ")}`);
  });
  $("#sendCurrentDirection").addEventListener("click", () => {
    const value = $("#currentDirection").value;
    if (!["0", "1"].includes(value)) return toast("尚未收到电流方向，暂不能写入", true);
    const inverted = value === "1";
    confirmCommand("current_direction", { inverted }, "写入电流方向", `明确设置为“${inverted ? "反转" : "正常"}”。此设置保存到 Flash Sector2。`);
  });
  $("#sendChargerType").addEventListener("click", () => {
    const value = $("#chargerType").value;
    if (!["0", "1"].includes(value)) return toast("尚未收到充电机类型，暂不能写入", true);
    const charger_type = +value;
    confirmCommand("charger_type", { charger_type }, "切换充电机类型",
      `设置为“${charger_type ? "Chroma · 500 kbit/s" : "Legacy · 250 kbit/s"}”。实体充电按钮决定是否进入充电模式。`);
  });
  $("#clearFaultLog").addEventListener("click", () => {
    confirmCommand("log_clear", {}, "清除 Flash 故障日志",
      "发送独立的三字节确认请求。不会清除当前实时告警，也不会解除故障保持。", true);
  });
  $("#readFlashLog").addEventListener("click", async () => {
    $("#readFlashLog").disabled = true;
    const result = await state.api.read_flash_fault_logs(50);
    $("#readFlashLog").disabled = false;
    if (!result.ok) return toast(result.error || "读取 Flash 日志失败", true);
    toast(`已读取 ${result.records.length} / ${result.count} 条重要故障日志`);
    await poll();
  });
  $("#faultReset").addEventListener("click", () => confirmCommand("fault_reset", {}, "故障保持复位", "只有实时一级、二级告警已清除且 HV_ACC 释放时，主控才会执行复位。", true));
  $("#readIvtConfig").addEventListener("click", readIvtConfig);
  $("#configureIvt").addEventListener("click", () => {
    const options = readIvtOptions();
    if (!options) return;
    confirmIvtAction("configure", options, "配置 IVT 为 BMS CANB",
      "将停止 IVT，写入 8 个通道和 10 个 CAN ID，保存到 IVT 非易失存储，重启后再逐项读回核对。执行前确认 CANB 上只有目标 IVT。", true);
  });
  $("#switchIvt250").addEventListener("click", () => confirmIvtBitrate(250000));
  $("#switchIvt500").addEventListener("click", () => confirmIvtBitrate(500000));
  $("#sendBenchCommand").addEventListener("click", sendBenchCommand);
  $("#benchCommand").addEventListener("keydown", event => {
    if (event.key === "Enter") sendBenchCommand();
  });
  $$("#page-bench [data-bench-command]").forEach(button => {
    button.addEventListener("click", () => runBenchCommand(button.dataset.benchCommand));
  });
  $("#confirmCheck").addEventListener("change", event => $("#doConfirm").disabled = !event.target.checked);
  $("#doConfirm").addEventListener("click", sendPendingCommand);
  $("#recordButton").addEventListener("click", toggleRecording);
  $("#replayButton").addEventListener("click", openReplay);
  $("#replayPlay").addEventListener("click", toggleReplay);
  $("#replaySpeed").addEventListener("change", event => state.api?.replay_control("speed", +event.target.value));
  $("#replaySeek").addEventListener("change", event => {
    const replay = state.snapshot?.connection?.replay; if (!replay) return;
    state.api?.replay_control("seek", replay.duration * (+event.target.value / 1000));
  });
  ["#ovInput", "#uvInput", "#otInput", "#utInput"].forEach(id => $(id).addEventListener("input", () => state.dirty.thresholds = true));
  ["#chargeVoltage", "#chargeCurrent"].forEach(id => $(id).addEventListener("input", () => state.dirty.charge = true));
  ["#fanTempOffInput", "#fanTempOnInput", "#fanTempFullInput", "#fanMinDutyInput", "#fanRampUpInput",
   "#fanFallback1Input", "#fanFallback2Input", "#fanHoldInput", "#fanRampDownInput", "#fanStrategySelect"]
    .forEach(id => $(id).addEventListener("input", () => state.dirty.fan = true));
  $("#switchList").addEventListener("change", () => {
    state.dirty.switches = true;
    updateSwitchRowState();
  });
  $("#currentDirection").addEventListener("change", () => state.dirty.direction = true);
  $("#chargerType").addEventListener("change", () => state.dirty.chargerType = true);
  $("#onlyActiveAlarms").addEventListener("change", event => {
    state.onlyActiveAlarms = event.target.checked;
    renderAlarms();
  });
  $("#copyFaultCode").addEventListener("click", async () => {
    const code = $("#faultCode").textContent;
    try {
      await navigator.clipboard.writeText(code);
      toast(`已复制故障码 ${code}`);
    } catch {
      toast("剪贴板不可用，请手动选择复制", true);
    }
  });
  document.addEventListener("visibilitychange", () => schedulePoll(document.hidden ? 1000 : 0));
  window.addEventListener("resize", () => {
    if (state.page === "overview") drawTrend();
  });
  window.addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && !event.altKey) {
      const zoomIn = event.key === "+" || event.key === "=" || event.code === "Equal" || event.code === "NumpadAdd";
      const zoomOut = event.key === "-" || event.code === "Minus" || event.code === "NumpadSubtract";
      const zoomReset = event.key === "0" || event.code === "Digit0" || event.code === "Numpad0";
      if (zoomIn || zoomOut || zoomReset) {
        event.preventDefault();
        event.stopImmediatePropagation();
        if (zoomReset) applyUiScale(UI_SCALE_DEFAULT);
        else stepUiScale(zoomIn ? 1 : -1);
        return;
      }
    }
    if (!event.altKey || event.ctrlKey || event.shiftKey) return;
    const index = Number(event.key) - 1;
    if (Number.isInteger(index) && PAGE_ORDER[index]) {
      event.preventDefault();
      showPage(PAGE_ORDER[index]);
    }
  }, true);
  window.addEventListener("wheel", event => {
    if (!event.ctrlKey || Math.abs(event.deltaY) < 1) return;
    event.preventDefault();
    const now = performance.now();
    if (now - state.lastZoomWheelAt < 140) return;
    state.lastZoomWheelAt = now;
    stepUiScale(event.deltaY < 0 ? 1 : -1);
  }, { passive: false, capture: true });
  renderFanControlFields();
}

function populateConnectionOptions(fallback) {
  const data = state.bootstrap || fallback;
  if (!data) return;
  const profiles = [...(data.profiles || [])];
  if (data.simulation_enabled === true && !profiles.some(item => item.key === "simulation")) {
    profiles.unshift({ key: "simulation", mode: "simulation", name: "内置模拟数据 · CAN1 / 开发测试", bitrate: 500000 });
  }
  $("#connectProfile").innerHTML = profiles.map(item =>
    `<option value="${item.key}" data-mode="${item.mode || "pcan"}" data-bitrate="${item.bitrate}">${item.name}</option>`
  ).join("");
  $("#connectChannel").innerHTML = data.channels.map(item => `<option>${item}</option>`).join("");
  updateConnectionDialog();
}

function populateToolChannelOptions(fallback) {
  const data = state.bootstrap || fallback;
  if (!data?.channels) return;
  const options = data.channels.map(item => `<option>${item}</option>`).join("");
  ["#benchChannelSelect", "#ivtChannelSelect", "#fanChannelSelect"].forEach(id => { if ($(id)) $(id).innerHTML = options; });
}

function updateConnectionDialog() {
  const option = $("#connectProfile").selectedOptions[0];
  const simulation = option?.dataset.mode === "simulation";
  $("#channelField").classList.toggle("hidden", simulation);
  text("#connectBitrate", simulation ? "虚拟 CAN1" : `${Number(option?.dataset.bitrate || 500000) / 1000} kbit/s`);
}

async function connectCan() {
  if (!state.api) return toast("应用后端未就绪", true);
  const option = $("#connectProfile").selectedOptions[0];
  const mode = option?.dataset.mode || "pcan";
  resetChargeTiming();
  $("#doConnect").disabled = true; text("#doConnect", "连接中…");
  $("#connectError").classList.add("hidden");
  const result = await state.api.connect_can({
    mode, bus_profile: mode === "simulation" ? "can1" : $("#connectProfile").value,
    channel: mode === "simulation" ? null : $("#connectChannel").value,
    bitrate: Number(option?.dataset.bitrate || 500000),
  });
  $("#doConnect").disabled = false; text("#doConnect", "连接");
  if (result.ok) {
    $("#connectDialog").close(); toast("上位机已连接"); await poll();
  } else {
    text("#connectError", result.error); $("#connectError").classList.remove("hidden");
  }
}

async function disconnectCan() {
  if (!state.api) return;
  resetChargeTiming();
  await state.api.disconnect_can();
  $("#connectDialog").close();
  toast("CAN 已断开");
  await poll();
}

async function connectBench() {
  if (!state.api) return toast("应用后端未就绪", true);
  const button = $("#connectBenchButton");
  button.disabled = true; button.textContent = "连接中…";
  const result = await state.api.connect_bench({ channel: $("#benchChannelSelect").value });
  button.disabled = false; button.textContent = "连接";
  if (!result.ok) return toast(result.error || "台架连接失败", true);
  toast("台架已连接到 F405 CAN1");
  await poll();
}

async function disconnectBench() {
  if (!state.api) return;
  await state.api.disconnect_bench();
  toast("台架已断开");
  await poll();
}

async function connectIvt() {
  if (!state.api) return toast("应用后端未就绪", true);
  const bitrate = Number($("#ivtBitrateSelect").value || 500000);
  const button = $("#connectIvtButton");
  button.disabled = true; button.textContent = "连接中…";
  const result = await state.api.connect_ivt({
    channel: $("#ivtChannelSelect").value,
    bitrate,
    bus_profile: bitrate === 250000 ? "canb_legacy" : "canb",
  });
  button.disabled = false; button.textContent = "连接";
  if (!result.ok) return toast(result.error || "IVT 连接失败", true);
  toast(`IVT 配置通道已连接 · ${bitrate / 1000} kbit/s`);
  await poll();
}

async function disconnectIvt() {
  if (!state.api) return;
  await state.api.disconnect_ivt();
  toast("IVT 配置通道已断开");
  await poll();
}

async function connectFan() {
  if (!state.api) return toast("应用后端未就绪", true);
  const bitrate = Number($("#fanBitrateSelect").value || 500000);
  const button = $("#connectFanButton");
  button.disabled = true; button.textContent = "连接中…";
  const result = await state.api.connect_fan({
    channel: $("#fanChannelSelect").value,
    bitrate,
    bus_profile: bitrate === 250000 ? "canb_legacy" : "canb",
  });
  button.disabled = false; button.textContent = "连接";
  if (!result.ok) return toast(result.error || "风扇通道连接失败", true);
  toast(`风扇通道已连接 · CANB ${bitrate / 1000} kbit/s`);
  await poll();
}

async function disconnectFan() {
  if (!state.api) return;
  await state.api.disconnect_fan();
  toast("风扇通道已断开");
  await poll();
}

function buildSwitchList() {
  const catalog = state.bootstrap?.switch_catalog || [];
  $("#switchList").innerHTML = catalog.map(item =>
    `<label class="switch-row" data-key="${item.key}" title="${item.name} · ${item.code} · ${item.variable}"><i class="switch-dot"></i>`
    + `<span class="switch-label"><b>${item.name}</b><small>${item.code}</small></span>`
    + `<b class="switch-state">等待</b><input type="checkbox" data-key="${item.key}"><span class="switch-track"></span></label>`
  ).join("");
}

/** Refresh the visible on/off state of every alarm switch row on the control page. */
function updateSwitchRowState() {
  const config = state.snapshot?.config || {};
  const hasReport = isFresh(config.switches_age, SLOW_DATA_FRESH_MAX_S) && Object.keys(config.switches || {}).length > 0;
  $$("#switchList input").forEach(input => {
    const row = input.closest(".switch-row");
    const label = row.querySelector(".switch-state");
    const enabled = input.checked;
    row.classList.toggle("on", enabled);
    if (state.dirty.switches) {
      label.textContent = "待写入";
      row.classList.add("dirty");
    } else if (!hasReport) {
      label.textContent = "等待";
      row.classList.remove("dirty", "on");
    } else {
      label.textContent = enabled ? "启用" : "关闭";
      row.classList.remove("dirty");
    }
  });
}

function buildAlarmMatrix() {
  $("#alarmMatrix").innerHTML = Array.from({ length: 32 }, (_, i) =>
    `<div class="alarm-item" data-alarm="${i}" title="统一故障码 bit ${i}"><span><b>告警项 ${i}</b><small>等待主控状态</small></span><em class="alarm-level">未收到</em></div>`
  ).join("");
}

function schedulePoll(delay) {
  if (!state.api) return;
  if (state.pollTimer !== null) clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(poll, delay);
}

async function poll() {
  if (!state.api || state.pollInFlight) return;
  if (state.pollTimer !== null) clearTimeout(state.pollTimer);
  state.pollTimer = null;
  state.pollInFlight = true;
  try {
    state.snapshot = await state.api.get_snapshot();
    if (state.page === "bench" && state.api.get_bench_snapshot) {
      state.toolSnapshots.bench = await state.api.get_bench_snapshot();
    } else if (state.page === "ivt" && state.api.get_ivt_snapshot) {
      state.toolSnapshots.ivt = await state.api.get_ivt_snapshot();
    } else if (state.page === "fan" && state.api.get_fan_snapshot) {
      state.toolSnapshots.fan = await state.api.get_fan_snapshot();
    }
    if (state.snapshot) {
      renderChargeTiming(state.snapshot.overview, state.snapshot.connection, state.snapshot.fault || {});
    }
    render();
  } catch (error) {
    console.error(error);
  } finally {
    state.pollInFlight = false;
    const detailedPage = state.page !== "overview";
    schedulePoll(document.hidden ? 1000 : detailedPage ? 500 : 250);
  }
}

function render() {
  const snap = state.snapshot; if (!snap) return;
  renderConnection(snap.connection);
  renderCellBadge();
  renderAlarmSummary();
  if (state.page === "overview") {
    renderOverview(); renderModules(); renderControls();
  } else if (state.page === "cells") {
    renderCells();
  } else if (state.page === "alarms") {
    renderAlarms(); renderConfig();
  } else if (state.page === "control") {
    renderConfig(); renderControls();
  } else if (state.page === "bench") {
    renderBench();
  } else if (state.page === "ivt") {
    renderIvtConfig();
  } else if (state.page === "fan") {
    renderFan();
  } else if (state.page === "frames") {
    renderReplay();
    if (!state.framePaused) renderFrames();
  }
}

function renderConnection(connection) {
  state.recording = !!connection.recording;
  text("#recordButton", state.recording ? "■ 停止记录" : "● 记录数据");
  setClass("#recordButton", "active", state.recording);
  text("#recordingState", state.recording ? `● 正在记录 ${connection.recording.format === "bmslog" ? "BMSLOG" : "CSV"}` : "");
  text("#rxCount", connection.rx_count.toLocaleString()); text("#txCount", connection.tx_count.toLocaleString());
  setClass("#connectButton", "connected", connection.connected);
  setClass("#connectButton", "simulation", connection.mode === "simulation");
  setClass("#connectButton", "replay", connection.mode === "replay");
  const profileNames = { can1: "CAN1 · F405", canb: "CANB · 500", canb_legacy: "CANB Legacy · 250" };
  const modeName = connection.mode === "simulation" ? "内置模拟数据"
    : connection.mode === "bench" ? "CAN1 从控台架"
    : connection.mode === "replay" ? "历史回放" : connection.channel || "PCAN";
  $("#connectButton b").textContent = connection.connected ? modeName : "连接上位机";
  $("#connectButton small").textContent = connection.connected
    ? connection.mode === "simulation" ? "虚拟 CAN1 · 开发测试"
      : `${profileNames[connection.bus_profile] || "CAN"} · ${(connection.bitrate || 500000) / 1000} kbit/s`
    : "选择 CAN 总线";
  $("#disconnectButton").classList.toggle("hidden", !connection.connected);

  const firmware = state.snapshot?.firmware || {};
  const firmwareText = [
    firmware.variant,
    firmware.charger_variant && firmware.charger_variant !== "Runtime" ? firmware.charger_variant : "",
    firmware.git,
    firmware.build_date ? `构建 ${firmware.build_date}` : "",
  ].filter(Boolean).join(" · ") || "—";
  const firmwareNode = $("#firmwareIdentity");
  if (firmwareNode) {
    firmwareNode.textContent = firmwareText;
    firmwareNode.title = firmwareText;
  }
}

function renderOverview() {
  const { overview: o, relay, hv, imd, connection } = state.snapshot;
  const faultState = liveFaultState(state.snapshot);
  const overviewFresh = connection.summary_age != null && connection.summary_age <= 1.5;
  const stale = !overviewFresh;
  const voltageFresh = overviewFresh && o.voltage_valid;
  const currentFresh = overviewFresh && o.current_valid;
  const socFresh = overviewFresh && o.soc_valid;
  const cellDataFresh = overviewFresh && o.cell_voltage_complete;
  const cellExtremesFresh = cellDataFresh && isFresh(o.cell_extremes_age);
  const cellSumFresh = cellDataFresh && isFresh(o.cell_sum_age);
  const stateName = !overviewFresh ? "等待 CAN 数据" : o.state_name || "等待 CAN 数据";
  text("#stateName", stateName);
  const stateNode = $("#stateName");
  if (stateNode) {
    stateNode.className = `machine-value ${!overviewFresh ? "stale" : MACHINE_STATE_CLASSES[o.state] || "unknown"}`;
    stateNode.title = overviewFresh ? `主控状态：${stateName}` : "等待主控状态";
  }
  const descriptions = { 2: "主控正在等待完整从控采样和 IVT U1。", 3: "高压未闭合，可以进行参数配置。", 4: "预充正在进行，配置命令将被主控忽略。", 5: "高压已闭合，优先监视电流、单体与告警。", 7: "故障已锁存，排除实时故障后再请求复位。" };
  text("#stateDescription", stale ? "主控周期状态帧缺失或已经超时。" : descriptions[o.state] || "正在读取主控状态。");
  text("#overviewClock", new Date().toLocaleTimeString("zh-CN", { hour12: false }));
  text("#overviewFaultCode", faultState.faultFresh ? faultState.fault.code_hex : "等待数据");
  text("#alarmLevelName", faultState.faultFresh ? o.alarm_level_name || "未知" : "等待数据");
  $("#alarmLevelName").className = !faultState.faultFresh || o.alarm_level == null ? "" : o.alarm_level === 1 ? "bad" : o.alarm_level === 2 ? "warn" : "ok";
  text("#packVoltage", voltageFresh ? fmt(o.voltage_v, 1) : "等待数据");
  text("#packCurrent", currentFresh ? fmt(o.current_a, 1) : "等待数据");
  text("#packSoc", socFresh ? fmt(o.soc_pct, 0) : "等待数据");
  $("#socBar").style.width = socFresh ? `${Math.max(0, Math.min(100, o.soc_pct))}%` : "0%";
  const delta = cellExtremesFresh && o.max_cell_mv != null && o.min_cell_mv != null ? o.max_cell_mv - o.min_cell_mv : null;
  text("#cellDelta", delta == null ? "等待数据" : fmt(delta, 0));
  text("#cellExtremes", delta == null ? "最高 / 最低 —" : `${o.max_cell_mv} / ${o.min_cell_mv} mV`);
  const sumDelta = voltageFresh && cellSumFresh && o.cell_sum_v != null ? o.voltage_v - o.cell_sum_v : null;
  text("#cellSumDelta", sumDelta == null ? "单体累加 —" : `单体累加 ${fmt(o.cell_sum_v, 1)} V · 差 ${fmt(sumDelta, 1)} V`);
  const relayFresh = relay.command_age != null && relay.command_age <= 1.5;
  const hvFresh = hv.age != null && hv.age <= 1.5;
  text("#prechargeVoltage", relayFresh ? fmt(relay.precharge_voltage_v, 1) : "等待数据");
  const positive = hvFresh && hv.positive != null ? hv.positive : relayFresh ? relay.positive : null;
  const negative = hvFresh && hv.negative != null ? hv.negative : relayFresh ? relay.negative : null;
  setClass("#positiveRelay", "closed", positive); setClass("#negativeRelay", "closed", negative);
  text("#positiveRelay em", positive == null ? "—" : positive ? "闭合" : "断开");
  text("#negativeRelay em", negative == null ? "—" : negative ? "闭合" : "断开");
  const precharge = hvFresh && hv.precharge != null ? hv.precharge : relayFresh ? relay.precharge : null;
  setClass("#prechargeRelay", "closed", precharge);
  text("#prechargeRelay em", precharge == null ? "—" : precharge ? "闭合" : "断开");
  const hvOutputName = !overviewFresh ? "等待数据" : o.state === 4 ? "预充中" : o.state === 5 || (positive && negative) ? "已接通"
    : o.state === 7 ? "故障保持" : hvFresh ? "已断开" : "等待数据";
  text("#hvOutput", hvOutputName);
  setClass("#hvOutput", "ok", hvOutputName === "已接通");
  setClass("#hvOutput", "bad", hvOutputName === "故障保持");
  text("#hvAcc", !hvFresh ? "等待数据" : hv.hv_acc ? "请求" : "已释放");
  text("#chargeButton", !hvFresh ? "等待数据" : hv.charge_button ? "已按下" : "已释放");
  const safetyAlarm = (state.snapshot.alarms || []).find(item => item.index === 22);
  const safetyEventKnown = faultState.completeFresh;
  const safetyEventActive = safetyEventKnown && !!(safetyAlarm?.level || safetyAlarm?.in_fault_code);
  text("#safetyEvent", !safetyEventKnown ? "等待数据" : safetyEventActive ? "已触发" : "未触发");
  setClass("#safetyEvent", "bad", safetyEventKnown && safetyEventActive);
  setClass("#safetyEvent", "ok", safetyEventKnown && !safetyEventActive);
  const bmsFaultOutput = state.snapshot.fault?.flags?.bms_output_latched;
  text("#bmsFaultSignal", !faultState.faultFresh ? "等待数据" : bmsFaultOutput ? "已输出" : "未输出");
  setClass("#bmsFaultSignal", "bad", faultState.faultFresh && bmsFaultOutput);
  setClass("#bmsFaultSignal", "ok", faultState.faultFresh && !bmsFaultOutput);
  const prechargeResultName = !hvFresh ? "等待数据" : hv.precharge_result_name || "未发生";
  const prechargeTime = !hvFresh ? "等待数据" : hv.precharge_result === 2 ? `${hv.failure_ms} ms` : hv.success_ms ? `${hv.success_ms} ms` : "—";
  text("#lastPrechargeResult", prechargeResultName);
  text("#prechargeTime", prechargeTime);
  text("#prechargeResult", !hvFresh ? "等待数据" : hv.precharge_result_name || "未发生预充");
  $("#prechargeResult").className = `state-note ${hvFresh && hv.precharge_result === 1 ? "ok" : hvFresh && hv.precharge_result === 2 ? "bad" : ""}`;
  renderThermal(o, relay, overviewFresh);
  renderImd(imd);
  drawTrend();
}

function renderThermal(overview, relay, overviewFresh) {
  const thermalFresh = relay.thermal_age != null && relay.thermal_age <= 1.5;
  const temperatureFresh = thermalFresh && overviewFresh && overview.temperature_complete;
  text("#maxTemp", !temperatureFresh || overview.max_temp_c == null ? "等待数据" : `${overview.max_temp_c} °C`);
  text("#minTemp", !temperatureFresh || overview.min_temp_c == null ? "等待数据" : `${overview.min_temp_c} °C`);
  text("#maxTempNo", !temperatureFresh || overview.max_temp_no == null ? "" : `T${overview.max_temp_no}`);
  text("#minTempNo", !temperatureFresh || overview.min_temp_no == null ? "" : `T${overview.min_temp_no}`);
  const spread = temperatureFresh && overview.max_temp_c != null && overview.min_temp_c != null
    ? overview.max_temp_c - overview.min_temp_c : null;
  text("#tempDelta", spread == null ? "等待数据" : `${spread} °C`);
  text("#fanDuty", !thermalFresh || relay.fan_duty_pct == null ? "等待数据" : `${relay.fan_duty_pct} %`);
  text("#fanRpm", !thermalFresh || relay.fan_rpm == null ? "—" : `${relay.fan_rpm} rpm`);
  text("#coolingTag", !thermalFresh ? "等待数据" : relay.cooling == null ? "目标 —" : relay.cooling ? "目标：请求" : "目标：关闭");
  $("#coolingTag").className = `state-text ${thermalFresh && relay.cooling ? "ok" : ""}`;
  const fanFlags = relay.fan_flags;
  let fanStatus = "等待数据";
  if (thermalFresh && fanFlags != null) {
    const commandOn = (fanFlags & 0x02) !== 0;
    const tachMoving = (fanFlags & 0x04) !== 0;
    const stalled = (fanFlags & 0x10) !== 0;
    fanStatus = stalled ? "停转故障" : commandOn && tachMoving ? "运行" : commandOn ? "等待转速" : "未请求";
  }
  text("#fanStatus", fanStatus);
  $("#fanStatus").className = !thermalFresh ? "" : fanStatus === "停转故障" ? "bad" : fanStatus === "运行" || fanStatus === "未请求" ? "ok" : "";
}

function renderImd(imd) {
  const fresh = imd.age != null && imd.age <= 1.5;
  const digital = !fresh ? "等待数据" : imd.digital_ok ? "正常" : "故障";
  text("#imdDigital", digital);
  $("#imdDigital").className = !fresh ? "" : imd.digital_ok ? "ok" : "bad";

  const frequency = !fresh || !imd.pwm_signal_ok || imd.frequency_hz == null ? "—" : `${fmt(imd.frequency_hz, 2)} Hz`;
  const frequencyNote = !fresh ? "" : IMD_FREQUENCY_NOTES[imd.frequency_class] || "PWM";
  text("#imdFrequency", frequency);
  text("#imdFrequencyNote", frequencyNote);
  $("#imdFrequency").className = !fresh || !imd.pwm_signal_ok ? "" : "ok";
  $("#imdFrequency").title = fresh ? `IMD 实际 PWM 频率 · ${frequencyNote}` : "等待 IMD 数据。";

  const resistanceMode = imd.frequency_class === 1 || imd.frequency_class === 2;
  const resistance = !fresh || !resistanceMode || !imd.insulation_valid ? "—"
    : imd.resistance_saturated ? "≥65535" : fmt(imd.resistance_kohm, 0);
  text("#imdResistance", resistance);
  text("#imdDuty", !fresh || !imd.pwm_signal_ok || imd.duty_pct == null ? "—" : `${fmt(imd.duty_pct, 1)} %`);
}

function renderModules() {
  if (state.snapshot.raw_cell_data_available !== true) {
    $("#moduleAverages").innerHTML = "";
    text("#moduleSummary", "CANB 无单体原始数据");
    $("#moduleSummary").className = "state-text";
    return;
  }
  const modules = state.snapshot.modules || [];
  const cells = state.snapshot.cells || [], temps = state.snapshot.temps || [];
  $("#moduleAverages").innerHTML = modules.map(item => {
    const cellValues = cells.filter(cell => cell.module === item.no && cell.value != null).map(cell => cell.value);
    const tempValues = temps.filter(temp => temp.module === item.no && temp.value != null).map(temp => temp.value);
    const avgCell = cellValues.length ? cellValues.reduce((sum, value) => sum + value, 0) / cellValues.length : null;
    const avgTemp = tempValues.length ? tempValues.reduce((sum, value) => sum + value, 0) / tempValues.length : null;
    const delta = cellValues.length ? Math.max(...cellValues) - Math.min(...cellValues) : null;
    return `<div class="${item.online ? "online" : "offline"}"><b>M${item.no}</b><span>${fmt(avgCell, 0)} mV</span>`
      + `<span>${fmt(avgTemp, 1)} °C</span><span>${fmt(delta, 0)} mV</span><em>${item.online ? "正常" : "缺失"}</em></div>`;
  }).join("");
  const online = modules.filter(item => item.online).length;
  text("#moduleSummary", `${online}/6 正常`);
  $("#moduleSummary").className = `state-text ${online === 6 ? "ok" : "bad"}`;
}

function durationLabel(totalSeconds) {
  const seconds = Math.max(0, Math.round(totalSeconds || 0));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  const remainder = seconds % 60;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}

function renderChargeTiming(overview, connection, fault) {
  const connectionKey = [connection.connected, connection.mode, connection.channel,
    connection.bus_profile, connection.bitrate].join("|");
  if (state.chargeTiming.connectionKey !== connectionKey) {
    resetChargeTiming();
    state.chargeTiming.connectionKey = connectionKey;
  }
  const timing = state.chargeTiming;
  if (connection.mode === "replay") {
    text("#chargeTimingState", "历史回放");
    text("#chargeElapsed", "—"); text("#chargeRemaining", "—");
    return;
  }

  const now = Date.now();
  const faultFresh = fault.received === true && fault.age != null && fault.age <= 1.5;
  const summaryFresh = connection.summary_age != null && connection.summary_age <= 1.5;
  if (!faultFresh || !summaryFresh) {
    timing.lastTickMs = null;
    text("#chargeTimingState", "等待数据");
    text("#chargeElapsed", "—"); text("#chargeRemaining", "—");
    return;
  }

  const chargeActive = !!fault.flags?.charge_mode;
  const currentFresh = overview.current_valid === true;
  const socFresh = overview.soc_valid === true;
  const chargeCurrent = Number(overview.current_a);

  if (chargeActive && !timing.active) {
    timing.active = true;
    timing.elapsedMs = 0;
    timing.lastTickMs = now;
    timing.averageCurrentA = null;
    timing.currentSumA = 0;
    timing.currentSamples = 0;
  } else if (!chargeActive && timing.active) {
    timing.active = false;
    timing.lastTickMs = null;
  }

  if (!currentFresh) {
    timing.lastTickMs = null;
    text("#chargeTimingState", chargeActive ? "等待数据" : timing.elapsedMs ? "本次已停止" : "未充电");
    text("#chargeElapsed", durationLabel(timing.elapsedMs / 1000));
    text("#chargeRemaining", "—");
    return;
  }

  if (timing.active) {
    if (timing.lastTickMs != null) timing.elapsedMs += Math.min(1500, now - timing.lastTickMs);
    timing.lastTickMs = now;
    if (Number.isFinite(chargeCurrent) && chargeCurrent > 0.05) {
      timing.currentSumA += chargeCurrent;
      timing.currentSamples++;
      timing.averageCurrentA = timing.currentSumA / timing.currentSamples;
    }
  }

  text("#chargeTimingState", chargeActive ? "充电计时中" : timing.elapsedMs ? "本次已停止" : "未充电");
  text("#chargeElapsed", durationLabel(timing.elapsedMs / 1000));

  const soc = Number(overview.soc_pct);
  const estimateCurrent = timing.averageCurrentA;
  if (!chargeActive || !socFresh || !Number.isFinite(soc) || estimateCurrent == null || estimateCurrent <= .1) {
    text("#chargeRemaining", "—");
    return;
  }

  const remainingAh = PACK_CAPACITY_AH * Math.max(0, 100 - soc) / 100;
  const remainingSeconds = Math.min(7 * 86400, remainingAh / estimateCurrent * 3600);
  if (remainingAh <= .01) {
    text("#chargeRemaining", "已充满");
  } else {
    text("#chargeRemaining", durationLabel(remainingSeconds));
  }
}

const TREND_COLORS = { voltage: "#aeb6b2", current: "#f0b429", precharge: "#858d89" };

function trendAxisRange(values, minimumSpan) {
  if (!values.length) return null;
  let min = Math.min(...values), max = Math.max(...values);
  if (max - min < minimumSpan) {
    const mid = (max + min) / 2;
    min = mid - minimumSpan / 2;
    max = mid + minimumSpan / 2;
  }
  return { min, max };
}

function drawTrend() {
  const canvas = $("#trendCanvas"), trends = state.snapshot?.trends || [];
  if (!canvas || !canvas.clientWidth || !canvas.clientHeight) return;
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth, height = canvas.clientHeight;
  const targetWidth = Math.round(width * ratio), targetHeight = Math.round(height * ratio);
  if (canvas.width !== targetWidth || canvas.height !== targetHeight) {
    canvas.width = targetWidth; canvas.height = targetHeight;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const latest = trends[trends.length - 1] || {};
  const overview = state.snapshot?.overview || {};
  const connection = state.snapshot?.connection || {};
  const relay = state.snapshot?.relay || {};
  const summaryFresh = isFresh(connection.summary_age);
  const relayFresh = isFresh(relay.command_age);
  const latestVoltage = summaryFresh && overview.voltage_valid ? latest.voltage : null;
  const latestCurrent = summaryFresh && overview.current_valid ? latest.current : null;
  const latestPrecharge = relayFresh ? latest.precharge : null;
  text("#trendVoltage", Number.isFinite(latestVoltage) ? `${fmt(latestVoltage, 1)} V` : "等待数据");
  text("#trendCurrent", Number.isFinite(latestCurrent) ? `${fmt(latestCurrent, 1)} A` : "等待数据");
  text("#trendPrecharge", Number.isFinite(latestPrecharge) ? `${fmt(latestPrecharge, 1)} V` : "等待数据");

  const pad = { left: 48, right: 48, top: 12, bottom: 24 };
  const plotWidth = width - pad.left - pad.right, plotHeight = height - pad.top - pad.bottom;
  const axisFont = '10px "SF Mono", "Cascadia Mono", Consolas, monospace';
  const labelColor = "#7c7f7d", gridColor = "#2d2f31";

  // Horizontal grid, five bands, plus the left/right axis tick labels.
  const left = trendAxisRange(trends.flatMap(item => [item.voltage, item.precharge]).filter(Number.isFinite), 8);
  const right = trendAxisRange(trends.map(item => item.current).filter(Number.isFinite), 6);
  ctx.font = axisFont;
  for (let row = 0; row <= 4; row++) {
    const y = Math.round(pad.top + plotHeight * row / 4) + .5;
    ctx.strokeStyle = gridColor; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
    if (trends.length >= 2) {
      ctx.fillStyle = labelColor;
      if (left && Number.isFinite(left.min)) {
        ctx.textAlign = "right";
        ctx.fillText(`${(left.max - (left.max - left.min) * row / 4).toFixed(1)}`, pad.left - 7, y + 3);
      }
      if (right && Number.isFinite(right.min)) {
        ctx.textAlign = "left";
        ctx.fillText(`${(right.max - (right.max - right.min) * row / 4).toFixed(1)}`, width - pad.right + 7, y + 3);
      }
    }
  }
  ctx.fillStyle = labelColor; ctx.textAlign = "left"; ctx.font = axisFont;
  ctx.fillText("V", 2, pad.top + 3);
  ctx.textAlign = "right"; ctx.fillText("A", width - 2, pad.top + 3);

  if (trends.length < 2) {
    ctx.fillStyle = "#87969c";
    ctx.font = '12px "PingFang SC", "Microsoft YaHei UI", sans-serif';
    ctx.textAlign = "center";
    ctx.fillText("等待状态帧形成曲线", pad.left + plotWidth / 2, pad.top + plotHeight / 2);
    return;
  }

  // Time axis: five evenly spaced labels over the recorded window.
  const t0 = trends[0].t, t1 = trends[trends.length - 1].t;
  const span = Math.max(1, t1 - t0);
  const timeLabel = seconds => {
    const total = Math.max(0, Math.round(seconds));
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
  };
  ctx.font = axisFont; ctx.fillStyle = labelColor; ctx.textAlign = "center";
  for (let col = 0; col <= 4; col++) {
    const x = pad.left + plotWidth * col / 4;
    ctx.strokeStyle = gridColor; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(Math.round(x) + .5, pad.top); ctx.lineTo(Math.round(x) + .5, pad.top + plotHeight); ctx.stroke();
    ctx.fillText(col === 4 ? "现在" : timeLabel(t0 + span * col / 4), x, height - 7);
  }

  const xOf = item => pad.left + plotWidth * (item.t - t0) / span;
  const yLeft = left ? value => pad.top + plotHeight * (left.max - value) / (left.max - left.min) : null;
  const yRight = right ? value => pad.top + plotHeight * (right.max - value) / (right.max - right.min) : null;

  // Dashed zero line on the current axis when the window crosses zero.
  if (right && Number.isFinite(right.min) && right.min < 0 && right.max > 0) {
    const y = Math.round(yRight(0)) + .5;
    ctx.strokeStyle = "#55585a"; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
    ctx.setLineDash([]);
  }

  const drawSeries = (key, color, yOf, fill) => {
    const points = [];
    trends.forEach(item => {
      if (!Number.isFinite(item[key])) return;
      points.push({ x: xOf(item), y: yOf(item[key]) });
    });
    if (points.length < 2) return;
    ctx.beginPath();
    points.forEach((point, index) => index ? ctx.lineTo(point.x, point.y) : ctx.moveTo(point.x, point.y));
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.stroke();
    if (fill) {
      const gradient = ctx.createLinearGradient(0, pad.top, 0, pad.top + plotHeight);
      gradient.addColorStop(0, fill); gradient.addColorStop(1, "rgba(0,0,0,0)");
      ctx.lineTo(points[points.length - 1].x, pad.top + plotHeight);
      ctx.lineTo(points[0].x, pad.top + plotHeight);
      ctx.closePath();
      ctx.fillStyle = gradient; ctx.fill();
    }
    const last = points[points.length - 1];
    ctx.beginPath(); ctx.arc(last.x, last.y, 2.5, 0, Math.PI * 2);
    ctx.fillStyle = color; ctx.fill();
  };

  if (left) {
    drawSeries("precharge", TREND_COLORS.precharge, yLeft, "rgba(133, 141, 137, .10)");
    drawSeries("voltage", TREND_COLORS.voltage, yLeft, "rgba(174, 182, 178, .13)");
  }
  if (right) drawSeries("current", TREND_COLORS.current, yRight, null);
}

function renderAlarmSummary() {
  const alarms = state.snapshot.alarms || [];
  const faultState = liveFaultState(state.snapshot);
  text("#faultCode", faultState.faultFresh ? faultState.fault.code_hex : "等待数据");
  const activeItems = faultState.completeFresh
    ? alarms.filter(item => item.level || item.in_fault_code) : [];
  const activeCount = activeItems.length;
  text("#alarmNavBadge", activeCount); $("#alarmNavBadge").classList.toggle("hidden", activeCount === 0);
  const alarmCountText = activeCount ? `${activeCount} 项`
    : faultState.completeFresh ? "无"
    : faultState.faultFresh ? "等待等级"
    : faultState.alarmLevelsFresh ? "等待故障码" : "等待数据";
  text("#alarmActiveCount", alarmCountText);
  text("#alarmPageLevel", faultState.faultFresh ? state.snapshot.overview?.alarm_level_name || "未知" : "等待数据");
  $("#alarmPageLevel")?.classList.toggle("bad", faultState.faultFresh && state.snapshot.overview?.alarm_level === 1);
  $("#alarmPageLevel")?.classList.toggle("warn", faultState.faultFresh && state.snapshot.overview?.alarm_level === 2);
  text("#activeAlarmCount", alarmCountText);
  const active = activeItems.slice(0, 3);
  $("#activeAlarmBrief").className = active.length ? "fault-brief" : "fault-brief empty-state";
  $("#activeAlarmBrief").innerHTML = active.length ? active.map(item => `<div class="brief-alarm ${item.level === 1 ? "lv1" : ""}"><b>${item.name}</b><span>${item.level_name}</span></div>`).join("")
    : faultState.completeFresh ? "当前没有活动告警" : "故障码与告警等级尚未同时更新";
}

function alarmRuleText(index, thresholds) {
  if (index === 0) return thresholds.ov_mv == null ? "阈值未回报 · 确认270 ms" : `≥${thresholds.ov_mv} mV · 确认270 ms`;
  if (index === 1) return thresholds.uv_mv == null ? "阈值未回报 · 确认270 ms" : `≤${thresholds.uv_mv} mV · 确认270 ms`;
  if (index === 2) {
    if (thresholds.ot_c == null) return "阈值未回报 · 确认780 ms";
    return `≥${thresholds.ot_c} °C · 充电时${Math.min(thresholds.ot_c, 50)} °C · 780 ms`;
  }
  if (index === 3) {
    if (thresholds.ut_c == null) return "阈值未回报 · 确认780 ms";
    return `≤${thresholds.ut_c} °C · 充电时${Math.max(thresholds.ut_c, 5)} °C · 780 ms`;
  }
  return ALARM_RULE_TEXTS[index] || "按主控当前程序判定";
}

function renderAlarms() {
  const alarms = state.snapshot.alarms || [];
  const faultState = liveFaultState(state.snapshot);
  const config = state.snapshot.config || {};
  const thresholds = isFresh(config.thresholds_age, SLOW_DATA_FRESH_MAX_S) ? (config.thresholds || {}) : {};
  alarms.forEach(alarm => {
    const node = $(`[data-alarm="${alarm.index}"]`); if (!node) return;
    const levelReceived = faultState.alarmLevelsFresh;
    const faultCodeActive = faultState.completeFresh && alarm.in_fault_code;
    const active = !!(faultState.completeFresh && (alarm.level || alarm.in_fault_code));
    const classes = ["alarm-item"];
    if (levelReceived && alarm.level === 1) classes.push("lv1");
    else if (levelReceived && alarm.level === 2) classes.push("lv2");
    if (faultCodeActive) classes.push("fc");
    if (!levelReceived && !faultCodeActive) classes.push("pending");
    if (alarm.index === 14 || alarm.index === 15) classes.push("reserved");
    if (state.onlyActiveAlarms && !active) classes.push("hidden");
    node.className = classes.join(" ");
    node.querySelector("b").textContent = alarm.name;
    node.querySelector("small").textContent = alarmRuleText(alarm.index, thresholds);
    node.querySelector("em").textContent = alarm.index === 22 && ((levelReceived && alarm.level) || faultCodeActive) ? "事件触发"
      : levelReceived && alarm.level === 1 ? "一级故障"
      : levelReceived && alarm.level === 2 ? "二级告警"
      : faultCodeActive ? "故障码置位"
      : !levelReceived ? "等待等级"
      : alarm.index === 14 || alarm.index === 15 ? "未启用" : "正常";
  });
  const history = state.snapshot.fault_history || [];
  $("#faultHistory").innerHTML = history.length ? history.map(event => `<div class="event-item"><time>${event.time}</time><b>${event.previous} → ${event.code}</b><p>${event.added.length ? `<span class="added">进入：${event.added.join("、")}</span>` : ""}${event.added.length && event.cleared.length ? "<br>" : ""}${event.cleared.length ? `<span class="cleared">清除：${event.cleared.join("、")}</span>` : ""}</p></div>`).join("") : `<div class="empty-state">故障码发生变化后在这里显示进入和清除记录。</div>`;
  const flashRecords = state.snapshot.flash_log_records || [];
  const logClearPending = !!state.snapshot.fault?.flags?.log_clear_pending;
  $("#readFlashLog").disabled = logClearPending;
  $("#clearFaultLog").disabled = logClearPending;
  $("#flashFaultLog").innerHTML = flashRecords.length ? [...flashRecords].reverse().map(event =>
    `<div class="event-item"><time>${event.timestamp}</time><b>${event.fault_code}</b><p>类型 ${event.event_type} · 详情 ${event.event_detail}</p></div>`
  ).join("") : `<div class="empty-state">尚未读取，或 Flash 中没有重要故障日志。</div>`;
  const logInfo = state.snapshot.flash_log_info || {};
  text("#flashLogInfo", logClearPending ? "正在分阶段清除"
    : logInfo.count == null ? "未读取"
    : `主控记录 ${logInfo.count} 条 · 丢弃 ${logInfo.dropped} 条 · 已显示 ${flashRecords.length} 条`);
}

function renderConfig() {
  const config = state.snapshot.config || {};
  const rawThresholds = config.thresholds || {};
  const thresholdsFresh = isFresh(config.thresholds_age, SLOW_DATA_FRESH_MAX_S);
  const thresholds = thresholdsFresh ? rawThresholds : {};
  const hasThresholdReport = thresholdsFresh && Object.keys(thresholds).length > 0;
  const rawSwitches = config.switches || {};
  const switchesFresh = isFresh(config.switches_age, SLOW_DATA_FRESH_MAX_S);
  const switches = switchesFresh ? rawSwitches : {};
  const hasSwitchReport = switchesFresh && Object.keys(switches).length > 0;
  const runtimeFresh = isFresh(config.runtime_age);
  const directionKnown = runtimeFresh && config.current_direction_inverted != null;
  const chargerTypeKnown = runtimeFresh && config.charger_type != null;

  text("#displayOv", hasThresholdReport ? `${thresholds.ov_mv} mV` : "等待数据");
  text("#displayUv", hasThresholdReport ? `${thresholds.uv_mv} mV` : "等待数据");
  text("#displayOt", hasThresholdReport ? `${thresholds.ot_c} °C` : "等待数据");
  text("#displayUt", hasThresholdReport ? `${thresholds.ut_c} °C` : "等待数据");
  const thresholdLabel = state.dirty.thresholds ? "页面值待写入" : hasThresholdReport ? "已从主控同步" : "等待主控回报";
  text("#thresholdSync", thresholdLabel);
  $("#thresholdSync").className = `tag ${state.dirty.thresholds ? "warn" : hasThresholdReport ? "ok" : "neutral"}`;
  if (!hasThresholdReport && !state.dirty.thresholds) {
    ["#ovInput", "#uvInput", "#otInput", "#utInput"].forEach(id => $(id).value = "");
    state.inputsInitialized.thresholds = false;
  } else if (hasThresholdReport && !state.dirty.thresholds
      && (!state.inputsInitialized.thresholds || ![$("#ovInput"), $("#uvInput"), $("#otInput"), $("#utInput")].includes(document.activeElement))) {
    $("#ovInput").value = thresholds.ov_mv; $("#uvInput").value = thresholds.uv_mv;
    $("#otInput").value = thresholds.ot_c; $("#utInput").value = thresholds.ut_c;
    state.inputsInitialized.thresholds = true;
  }
  $("#sendThresholds").disabled = !hasThresholdReport && !state.dirty.thresholds;

  text("#switchVersion", hasSwitchReport && config.switch_version != null
    ? `回报 V${config.switch_version}` : "等待数据");
  const statusList = $("#switchStatusList");
  if (statusList && state.bootstrap?.switch_catalog) {
    const enabledCount = state.bootstrap.switch_catalog.filter(item => switches[item.key]).length;
    text("#switchStatusSummary", hasSwitchReport ? `启用 ${enabledCount} / ${state.bootstrap.switch_catalog.length}` : "等待回报");
    statusList.innerHTML = state.bootstrap.switch_catalog.map(item => {
      const enabled = hasSwitchReport && !!switches[item.key];
      const stateClass = !hasSwitchReport ? "pending" : enabled ? "enabled" : "disabled";
      const stateText = !hasSwitchReport ? "等待" : enabled ? "启用" : "关闭";
      return `<span class="protection-switch ${stateClass}" title="${item.name} · ${item.code} · ${item.variable}">`
        + `<i></i><span class="protection-label"><b>${item.name}</b><small>${item.code}</small></span><b>${stateText}</b></span>`;
    }).join("");
  }
  if (!hasSwitchReport && !state.dirty.switches) {
    $$("#switchList input").forEach(input => input.checked = false);
    state.inputsInitialized.switches = false;
  } else if (hasSwitchReport && !state.dirty.switches
      && (!state.inputsInitialized.switches || !$("#switchList").contains(document.activeElement))) {
    $$("#switchList input").forEach(input => input.checked = !!switches[input.dataset.key]);
    state.inputsInitialized.switches = true;
  }
  $$("#switchList input").forEach(input => input.disabled = !hasSwitchReport && !state.dirty.switches);
  $("#sendSwitches").disabled = !hasSwitchReport && !state.dirty.switches;
  updateSwitchRowState();
  if (directionKnown && !state.dirty.direction && document.activeElement !== $("#currentDirection")) {
    $("#currentDirection").value = config.current_direction_inverted ? "1" : "0";
  } else if (!directionKnown && !state.dirty.direction) {
    $("#currentDirection").value = "";
  }
  if (chargerTypeKnown && !state.dirty.chargerType && document.activeElement !== $("#chargerType")) {
    $("#chargerType").value = String(config.charger_type);
  } else if (!chargerTypeKnown && !state.dirty.chargerType) {
    $("#chargerType").value = "";
  }
  $("#currentDirection").disabled = !directionKnown && !state.dirty.direction;
  $("#sendCurrentDirection").disabled = !directionKnown && !state.dirty.direction;
  $("#chargerType").disabled = !chargerTypeKnown && !state.dirty.chargerType;
  $("#sendChargerType").disabled = !chargerTypeKnown && !state.dirty.chargerType;
}

function watchFlashSave(command, ack) {
  const kind = SAVE_KINDS[command];
  if (!kind) return;
  const flags = ack?.flags || {};
  const pendingKey = kind === "direction" ? "current_direction_save_pending" : "config_save_pending";
  const pending = !!flags[pendingKey];
  state.saveWatch = {
    command, kind, startedAt: Date.now(), ackPending: pending,
    pendingSeen: false, noWrite: ack?.result === 0 && !pending, confirmedAt: null,
  };
}

function renderSaveStatus(runtime) {
  const node = $("#saveStatus");
  if (!node) return;
  const fresh = runtime.age != null && runtime.age <= 1.5;
  const configPending = !!runtime.config_save_pending;
  const directionPending = !!runtime.current_direction_save_pending;
  const pending = configPending || directionPending;
  const watch = state.saveWatch;
  const now = Date.now();
  const watchPending = watch && (watch.kind === "direction" ? directionPending : configPending);
  if (watchPending) watch.pendingSeen = true;
  let label = "等待保存状态";
  let className = "neutral";
  let title = "等待主控确认 Flash 状态。";

  if (!fresh) {
    label = "等待保存状态";
  } else if (!runtime.flash_ready) {
    label = "Flash 离线";
    className = "bad";
    title = "外置 Flash 当前不可用。";
  } else if (watch?.noWrite) {
    label = `${SAVE_LABELS[watch.command]}已接受 · 无待保存`;
    className = "ok";
    title = "当前值无需再次写入，暂无新的保存任务。";
    if (!watch.confirmedAt) watch.confirmedAt = now;
  } else if (watch && watchPending) {
    const overdue = now - watch.startedAt > 5000;
    label = overdue ? "Flash 保存未完成" : "等待 Flash 保存";
    className = overdue ? "bad" : "warn";
    title = overdue ? "写入尚未完成，请检查外置 Flash 或主控状态。"
      : `“${SAVE_LABELS[watch.command]}”写入已接受，正在保存到外置 Flash。`;
  } else if (watch && !watch.noWrite && watch.ackPending && !watch.pendingSeen && now - watch.startedAt < 1000) {
    label = "等待 Flash 保存";
    className = "warn";
    title = `“${SAVE_LABELS[watch.command]}”写入已接受，正在等待保存结果。`;
  } else if (watch && !watch.confirmedAt) {
    watch.confirmedAt = now;
    label = "已保存到 Flash";
    className = "ok";
    title = "主控已确认写入完成。";
    toast(`${SAVE_LABELS[watch.command]}已确认保存到外置 Flash`);
  } else if (watch?.confirmedAt && now - watch.confirmedAt <= 6000) {
    label = "已保存到 Flash";
    className = "ok";
    title = "主控已确认写入完成。";
  } else if (pending) {
    label = "等待 Flash 保存";
    className = "warn";
    title = "有配置正在保存到外置 Flash。";
  } else {
    label = "Flash 就绪 · 无待保存";
    className = "ok";
    title = "外置 Flash 可用，当前没有待保存配置。";
  }

  if (watch?.confirmedAt && now - watch.confirmedAt > 6000) state.saveWatch = null;
  text("#saveStatus", label);
  node.className = `tag ${className}`;
  node.title = title;
}

function renderControls() {
  const { relay, fault, connection, overview } = state.snapshot;
  const requestFresh = isFresh(relay.command_age);
  if (requestFresh && relay.request_voltage_v != null && !state.dirty.charge
      && (!state.inputsInitialized.charge || ![$("#chargeVoltage"), $("#chargeCurrent")].includes(document.activeElement))) {
    $("#chargeVoltage").value = relay.request_voltage_v; $("#chargeCurrent").value = relay.request_current_a; state.inputsInitialized.charge = true;
  }
  if (!requestFresh && !state.dirty.charge) {
    $("#chargeVoltage").value = ""; $("#chargeCurrent").value = "";
    state.inputsInitialized.charge = false;
  }
  const requestText = !requestFresh || relay.request_voltage_v == null || relay.request_current_a == null
    ? "等待数据"
    : `${fmt(relay.request_voltage_v, 1)} V / ${fmt(relay.request_current_a, 1)} A`;
  text("#chargeRequestEcho", requestText);
  const runtime = state.snapshot.runtime_diag || {};
  renderSaveStatus(runtime);
  const config = state.snapshot.config || {};
  if (isFresh(config.runtime_age) && config.charger_type != null
      && !state.dirty.chargerType && document.activeElement !== $("#chargerType")) {
    $("#chargerType").value = String(config.charger_type);
  }
  const runtimeFresh = runtime.age != null && runtime.age <= 1.5;
  if (runtimeFresh && runtime.charger_feedback_voltage_v != null && runtime.charger_feedback_current_a != null && runtime.charger_feedback_fresh) {
    text("#chargeFeedbackEcho", `${fmt(runtime.charger_feedback_voltage_v, 1)} V / ${fmt(runtime.charger_feedback_current_a, 1)} A`);
  } else if (runtimeFresh && (runtime.charger_feedback_voltage_v != null || runtime.charger_feedback_current_a != null)) {
    text("#chargeFeedbackEcho", "反馈超时");
  } else {
    text("#chargeFeedbackEcho", "等待数据");
  }
  const faultFresh = fault.received === true && fault.age != null && fault.age <= 1.5;
  const charge = faultFresh ? fault.flags?.charge_mode : null;
  const chargeLabel = charge == null ? "模式未知" : charge ? `充电 · ${fault.flags.charger_type}` : "放电 / 待机";
  text("#chargeModeTag", chargeLabel);
  $("#chargeModeTag").className = `tag ${charge == null ? "neutral" : charge ? "warn" : "neutral"}`;
  text("#heroChargeMode", chargeLabel);
  renderRtcReply(state.snapshot.rtc_reply || {});
  const connectedCan1 = connection.connected && connection.bus_profile === "can1";
  const fresh = connection.summary_age != null && connection.summary_age <= 1.5;
  const allowedState = fresh && [2, 3, 7].includes(overview.state);
  setClass("#lockConnected", "ok", connectedCan1); setClass("#lockState", "ok", allowedState); setClass("#lockFresh", "ok", fresh);
}

const RTC_STATUS_NAMES = {
  0: "设置成功", 1: "请求帧长度错误",
  3: "日期时间参数非法", 4: "RTC 未就绪", 5: "设置时间或日期失败",
  6: "工具协议版本错误", 7: "RTC 请求忙",
};

function renderRtcReply(reply) {
  if (reply.status == null || !isFresh(reply.age, RTC_REPLY_FRESH_MAX_S)) return text("#rtcReply", "等待新回复");
  const name = RTC_STATUS_NAMES[reply.status] || `未知 ${reply.status}`;
  if (!reply.year) return text("#rtcReply", `${name} · 时间读取失败`);
  const pad = value => String(value).padStart(2, "0");
  text("#rtcReply", `${name} · ${reply.year}-${pad(reply.month)}-${pad(reply.day)} ${pad(reply.hour)}:${pad(reply.minute)}:${pad(reply.second)}`);
}

function parseIvtNumber(id, label, maximum = 0xFFFF) {
  const raw = $(id)?.value?.trim() ?? "";
  if (!raw) {
    toast(label + "不能为空", true);
    return null;
  }
  const value = /^0x/i.test(raw) ? Number.parseInt(raw.slice(2), 16) : Number(raw);
  if (!Number.isInteger(value) || value < 0 || value > maximum) {
    toast(label + "格式或范围错误", true);
    return null;
  }
  return value;
}

function readIvtOptions() {
  const commandRaw = $("#ivtCommandId")?.value?.trim() || "";
  const responseRaw = $("#ivtResponseId")?.value?.trim() || "";
  const cmdId = commandRaw ? parseIvtNumber("#ivtCommandId", "Command ID", 0x7FF) : undefined;
  const rspId = responseRaw ? parseIvtNumber("#ivtResponseId", "Response ID", 0x7FF) : undefined;
  const positive = parseIvtNumber("#ivtPositiveThreshold", "正向过流阈值");
  const positiveReset = parseIvtNumber("#ivtPositiveResetThreshold", "正向复位阈值");
  const negative = parseIvtNumber("#ivtNegativeThreshold", "负向过流阈值");
  const negativeReset = parseIvtNumber("#ivtNegativeResetThreshold", "负向复位阈值");
  if ([positive, positiveReset, negative, negativeReset].some(value => value == null)) return null;
  return {
    ...(cmdId == null ? {} : { cmd_id: cmdId }),
    ...(rspId == null ? {} : { rsp_id: rspId }),
    startup: $("#ivtStartupMode").value,
    positive_threshold_a: positive,
    positive_reset_threshold_a: positiveReset,
    negative_threshold_a: negative,
    negative_reset_threshold_a: negativeReset,
  };
}

async function readIvtConfig() {
  if (!state.api) return;
  const options = readIvtOptions();
  if (!options) return;
  $("#readIvtConfig").disabled = true;
  const result = await state.api.read_ivt_config(options);
  $("#readIvtConfig").disabled = false;
  if (!result.ok) return toast(result.error || "读取 IVT 配置失败", true);
  toast("IVT 读回完成：" + (result.readback?.comparison?.status_name || "已读取"));
  await poll();
}

function ivtConnectionAvailable() {
  const connection = state.toolSnapshots.ivt?.connection;
  return connection?.connected === true && connection.mode === "pcan"
    && ["canb", "canb_legacy"].includes(connection.bus_profile);
}

function confirmIvtAction(kind, options, title, message, destructive = false) {
  if (!ivtConnectionAvailable()) return toast("请先连接真实 CANB PCAN", true);
  state.pendingCommand = null;
  state.pendingFanCommand = null;
  state.pendingIvtAction = { kind, options };
  const conn = state.toolSnapshots.ivt.connection;
  const cmdId = options.cmd_id ?? state.toolSnapshots.ivt.ivt_config?.command_id ?? 0x411;
  const rspId = options.rsp_id ?? state.toolSnapshots.ivt.ivt_config?.response_id ?? 0x511;
  text("#confirmTitle", title);
  text("#confirmMessage", message);
  const operation = kind === "configure" ? "完整配置并重启 IVT"
    : "切换到 " + (options.target_bitrate / 1000) + " kbit/s";
  text("#confirmPayload", "通道：" + (conn.channel || "PCAN") + "\n"
    + "总线：CANB · " + ((conn.bitrate || 500000) / 1000) + " kbit/s\n"
    + "Command：0x" + cmdId.toString(16).toUpperCase() + "\n"
    + "Response：0x" + rspId.toString(16).toUpperCase() + "\n"
    + "操作：" + operation);
  $("#confirmCheck").checked = false;
  $("#doConfirm").disabled = true;
  $("#doConfirm").className = destructive ? "danger-button" : "action-button";
  $("#confirmDialog").showModal();
}

function confirmIvtBitrate(targetBitrate) {
  const options = readIvtOptions();
  if (!options) return;
  options.target_bitrate = targetBitrate;
  confirmIvtAction("bitrate", options, "切换 IVT 到 " + (targetBitrate / 1000) + " kbit/s",
    "IVT 会停止并重启到目标位率。上位机会关闭当前 PCAN，再用目标位率重新打开并等待 Alive。执行前确认 CANB 上只有目标 IVT。", true);
}

function renderIvtConfig() {
  const snapshot = state.toolSnapshots.ivt || {};
  const connection = snapshot.connection || {};
  const available = ivtConnectionAvailable();
  const currentBitrate = Number(connection.bitrate || 0);
  text("#ivtCurrentBus", available
    ? "当前 CANB：" + (currentBitrate / 1000) + " kbit/s · " + (connection.channel || "PCAN")
    : "CANB：等待真实 PCAN 连接");
  if (currentBitrate && document.activeElement !== $("#ivtBitrateSelect")) {
    $("#ivtBitrateSelect").value = String(currentBitrate);
  }
  const readback = snapshot?.ivt_config;
  const comparison = readback?.comparison;
  $("#connectIvtButton").classList.toggle("hidden", available);
  $("#disconnectIvtButton").classList.toggle("hidden", !available);
  if ($("#readIvtConfig")) $("#readIvtConfig").disabled = !available;
  const configureButton = $("#configureIvt");
  if (configureButton) {
    const alreadyConfigured = comparison?.status === "configured";
    configureButton.disabled = !available || !readback || alreadyConfigured;
    configureButton.title = !available
      ? "请先连接真实 IVT-S"
      : !readback
        ? "请先读取并核对当前配置"
        : alreadyConfigured
          ? "当前配置已与 BMS CANB 对齐，无需重复配置"
          : "写入 BMS CANB 目标配置并重启 IVT";
  }
  if ($("#switchIvt250")) $("#switchIvt250").disabled = !available || currentBitrate === 250000;
  if ($("#switchIvt500")) $("#switchIvt500").disabled = !available || currentBitrate === 500000;

  const statusNode = $("#ivtConfigStatus");
  if (!statusNode) return;
  const statusClasses = { configured: "ok", unconfigured: "warn", mismatch: "bad" };
  statusNode.className = "tag " + (statusClasses[comparison?.status] || "neutral");
  statusNode.textContent = comparison?.status_name || "未读取";
  const readbackNode = $("#ivtReadback");
  if (!readback) {
    text("#ivtCheckSummary", available ? "点击读取，自动尝试出厂地址和 BMS 目标地址。" : "连接后读取设备信息、通道和 CAN ID。");
    readbackNode?.classList.add("hidden");
    return;
  }
  readbackNode?.classList.remove("hidden");
  const device = readback.device_id || {};
  const mode = readback.mode || {};
  const positive = readback.thresholds?.positive || {};
  const negative = readback.thresholds?.negative || {};
  const resultNames = ["I", "U1", "U2", "U3", "T", "W", "As", "Wh"];
  const resultIds = resultNames.map(name => readback.can_ids?.[name]).filter(value => value != null);
  const resultRange = resultIds.length === 8
    ? `0x${Math.min(...resultIds).toString(16).toUpperCase()}–0x${Math.max(...resultIds).toString(16).toUpperCase()}` : "—";
  const format = readback.channels?.[0]
    ? `${readback.channels[0].byte_order === "little" ? "小端" : "大端"} · ${readback.channels[0].mode_name || "—"}` : "—";
  const statusText = comparison?.status_name || "已读取";
  const summaryText = comparison?.status === "configured"
    ? "目标已对齐，可以断开配置通道并接入 F405。"
    : comparison?.status === "unconfigured"
      ? "读到出厂配置；首次接入 F405 前请执行一次 BMS CANB 配置。"
      : "发现配置差异；展开逐通道核对后决定是否重新配置。";
  text("#ivtCheckSummary", statusText + " · " + summaryText);
  text("#ivtConfigResult",
    statusText + " · 序列号 " + (readback.serial_number_hex || "—")
    + " · 当前 " + (mode.current_name || "—") + " / 上电 " + (mode.startup_name || "—")
    + " · 位率 " + (readback.bitrate == null ? "—" : readback.bitrate / 1000 + " kbit/s")
    + " · 结果 " + resultRange + " · " + format);
  $("#ivtIdentity").innerHTML = [
    ["设备", device.device_type_name || "—"],
    ["额定电流", device.nominal_current_a == null ? "—" : device.nominal_current_a + " A"],
    ["软件版本", readback.software_version?.payload_hex || "—"],
    ["物料号", readback.article_number?.payload_hex || "—"],
    ["当前地址", "0x" + (readback.command_id ?? readback.can_ids?.command ?? 0).toString(16).toUpperCase()
      + " / 0x" + (readback.response_id ?? readback.can_ids?.response ?? 0).toString(16).toUpperCase()],
    ["过流阈值", "+" + (positive.threshold_a ?? "—") + " / −" + (negative.threshold_a ?? "—") + " A"],
  ].map(([label, value]) => "<div><span>" + escapeHtml(label) + "</span><b>" + escapeHtml(value) + "</b></div>").join("");

  const detectedCommand = readback.command_id ?? readback.can_ids?.command;
  const detectedResponse = readback.response_id ?? readback.can_ids?.response;
  if (detectedCommand != null && document.activeElement !== $("#ivtCommandId")) $("#ivtCommandId").value = "0x" + Number(detectedCommand).toString(16).toUpperCase();
  if (detectedResponse != null && document.activeElement !== $("#ivtResponseId")) $("#ivtResponseId").value = "0x" + Number(detectedResponse).toString(16).toUpperCase();

  const expectedChannels = readback.expected?.channels || {};
  const diffFields = new Set((comparison?.differences || []).map(item => item.field));
  const channelRows = (readback.channels || []).map(channel => {
    const expected = expectedChannels[channel.name] || {};
    const id = readback.can_ids?.[channel.name];
    const mismatch = ["db1", "period_ms", "mode_name", "byte_order", "report_errors", "invert_sign"]
      .some(key => diffFields.has("channel." + channel.name + "." + key))
      || diffFields.has("can_id." + channel.name);
    return "<tr class=\"" + (mismatch ? "mismatch" : "") + "\">"
      + "<td>" + escapeHtml(channel.name) + "</td><td>" + (channel.period_ms ?? "—") + " / " + (expected.period_ms ?? "—") + " ms</td>"
      + "<td>" + escapeHtml(channel.mode_name || "—") + "</td><td>" + escapeHtml(channel.byte_order || "—") + "</td>"
      + "<td>" + (channel.invert_sign ? "反转" : "正常") + "</td><td>" + (channel.report_errors ? "启用" : "关闭") + "</td>"
      + "<td>0x" + (id ?? 0).toString(16).toUpperCase() + "</td></tr>";
  }).join("");
  $("#ivtConfigTable").innerHTML = "<table><thead><tr><th>通道</th><th>周期 / 目标</th><th>模式</th><th>字节序</th><th>符号</th><th>报错位</th><th>CAN ID</th></tr></thead><tbody>" + channelRows + "</tbody></table>";
  const differences = comparison?.differences || [];
  $("#ivtConfigDifferences").innerHTML = differences.length
    ? "<b>差异 " + differences.length + " 项</b><ul>" + differences.map(item => "<li>" + escapeHtml(item.field) + "：实际 " + escapeHtml(item.actual_text) + "，期望 " + escapeHtml(item.expected_text) + "</li>").join("") + "</ul>"
    : "<span class=\"ok-text\">所有目标字段与期望值一致。</span>";
}

function escapeHtml(value) {
  return String(value ?? "—").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  }[character]));
}

function fanConnectionAvailable() {
  const connection = state.toolSnapshots.fan?.connection;
  return connection?.connected === true && connection.mode === "pcan"
    && ["canb", "canb_legacy"].includes(connection.bus_profile);
}

function confirmFanCommand(name, values, title, message, destructive = false) {
  if (!fanConnectionAvailable()) return toast("请先连接真实 CANB PCAN", true);
  state.pendingCommand = null;
  state.pendingIvtAction = null;
  state.pendingFanCommand = { name, values };
  const conn = state.toolSnapshots.fan.connection;
  text("#confirmTitle", title);
  text("#confirmMessage", message);
  text("#confirmPayload", `通道：${conn.channel || "PCAN"}\n`
    + `总线：CANB · ${(conn.bitrate || 500000) / 1000} kbit/s\n`
    + `命令：${name}`);
  $("#confirmCheck").checked = false;
  $("#doConfirm").disabled = true;
  $("#doConfirm").className = destructive ? "danger-button" : "action-button";
  $("#confirmDialog").showModal();
}

function renderFanControlFields() {
  const mode = $("#fanModeSelect").value;
  const manual = mode === "1";
  $("#fanDuty1Input").disabled = !manual;
  $("#fanDuty2Input").disabled = !manual;
  $("#fanLeaseInput").disabled = mode === "0";
  text("#fanModeNote", mode === "0" ? "自动温控 · 根据电机与逆变器温度自动计算目标占空比"
    : mode === "1" ? "手动调速 · 设置固定占空比，到期自动恢复温控" : "强制停机 · 关闭两路输出，到期自动恢复温控");
}

function renderFan() {
  const snapshot = state.toolSnapshots.fan || {};
  const connection = snapshot.connection || {};
  const available = fanConnectionAvailable();
  const currentBitrate = Number(connection.bitrate || 0);
  if (currentBitrate && document.activeElement !== $("#fanBitrateSelect")) {
    $("#fanBitrateSelect").value = String(currentBitrate);
  }
  $("#connectFanButton").classList.toggle("hidden", available);
  $("#disconnectFanButton").classList.toggle("hidden", !available);
  ["#sendFanControl", "#sendFanCurve", "#sendFanFailsafe", "#fanQueryButton", "#fanRestoreButton"]
    .forEach(id => { const node = $(id); if (node) node.disabled = !available; });

  const fan = snapshot.fan || {};
  const status = fan.status || {};
  const diag = fan.diagnostic || {};
  const statusFresh = isFresh(fan.status_age);
  const diagFresh = isFresh(fan.diagnostic_age);
  const rpm = status.rpm || [];
  const duty = status.duty_pct || [];
  const target = diagFresh ? (diag.target_pct || []) : [];
  const faults = diagFresh ? (diag.faults || 0) : 0;

  // Render RPM and Tachometer card states
  const duty1 = duty[0] ?? 0;
  const duty2 = duty[1] ?? 0;
  const tachDefs = [
    { cardId: "#fanTachCard1", rpmId: "#fanRpm1", stateId: "#fanState1", rpm: rpm[0] ?? 0, duty: duty1, faultBit: 0 },
    { cardId: "#fanTachCard2", rpmId: "#fanRpm2", stateId: "#fanState2", rpm: rpm[1] ?? 0, duty: duty1, faultBit: 1 },
    { cardId: "#fanTachCard3", rpmId: "#fanRpm3", stateId: "#fanState3", rpm: rpm[2] ?? 0, duty: duty2, faultBit: 2 },
  ];

  tachDefs.forEach(item => {
    text(item.rpmId, statusFresh ? String(item.rpm) : "—");
    const card = $(item.cardId);
    const stateNode = $(item.stateId);
    if (!card || !stateNode) return;
    card.classList.remove("running", "stalled", "starting", "idle");
    stateNode.className = "fan-tach-state";
    if (!statusFresh) {
      stateNode.textContent = "—";
    } else if (faults & (1 << item.faultBit)) {
      stateNode.textContent = "停转故障";
      stateNode.classList.add("bad");
      card.classList.add("stalled");
    } else if (item.duty > 0 && item.rpm > 100) {
      stateNode.textContent = "运行中";
      stateNode.classList.add("ok");
      card.classList.add("running");
    } else if (item.duty > 0) {
      stateNode.textContent = "等待转速";
      stateNode.classList.add("warn");
      card.classList.add("starting");
    } else {
      stateNode.textContent = "已停机";
      stateNode.classList.add("muted");
      card.classList.add("idle");
    }
  });

  // Render PWM Duty progress bars and text
  for (let index = 0; index < 2; index++) {
    const bar = $(`#fanDuty${index + 1}Bar`);
    if (bar) bar.style.width = statusFresh ? `${Math.max(0, Math.min(100, duty[index] ?? 0))}%` : "0%";
    text(`#fanDuty${index + 1}Text`, !statusFresh ? "等待数据"
      : `${duty[index] ?? 0}%${target.length ? ` · 目标 ${target[index] ?? 0}%` : ""}`);
  }

  // Render Temperatures & Source Indicators
  text("#fanModeText", diagFresh ? diag.mode_name || "未知" : "等待数据");
  text("#fanMotorTemp", !diagFresh ? "等待数据"
    : diag.motor_temp_c == null ? "失联" : `${fmt(diag.motor_temp_c, 1)} °C`);
  text("#fanControllerTemp", !diagFresh ? "等待数据"
    : diag.controller_temp_c == null ? "失联" : `${fmt(diag.controller_temp_c, 1)} °C`);

  const tempChips = [
    ["#fanTempChip506", diag.motor_temp_valid],
    ["#fanTempChip507", diag.inverter_temp_valid],
    ["#fanTempChip508", diag.igbt_temp_valid],
  ];
  tempChips.forEach(([id, valid]) => {
    const node = $(id);
    if (node) node.className = `fan-source-chip${diagFresh && valid ? " on" : ""}`;
  });

  // Render Fault Badges
  $$("#page-fan [data-fan-fault]").forEach(chip => {
    const bit = +chip.dataset.fanFault;
    chip.classList.toggle("on", diagFresh && !!(faults & (1 << bit)));
  });

  const diagSummaryNode = $("#fanDiagStatus");
  if (diagSummaryNode) {
    diagSummaryNode.className = "fan-diag-summary " + (!diagFresh ? "" : faults !== 0 ? "bad" : "ok");
    diagSummaryNode.textContent = !diagFresh ? "等待数据"
      : faults === 0 ? "自检全部通过 (正常)" : `${diag.fault_names.length} 项故障活动`;
  }

  // Header Status Tag
  const statusNode = $("#fanToolStatus");
  const faultsActive = diagFresh && faults !== 0;
  const receiving = statusFresh || diagFresh;
  statusNode.className = "tag " + (!available || !receiving ? "neutral" : faultsActive ? "bad" : "ok");
  statusNode.textContent = !available ? "未连接"
    : !receiving ? "已连接 · 等待数据"
    : `${diag.mode_name || "风扇"} · ${faultsActive ? `${diag.fault_names.length} 项故障` : "正常"}`;
  const newestFanAge = Math.min(...[fan.status_age, fan.diagnostic_age].filter(age => age != null));
  text("#fanFreshTag", receiving ? `0x5A2/0x5A3 · ${fmt(newestFanAge, 1)} s 前` : "未收到状态帧");

  // Policies (Curves & Failsafe)
  const curve = fan.curve || {};
  const curveFresh = isFresh(fan.curve_age, SLOW_DATA_FRESH_MAX_S);
  const failsafe = fan.failsafe || {};
  const failsafeFresh = isFresh(fan.failsafe_age, SLOW_DATA_FRESH_MAX_S);
  text("#fanCurveReport", curveFresh
    ? `${curve.temp_off_c}/${curve.temp_on_c}/${curve.temp_full_c} ℃ · ${curve.min_duty_pct}% · ${curve.ramp_up_pct_per_s}%/s`
    : "当前值未读取");
  text("#fanFailsafeReport", failsafeFresh
    ? `${failsafe.failsafe_name} · 保底 ${failsafe.fallback1_duty_pct}/${failsafe.fallback2_duty_pct}% · 保持 ${failsafe.stale_hold_s}s`
    : "当前值未读取");

  // Autofill form if user hasn't edited
  const fill = (id, value) => { if (document.activeElement !== $(id)) $(id).value = value; };
  if (curveFresh && !state.dirty.fan) {
    fill("#fanTempOffInput", curve.temp_off_c);
    fill("#fanTempOnInput", curve.temp_on_c);
    fill("#fanTempFullInput", curve.temp_full_c);
    fill("#fanMinDutyInput", curve.min_duty_pct);
    fill("#fanRampUpInput", curve.ramp_up_pct_per_s);
  }
  if (failsafeFresh && !state.dirty.fan) {
    fill("#fanStrategySelect", String(failsafe.failsafe));
    fill("#fanFallback1Input", failsafe.fallback1_duty_pct);
    fill("#fanFallback2Input", failsafe.fallback2_duty_pct);
    fill("#fanHoldInput", failsafe.stale_hold_s);
    fill("#fanRampDownInput", failsafe.ramp_down_pct_per_s);
  }
  if (!curveFresh && !failsafeFresh && !state.dirty.fan) {
    ["#fanTempOffInput", "#fanTempOnInput", "#fanTempFullInput", "#fanMinDutyInput", "#fanRampUpInput",
     "#fanFallback1Input", "#fanFallback2Input", "#fanHoldInput", "#fanRampDownInput"].forEach(id => { if ($(id)) $(id).value = ""; });
    $("#fanStrategySelect").value = "1";
  }

  // ACK stream
  const acks = snapshot.fan_ack_history || [];
  $("#fanAckList").innerHTML = acks.length ? acks.slice(0, 8).map(item =>
    `<div class="event-item"><time>${item.time}</time><b>${item.opcode_name} · 序号 ${item.sequence}</b>`
    + `<p class="${item.accepted ? "ok" : "bad"}">${item.result_name} · 模式 ${item.mode_name} · 失联 ${item.failsafe_name}`
    + ` · 输出 ${item.duty_pct[0]}/${item.duty_pct[1]}% · 目标 ${item.target_pct[0]}/${item.target_pct[1]}%</p></div>`
  ).join("") : '<div class="empty-state">尚未收到 0x5A5 应答。</div>';
}

function renderBench() {
  const snapshot = state.toolSnapshots.bench || {};
  const bench = snapshot.bench;
  const connection = snapshot.connection || {};
  const active = connection.connected === true && connection.mode === "bench" && bench?.active === true;
  const controls = $$("#page-bench [data-bench-command], #sendBenchCommand, #benchCommand");
  controls.forEach(node => { node.disabled = !active; });
  $("#connectBenchButton").classList.toggle("hidden", active);
  $("#disconnectBenchButton").classList.toggle("hidden", !active);
  text("#benchConnectionInfo", active
    ? `${connection.channel || "PCAN"} · CAN1 · 500 kbit/s`
    : "等待连接 CAN1");
  if (!active) {
    text("#benchStatus", "未连接");
    text("#benchFrameSummary", "未连接");
    $("#benchStatus").className = "tag neutral";
    $("#benchSlaves").innerHTML = '<div class="empty-state">连接 CAN1 后显示六个从控模型。</div>';
    return;
  }
  const online = (bench.slaves || []).filter(slave => slave.online).length;
  text("#benchStatus", "运行中 · " + online + "/6 在线");
  $("#benchStatus").className = "tag " + (online === 6 ? "ok" : "warn");
  text("#benchFrameSummary", `36 帧电压 + 6 帧温度 · 断线 ${bench.open_wire_cells || 0} 串 / ${bench.open_wire_temps || 0} 路`);
  $("#benchSlaves").innerHTML = (bench.slaves || []).map(slave =>
    `<div class="bench-slave ${slave.online ? "online" : "offline"}">`
    + `<span>从控 ${slave.id}</span><b>${slave.online ? "在线" : "离线"}</b>`
    + `<small>${slave.base_cell_mv} mV · ${slave.base_temp_c} °C</small></div>`
  ).join("");
}

async function runBenchCommand(command) {
  if (!state.api) return;
  const result = await state.api.bench_command(command);
  if (!result.ok) return toast(result.error || "台架命令失败", true);
  text("#benchResult", result.message || "命令已执行");
  await poll();
}

async function sendBenchCommand() {
  const input = $("#benchCommand");
  const command = input.value.trim();
  if (!command) return;
  await runBenchCommand(command);
  input.value = "";
}

/** Classify one cell or temperature against the thresholds currently reported by the master. */
function cellStatus(item, voltage, thresholds) {
  if (item.value == null) return "invalid";
  if (voltage) {
    if (thresholds.uv_mv != null && item.value <= thresholds.uv_mv) return "low";
    if (thresholds.ov_mv != null && item.value >= thresholds.ov_mv) return "high";
  } else {
    if (thresholds.ut_c != null && item.value <= thresholds.ut_c) return "low";
    if (thresholds.ot_c != null && item.value >= thresholds.ot_c) return "high";
  }
  return "";
}

function extremes(items) {
  const values = items.filter(item => item.value != null).map(item => item.value);
  return values.length
    ? { max: Math.max(...values), min: Math.min(...values), valid: values.length }
    : { max: null, min: null, valid: 0 };
}

/** Badge counts every cell and temperature the master is not currently reporting as valid. */
function renderCellBadge() {
  const cells = state.snapshot?.cells || [], temps = state.snapshot?.temps || [];
  const missing = cells.filter(item => item.value == null).length + temps.filter(item => item.value == null).length;
  text("#cellNavBadge", missing);
  $("#cellNavBadge").classList.toggle("hidden", missing === 0);
}

function renderCells() {
  if (!state.snapshot) return;
  if (state.snapshot.raw_cell_data_available !== true) {
    ["#gridMax", "#gridMin", "#gridDelta", "#tempGridMax", "#tempGridMin", "#tempGridDelta"].forEach(id => text(id, "—"));
    $("#cellDataIssue").classList.add("hidden");
    $("#cellModules").innerHTML = '<div class="filter-empty"><b>CANB 无单体原始数据</b><span>请连接 CAN1 查看 138 串电压和 48 路温度。</span></div>';
    return;
  }
  const cells = state.snapshot.cells || [], temps = state.snapshot.temps || [];
  const cellTotal = extremes(cells), tempTotal = extremes(temps);
  text("#gridMax", cellTotal.max == null ? "—" : `${cellTotal.max} mV`);
  text("#gridMin", cellTotal.min == null ? "—" : `${cellTotal.min} mV`);
  text("#gridDelta", cellTotal.max == null ? "—" : `${cellTotal.max - cellTotal.min} mV`);
  text("#tempGridMax", tempTotal.max == null ? "—" : `${tempTotal.max} °C`);
  text("#tempGridMin", tempTotal.min == null ? "—" : `${tempTotal.min} °C`);
  text("#tempGridDelta", tempTotal.max == null ? "—" : `${fmt(tempTotal.max - tempTotal.min, 1)} °C`);
  const missingCells = cells.length - cellTotal.valid;
  const missingTemps = temps.length - tempTotal.valid;
  const dataIssue = $("#cellDataIssue");
  dataIssue.classList.toggle("hidden", missingCells === 0 && missingTemps === 0);
  dataIssue.textContent = `缺失 ${missingCells} 串 / ${missingTemps} 路`;
  const config = state.snapshot.config || {};
  const thresholds = isFresh(config.thresholds_age, SLOW_DATA_FRESH_MAX_S) ? (config.thresholds || {}) : {};
  text("#displayOv", thresholds.ov_mv == null ? "等待数据" : `${thresholds.ov_mv} mV`);
  text("#displayUv", thresholds.uv_mv == null ? "等待数据" : `${thresholds.uv_mv} mV`);
  text("#displayOt", thresholds.ot_c == null ? "等待数据" : `${thresholds.ot_c} °C`);
  text("#displayUt", thresholds.ut_c == null ? "等待数据" : `${thresholds.ut_c} °C`);
  const onlyAbnormal = $("#onlyAbnormal").checked;
  const renderItem = (item, voltage, localNo) => {
    const status = cellStatus(item, voltage, thresholds);
    if (onlyAbnormal && !status) return "";
    const label = voltage ? "C" : "T", unit = voltage ? "mV" : "°C";
    return `<div class="cell-item ${status}" title="${item.status} · 最近数据 ${item.age ?? "—"} s">`
      + `<small>${label}${localNo}</small><b>${item.value ?? "—"}<em>${unit}</em></b></div>`;
  };
  const modules = Array.from({ length: 6 }, (_, index) => {
    const cellGroup = cells.filter(item => item.module === index + 1);
    const tempGroup = temps.filter(item => item.module === index + 1);
    const module = state.snapshot.modules[index] || {};
    const cellStats = extremes(cellGroup), tempStats = extremes(tempGroup);
    const cellMarkup = cellGroup.map((item, itemIndex) => renderItem(item, true, itemIndex + 1)).join("");
    const tempMarkup = tempGroup.map((item, itemIndex) => renderItem(item, false, itemIndex + 1)).join("");
    if (onlyAbnormal && !cellMarkup && !tempMarkup) return "";
    const missing = cellGroup.length - cellStats.valid + tempGroup.length - tempStats.valid;
    const moduleIssue = !module.online
      ? `<span class="module-issue">通信中断</span>`
      : missing > 0 ? `<span class="module-issue">缺失 ${missing} 项</span>` : "";
    return `<section class="cell-module"><div class="cell-module-head">`
      + `<strong>BMU ${index + 1}</strong>`
      + moduleIssue
      + `<span class="module-stats"><span>电压 <b>${cellStats.min ?? "—"}–${cellStats.max ?? "—"} mV</b></span>`
      + `<span>压差 <b>${cellStats.max == null ? "—" : cellStats.max - cellStats.min} mV</b></span>`
      + `<span>温度 <b>${tempStats.min == null ? "—" : fmt(tempStats.min, 1)}–${tempStats.max == null ? "—" : fmt(tempStats.max, 1)} °C</b></span></span>`
      + `</div><div class="combined-module-body"><div aria-label="BMU ${index + 1} 电压"><div class="cell-grid voltage-grid">${cellMarkup}</div></div>`
      + `<div aria-label="BMU ${index + 1} 温度"><div class="cell-grid temperature-grid">${tempMarkup}</div></div></div></section>`;
  }).join("");
  $("#cellModules").innerHTML = modules || `<div class="filter-empty"><b>当前没有异常或缺失数据</b><span>138 串电压与 48 路温度均在当前阈值范围内。</span></div>`;
}

function renderFrames() {
  if (!state.snapshot) return;
  const frames = state.framePaused ? state.pausedFrames : state.snapshot.raw_frames;
  const query = $("#frameSearch").value.trim().toLowerCase();
  const filtered = frames.filter(frame => (state.frameKind === "all" || frame.direction === state.frameKind) && (!query || `${frame.id} ${frame.name} ${frame.data}`.toLowerCase().includes(query))).slice(0, 180);
  $("#frameRows").innerHTML = filtered.map(frame => `<tr class="${frame.direction}"><td>${frame.time}</td>`
    + `<td><span class="dir-tag ${frame.direction}">${frame.direction.toUpperCase()}</span></td>`
    + `<td>${frame.id}</td><td>${frame.extended ? "扩展" : "标准"}</td><td>${frame.dlc}</td>`
    + `<td title="${frame.data}">${frame.data}</td><td title="${frame.name}">${frame.name}</td></tr>`).join("");
  $("#frameEmpty").classList.toggle("hidden", filtered.length > 0);
}

function timeLabel(seconds) {
  const value = Math.max(0, Math.floor(seconds || 0));
  const hours = Math.floor(value / 3600), minutes = Math.floor(value % 3600 / 60), secs = value % 60;
  return hours ? `${String(hours).padStart(2,"0")}:${String(minutes).padStart(2,"0")}:${String(secs).padStart(2,"0")}` : `${String(minutes).padStart(2,"0")}:${String(secs).padStart(2,"0")}`;
}

function renderReplay() {
  const connection = state.snapshot?.connection, replay = connection?.replay;
  $("#replayBar").classList.toggle("hidden", !replay);
  if (!replay) return;
  text("#replayFile", connection.channel); text("#replayPosition", `${timeLabel(replay.position)} / ${timeLabel(replay.duration)}`);
  $("#replaySeek").value = replay.duration ? Math.round(replay.position / replay.duration * 1000) : 0;
  $("#replayPlay").textContent = replay.paused ? "▶" : "Ⅱ";
  $("#replaySpeed").value = String(replay.speed);
}

function confirmCommand(name, values, title, message, destructive = false) {
  const conn = state.snapshot?.connection;
  if (!conn?.connected) return toast("请先连接 CAN", true);
  if (conn.mode === "replay") return toast("历史回放为只读，不能发送命令", true);
  if (conn.bus_profile !== "can1") return toast("工具命令只能从 CAN1 发送", true);
  state.pendingCommand = { name, values };
  state.pendingIvtAction = null;
  state.pendingFanCommand = null;
  text("#confirmTitle", title); text("#confirmMessage", message);
  text("#confirmPayload", `通道：${conn.channel || "PCAN"}\n`
    + `总线：CAN1 · ${conn.bitrate ? conn.bitrate / 1000 : 500} kbit/s\n`
    + `主控状态：${state.snapshot.overview.state_name}\n`
    + `告警等级：${state.snapshot.overview.alarm_level_name}\n`
    + `命令：${name}`);
  $("#confirmCheck").checked = false; $("#doConfirm").disabled = true;
  $("#doConfirm").className = destructive ? "danger-button" : "action-button";
  $("#confirmDialog").showModal();
}

async function sendPendingCommand() {
  if (!state.api) return;
  if (state.pendingIvtAction) {
    const pending = state.pendingIvtAction;
    $("#doConfirm").disabled = true;
    const result = pending.kind === "configure"
      ? await state.api.configure_ivt_bms_canb(pending.options)
      : await state.api.switch_ivt_bitrate(pending.options);
    if (result.ok) {
      $("#confirmDialog").close();
      state.pendingIvtAction = null;
      toast(result.message || "IVT 操作完成");
      await poll();
    } else {
      toast(result.error || "IVT 操作失败", true);
      $("#doConfirm").disabled = false;
    }
    return;
  }
  if (state.pendingFanCommand) {
    const pending = state.pendingFanCommand;
    $("#doConfirm").disabled = true;
    const result = await state.api.send_fan_command(pending.name, pending.values, true);
    if (result.ok) {
      $("#confirmDialog").close();
      state.pendingFanCommand = null;
      if (["fan_curve", "fan_failsafe", "fan_restore_defaults"].includes(pending.name)) {
        state.dirty.fan = false;
      }
      toast(result.message || "风扇命令已执行");
      await poll();
    } else {
      toast(result.error || "风扇命令失败", true);
      $("#doConfirm").disabled = false;
    }
    return;
  }
  if (!state.pendingCommand) return;
  $("#doConfirm").disabled = true;
  const command = state.pendingCommand.name;
  const result = await state.api.send_command(command, state.pendingCommand.values, true);
  if (result.ok) {
    const dirtyMap = { alarm_thresholds: "thresholds", alarm_switches: "switches", charge_config: "charge", current_direction: "direction", charger_type: "chargerType" };
    if (dirtyMap[command]) state.dirty[dirtyMap[command]] = false;
    watchFlashSave(command, result.ack);
    $("#confirmDialog").close(); toast(result.message || "命令已发送");
    state.pendingCommand = null;
    await poll();
  }
  else { toast(result.error || "发送失败", true); $("#doConfirm").disabled = false; }
}

async function toggleRecording() {
  if (!state.api) return;
  if (state.recording) {
    await state.api.stop_recording(); state.recording = false; text("#recordButton", "● 记录数据"); toast("CAN 数据记录已停止");
  } else {
    const result = await state.api.choose_record_file();
    if (result.ok) { state.recording = true; text("#recordButton", "■ 停止记录"); toast(`正在记录 ${result.format === "bmslog" ? "BMSLOG" : "CSV"}：${result.path}`); }
    else if (!result.cancelled) toast(result.error || "无法开始记录", true);
  }
}

async function openReplay() {
  if (!state.api) return;
  if (state.recording) return toast("请先停止当前数据记录", true);
  const result = await state.api.choose_replay_file();
  if (result.ok) { toast(`已载入 ${result.frames.toLocaleString()} 帧历史记录`); showPage("frames"); await poll(); }
  else if (!result.cancelled) toast(result.error || "历史记录载入失败", true);
}

async function toggleReplay() {
  const replay = state.snapshot?.connection?.replay; if (!replay || !state.api) return;
  await state.api.replay_control(replay.paused ? "play" : "pause"); await poll();
}

document.addEventListener("DOMContentLoaded", init);
