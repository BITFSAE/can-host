/* BMS 页面模块：运行总览、电芯与温度、故障与记录、参数与命令。 */

const PACK_CAPACITY_AH = 16.2;
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
  "保留位 · 当前固定为 0", "充电机反馈 >500 ms · 按在线源定级",
  "命令批次重试超时或应答错误 · 复位前锁存",
  "BMU 1 通信离线", "BMU 2 通信离线", "BMU 3 通信离线",
  "BMU 4 通信离线", "BMU 5 通信离线", "BMU 6 通信离线",
  "IVT U1失联约360 ms",
];

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

function bindBmsControls() {
  $("#onlyAbnormal").addEventListener("change", renderCells);
  $("#sendThresholds").addEventListener("click", () => confirmCommand(
    "alarm_thresholds",
    { ov_mv: +$("#ovInput").value, uv_mv: +$("#uvInput").value, ot_c: +$("#otInput").value, ut_c: +$("#utInput").value },
    "写入单体告警阈值", `OV ${$("#ovInput").value} mV\nUV ${$("#uvInput").value} mV\nOT ${$("#otInput").value} °C\nUT ${$("#utInput").value} °C`
  ));
  $("#sendSwitches").addEventListener("click", () => {
    const switches = {};
    $$("#switchList input").forEach(input => switches[input.dataset.key] = input.checked);
    confirmCommand("alarm_switches", { switches }, "写入告警开关", "将当前页面的全部开关作为一组写入主控。基础安全保护关闭后故障仍检测并上报，只是不再触发保护动作；写入后以周期回报值为准。");
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
  ["#ovInput", "#uvInput", "#otInput", "#utInput"].forEach(id => $(id).addEventListener("input", () => state.dirty.thresholds = true));
  ["#chargeVoltage", "#chargeCurrent"].forEach(id => $(id).addEventListener("input", () => state.dirty.charge = true));
  $("#switchList").addEventListener("change", () => {
    state.dirty.switches = true;
    updateSwitchRowState();
  });
  $("#currentDirection").addEventListener("change", () => state.dirty.direction = true);
  $("#chargerType").addEventListener("change", () => state.dirty.chargerType = true);
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
  const safetyEventKnown = hvFresh;
  const safetyEventActive = safetyEventKnown && !!hv.external_safety_event;
  text("#safetyEvent", !safetyEventKnown ? "等待数据" : safetyEventActive ? "已触发" : "未触发");
  setClass("#safetyEvent", "bad", false);
  setClass("#safetyEvent", "warn", safetyEventKnown && safetyEventActive);
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
    const alarmTitle = node.querySelector("b");
    const alarmRule = node.querySelector("small");
    const ruleText = alarmRuleText(alarm.index, thresholds);
    alarmTitle.textContent = alarm.name;
    alarmTitle.title = alarm.name;
    alarmRule.textContent = ruleText;
    alarmRule.title = ruleText;
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
    `<div class="event-item"><time>${event.timestamp}</time><b>${event.fault_code}</b><p>${event.event_type_name || `类型 ${event.event_type}`} · 详情 ${event.event_detail}</p></div>`
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
    [...statusList.querySelectorAll(".protection-switch")].forEach((node, index) => {
      const item = state.bootstrap.switch_catalog[index];
      if (!item) return;
      const enabled = hasSwitchReport && !!switches[item.key];
      const stateClass = !hasSwitchReport ? "pending" : enabled ? "enabled" : "disabled";
      const stateText = !hasSwitchReport ? "等待" : enabled ? "启用" : "关闭";
      node.className = `protection-switch ${stateClass}`;
      node.querySelector(".switch-state").textContent = stateText;
    });
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
  renderRtcReply(state.snapshot.rtc_reply || {}, runtime);
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

function renderRtcReply(reply, runtime) {
  const pad = value => String(value).padStart(2, "0");
  if (reply.status != null) {
    const name = RTC_STATUS_NAMES[reply.status] || `未知 ${reply.status}`;
    return text("#rtcReply", !reply.year ? `${name} · 时间读取失败`
      : `${name} · ${reply.year}-${pad(reply.month)}-${pad(reply.day)} ${pad(reply.hour)}:${pad(reply.minute)}:${pad(reply.second)}`);
  }
  const runtimeFresh = runtime?.age != null && runtime.age <= 1.5;
  text("#rtcReply", !runtimeFresh ? "等待数据" : runtime.rtc_valid ? "主控 RTC 有效" : "RTC 未校时");
}

function buildSwitchList() {
  const catalog = state.bootstrap?.switch_catalog || [];
  $("#switchList").innerHTML = catalog.map(item =>
    `<label class="switch-row" data-key="${item.key}" title="${item.name} · ${item.code} · ${item.variable}"><i class="switch-dot"></i>`
    + `<span class="switch-label"><b>${item.name}</b><small>${item.code}</small></span>`
    + `<b class="switch-state">等待</b><input type="checkbox" data-key="${item.key}"><span class="switch-track"></span></label>`
  ).join("");
}

/** Build the protection-switch status grid once; renderConfig only patches state
 *  text and class afterwards so the static title tooltips survive polling. */
function buildSwitchStatusList() {
  const catalog = state.bootstrap?.switch_catalog || [];
  const statusList = $("#switchStatusList");
  if (!statusList) return;
  statusList.innerHTML = catalog.map(item =>
    `<span class="protection-switch" title="${item.name} · ${item.code} · ${item.variable}">`
    + `<i></i><span class="protection-label"><b>${item.name}</b><small>${item.code}</small></span><b class="switch-state">等待</b></span>`
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

function makeCellItem(label, unit) {
  const root = document.createElement("div");
  root.className = "cell-item";
  root.innerHTML = `<small>${label}</small><b><span class="cell-value"></span><em>${unit}</em></b>`;
  return { root, value: root.querySelector(".cell-value"), unit: root.querySelector("em") };
}

function cellDisplayValue(item) {
  if (item.status === "断线") return "断";
  if (item.value == null) return "—";
  return String(item.value);
}

/** Build the fixed 6-module / 23-cell / 8-temp grid once so later polls only patch
 *  text, class and title in place. Rebuilding the DOM every poll kept destroying the
 *  element under the cursor, which made the native title tooltip (data age) flicker. */
function buildCellGrids() {
  const container = $("#cellModules");
  container.innerHTML = "";
  const placeholder = document.createElement("div");
  placeholder.className = "filter-empty";
  placeholder.innerHTML = "<b>当前没有异常或缺失数据</b><span>138 串电压与 48 路温度均在当前阈值范围内。</span>";
  placeholder.classList.add("hidden");
  container.appendChild(placeholder);
  const modules = [];
  for (let index = 0; index < 6; index++) {
    const section = document.createElement("section");
    section.className = "cell-module";
    section.innerHTML = `<div class="cell-module-head"><strong>BMU ${index + 1}</strong>`
      + `<span class="module-issue hidden"></span>`
      + `<span class="module-stats"><span>电压 <b></b></span><span>压差 <b></b></span><span>温度 <b></b></span></span></div>`
      + `<div class="combined-module-body">`
      + `<div aria-label="BMU ${index + 1} 电压"><div class="cell-grid voltage-grid"></div></div>`
      + `<div aria-label="BMU ${index + 1} 温度"><div class="cell-grid temperature-grid"></div></div></div>`;
    container.appendChild(section);
    const cells = Array.from({ length: 23 }, (_, i) => makeCellItem(`C${i + 1}`, "mV"));
    const temps = Array.from({ length: 8 }, (_, i) => makeCellItem(`T${i + 1}`, "°C"));
    section.querySelector(".voltage-grid").append(...cells.map(item => item.root));
    section.querySelector(".temperature-grid").append(...temps.map(item => item.root));
    const statBs = [...section.querySelectorAll(".module-stats b")];
    modules.push({ section, issue: section.querySelector(".module-issue"), statBs, cells, temps });
  }
  state.cellRefs = { placeholder, modules };
}

function updateCellItem(item, ref, voltage, onlyAbnormal, thresholds) {
  const status = cellStatus(item, voltage, thresholds);
  ref.root.className = `cell-item ${status}`;
  ref.root.classList.toggle("hidden", onlyAbnormal && !status);
  ref.root.title = `${item.status} · 最近数据 ${item.age ?? "—"} s`;
  ref.value.textContent = cellDisplayValue(item);
  if (ref.unit) ref.unit.classList.toggle("hidden", item.value == null);
}

function renderCells() {
  if (!state.snapshot) return;
  if (state.snapshot.raw_cell_data_available !== true) {
    ["#gridMax", "#gridMin", "#gridDelta", "#tempGridMax", "#tempGridMin", "#tempGridDelta"].forEach(id => text(id, "—"));
    $("#cellDataIssue").classList.add("hidden");
    const container = $("#cellModules");
    if (state.cellRefs || !container.firstElementChild) {
      state.cellRefs = null;
      container.innerHTML = '<div class="filter-empty"><b>CANB 无单体原始数据</b><span>请连接 CAN1 查看 138 串电压和 48 路温度。</span></div>';
    }
    return;
  }
  if (!state.cellRefs) buildCellGrids();
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
  let abnormalTotal = 0;
  state.cellRefs.modules.forEach((refs, index) => {
    const cellGroup = cells.filter(item => item.module === index + 1);
    const tempGroup = temps.filter(item => item.module === index + 1);
    const module = state.snapshot.modules[index] || {};
    const cellStats = extremes(cellGroup), tempStats = extremes(tempGroup);
    let moduleAbnormal = 0;
    cellGroup.forEach((item, itemIndex) => {
      if (itemIndex >= refs.cells.length) return;
      updateCellItem(item, refs.cells[itemIndex], true, onlyAbnormal, thresholds);
      if (item.value == null || cellStatus(item, true, thresholds)) moduleAbnormal += 1;
    });
    tempGroup.forEach((item, itemIndex) => {
      if (itemIndex >= refs.temps.length) return;
      updateCellItem(item, refs.temps[itemIndex], false, onlyAbnormal, thresholds);
      if (item.value == null || cellStatus(item, false, thresholds)) moduleAbnormal += 1;
    });
    abnormalTotal += moduleAbnormal;
    const missing = cellGroup.length - cellStats.valid + tempGroup.length - tempStats.valid;
    refs.issue.textContent = !module.online ? "通信中断" : missing > 0 ? `缺失 ${missing} 项` : "";
    refs.issue.classList.toggle("hidden", module.online && missing === 0);
    refs.statBs[0].textContent = `${cellStats.min ?? "—"}–${cellStats.max ?? "—"} mV`;
    refs.statBs[1].textContent = cellStats.max == null ? "—" : `${cellStats.max - cellStats.min} mV`;
    refs.statBs[2].textContent = `${tempStats.min == null ? "—" : fmt(tempStats.min, 1)}–${tempStats.max == null ? "—" : fmt(tempStats.max, 1)} °C`;
    refs.section.classList.toggle("hidden", onlyAbnormal && moduleAbnormal === 0);
  });
  state.cellRefs.placeholder.classList.toggle("hidden", !(onlyAbnormal && abnormalTotal === 0));
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
