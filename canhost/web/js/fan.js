/* 整车风扇页面模块：FanController 遥测与配置，通过统一 CANB 连接收发。 */

let lastBatteryCalibSource = null;
let pendingBatteryFanSave = null;
const BATTERY_FAN_PACK_FRESH_S = 2.0;
const BATTERY_FAN_PDM_FRESH_S = 1.5;
const BATTERY_FAN_STATUS_FRESH_S = 1.5;
const BATTERY_FAN_CALIB_FRESH_S = 3.0;

function fanConnectionAvailable() {
  const connection = state.vehicleSnapshot?.connection;
  return connection?.connected === true && connection.mode === "pcan"
    && connection.bus_profile === "canb" && Number(connection.bitrate) === 500000;
}

function readFanNumber(id, label) {
  const input = $(id);
  if (!input || input.value.trim() === "" || !input.checkValidity()) {
    input?.reportValidity();
    toast(`${label}超出允许范围或未填写`, true);
    return null;
  }
  const value = Number(input.value);
  if (!Number.isFinite(value)) {
    input.reportValidity();
    toast(`${label}必须是有效数字`, true);
    return null;
  }
  return value;
}

let vehicleCalibStarting = false;
function bindFanControls() {
  $("#vehicleCalibTier")?.addEventListener("change", () => {
    const battery = $("#vehicleCalibTier").value === "battery";
    $("#vehicleCalibCurrent").value = battery ? "8" : "18";
    $("#vehicleCalibCurrent").max = battery ? "8" : "20";
  });
  $("#vehicleCalibStart")?.addEventListener("click", () => {
    const hold_s = readFanNumber("#vehicleCalibHold", "测点保持时间");
    const max_current_a = readFanNumber("#vehicleCalibCurrent", "总线电流保护");
    if (hold_s == null || max_current_a == null) return;
    const channel = Number($("#vehicleCalibChannel").value);
    const tier = $("#vehicleCalibTier").value;
    confirmFanAction("开始整车风扇标定", "将单独扫描所选回路的 0%～100% 上升和下降测点，并记录电流、功率和转速。",
      "我已确认车辆静止、供电稳定、风道无遮挡且人员远离旋转部件。", async () => {
        vehicleCalibStarting = true;
        try {
          const res = await state.api.start_fan_calibration({channel, tier, hold_s, max_current_a});
          toast(res?.ok ? "整车风扇扫频已启动" : `启动失败：${res?.error || "未知原因"}`, !res?.ok);
        } finally { vehicleCalibStarting = false; }
      });
  });
  $("#vehicleCalibStop")?.addEventListener("click", async () => {
    const res = await state.api.stop_fan_calibration();
    toast(res?.ok ? "整车风扇标定已停止" : `停止失败：${res?.error || "未知原因"}`, !res?.ok);
  });
  for (const [id, format] of [["#vehicleCalibCsv", "csv"], ["#vehicleCalibJson", "json"]]) {
    $(id)?.addEventListener("click", async () => {
      try {
        const res = await state.api.choose_export_fan_calibration(format);
        if (res?.ok) toast(`标定记录已导出：${res.path}`);
        else if (!res?.cancelled) toast(res?.error || "导出失败", true);
      } catch (error) { toast(`导出失败：${error}`, true); }
    });
  }

  $("#sendFanProfile")?.addEventListener("click", () => {
    const lease = readFanNumber("#fanProfileLease", "有效时间");
    if (lease == null) return;
    const profile = Number($("#fanProfileSelect").value);
    confirmFanCommand("fan_profile", { profile, lease_s: lease }, "下发散热档位",
      `档位 ${["保守", "默认", "激进"][profile]} · 有效 ${lease} s；到期回默认档。`);
  });
  $("#clearFanFaults")?.addEventListener("click", () => {
    confirmFanCommand("fan_clear_faults", {}, "清除停转锁存", "允许故障回路重新启动，仍遵守供电和电流限制。", true);
  });
  $("#fanModeSelect")?.addEventListener("change", renderFanControlFields);
  $("#sendFanControl")?.addEventListener("click", () => {
    const mode = $("#fanModeSelect").value;
    if (mode === "1") {
      const duty1 = readFanNumber("#fanDuty1Input", "PWM1目标");
      const duty2 = readFanNumber("#fanDuty2Input", "PWM2目标");
      const lease = readFanNumber("#fanLeaseInput", "有效时间");
      if (duty1 == null || duty2 == null || lease == null) return;
      const values = { mode: 1, duty1_pct: duty1, duty2_pct: duty2, lease_s: lease };
      const summary = `模式 手动 · PWM1 ${values.duty1_pct}% · PWM2 ${values.duty2_pct}% · 有效 ${values.lease_s} s`;
      confirmFanCommand("fan_control", values, "发送风扇手动模式", summary + "\n有效时间到期后自动回到温控模式；需要保持时请按短于有效时间的间隔重发。");
    } else if (mode === "2") {
      const lease = readFanNumber("#fanLeaseInput", "有效时间");
      if (lease == null) return;
      confirmFanCommand("fan_control", { mode: 2, duty1_pct: 0, duty2_pct: 0, lease_s: lease },
        "发送风扇关闭命令", `模式 关闭 · 两路 0% · 有效 ${lease} s\n到期自动回到温控模式。`, true);
    } else {
      confirmFanCommand("fan_control", { mode: 0, duty1_pct: 0, duty2_pct: 0, lease_s: 0 },
        "回到风扇自动温控", "模式 自动 · 由 FanController 按电机/控制器温度自动调速。");
    }
  });
  $("#fanQueryButton")?.addEventListener("click", () => {
    confirmFanCommand("fan_query", {}, "查询风扇当前策略", "读取当前档位、实际策略和保护状态。");
  });
  $("#fanRestoreButton")?.addEventListener("click", () => {
    confirmFanCommand("fan_restore_defaults", {}, "恢复风扇默认档",
      "回到默认档和自动模式，保留停转锁存。", true);
  });

  const sendBatteryFan = (name, values, title, message, destructive = false) => {
    confirmFanAction(title, message, "我已核对电池箱风扇、供电状态和旋转部件安全。", async () => {
      const res = await pywebview.api.send_battery_fan_command(name, values, true);
      if (res?.ok && ["battery_fan_commit", "battery_fan_clear"].includes(name)) {
        pendingBatteryFanSave = {
          generation: Number(res.calibration_generation),
          chroma: name === "battery_fan_commit" ? values.chroma_cap_pct : 55,
          hv: name === "battery_fan_commit" ? values.hv_cap_pct : 55,
          calibrated: name === "battery_fan_commit",
        };
        state.dirty.batteryFanCaps = true;
      }
      const acceptedSave = res?.ok && pendingBatteryFanSave
        && ["battery_fan_commit", "battery_fan_clear"].includes(name);
      toast(res?.ok ? (acceptedSave ? "命令已接受；等待新 0x5AD 核对上限及 Flash 保存位"
        : res.message || "电池箱风扇命令已执行") : `命令失败：${res?.error || "未知原因"}`, !res?.ok);
    }, destructive);
  };
  $("#batteryFanControlButton")?.addEventListener("click", () => {
    const mode = +$("#batteryFanModeSelect").value;
    const duty = mode === 1 ? readFanNumber("#batteryFanDutyInput", "占空比") : 0;
    const lease = mode === 0 ? 0 : readFanNumber("#batteryFanLeaseInput", "租约");
    if (duty == null || lease == null) return;
    sendBatteryFan("battery_fan_control", {
      mode, duty_pct: duty, lease_s: lease,
    }, "控制电池箱风扇", `模式 ${["自动", "手动", "关闭"][mode]}；普通手动仍受当前 35W/70W 上限约束。`, mode === 2);
  });
  $("#batteryFanAutoStartButton")?.addEventListener("click", () => {
    const hold_s = readFanNumber("#batteryFanAutoHoldInput", "稳态保持时间");
    const max_current_a = readFanNumber("#batteryFanAutoCurrentInput", "总线电流保护");
    if (hold_s == null || max_current_a == null) return;
    confirmFanAction("确认电池箱风扇自动扫频", "将使用 CANB 实时状态，采集0%基线并逐档计算增量功率，完成后给出35W/70W建议上限。",
      "我已确认车辆静止、供电状态稳定、风道无遮挡且人员远离旋转部件。", async () => {
        const res = await pywebview.api.start_battery_fan_calibration({
          hold_s, max_current_a,
        });
        if (res?.ok) state.dirty.batteryFanCaps = false;
        toast(res?.ok ? "电池箱风扇自动扫频已启动" : `启动失败：${res?.error || "未知原因"}`, !res?.ok);
      });
  });
  $("#batteryFanAutoStopButton")?.addEventListener("click", async () => {
    const res = await pywebview.api.stop_battery_fan_calibration();
    toast(res?.ok ? "已中止电池箱风扇标定" : `中止失败：${res?.error || "未知原因"}`, !res?.ok);
  });
  const exportBatteryFan = async format => {
    try {
      const res = await state.api.choose_export_battery_fan_calibration(format);
      if (res?.ok) toast(`标定记录已导出：${res.path}`);
      else if (!res?.cancelled) toast(res?.error || "标定记录导出失败", true);
    } catch (e) {
      toast(`导出失败：${e}`, true);
    }
  };
  $("#batteryFanExportButton")?.addEventListener("click", () => exportBatteryFan("csv"));
  $("#batteryFanExportJsonButton")?.addEventListener("click", () => exportBatteryFan("json"));
  $("#batteryFanCalibButton")?.addEventListener("click", () => {
    const action = +$("#batteryFanCalibAction").value;
    const step = readFanNumber("#batteryFanStepInput", "步骤");
    const duty = action === 3 ? 0 : readFanNumber("#batteryFanCalibDutyInput", "标定占空比");
    const lease = action === 3 ? 0 : readFanNumber("#batteryFanCalibLeaseInput", "标定租约");
    if (step == null || duty == null || lease == null) return;
    sendBatteryFan("battery_fan_calib", {
      action, step, duty_pct: duty, lease_s: lease,
    }, "发送电池箱风扇标定步骤", "标定会话可越过当前运行上限；供电切换、超温、停转或租约到期会立即中止。", action === 3);
  });
  $("#batteryFanCommitButton")?.addEventListener("click", () => {
    const chroma_cap_pct = readFanNumber("#batteryFanChromaCapInput", "35W 充电车上限");
    const hv_cap_pct = readFanNumber("#batteryFanHvCapInput", "70W 高压上限");
    if (chroma_cap_pct == null || hv_cap_pct == null) return;
    if (chroma_cap_pct > hv_cap_pct) return toast("上限必须满足 35W 充电车 ≤ 70W 高压", true);
    sendBatteryFan("battery_fan_commit", { chroma_cap_pct, hv_cap_pct },
      "提交电池箱风扇功率上限", `35W 充电车 ${chroma_cap_pct}% · 70W 高压 ${hv_cap_pct}%\n停止有效标定会话后才能提交。`);
  });
  $("#batteryFanClearButton")?.addEventListener("click", () => sendBatteryFan(
    "battery_fan_clear", {}, "清除电池箱风扇标定", "清除保存值并恢复两档 55% 上限；只允许未上高压且非充电时执行。", true));

  ["#batteryFanChromaCapInput", "#batteryFanHvCapInput"]
    .forEach(id => $(id)?.addEventListener("input", () => state.dirty.batteryFanCaps = true));
  $("#batteryFanModeSelect")?.addEventListener("change", renderBatteryFanControlFields);
  $("#batteryFanCalibAction")?.addEventListener("change", renderBatteryFanCalibFields);
  renderFanControlFields();
  renderBatteryFanControlFields();
  renderBatteryFanCalibFields();
}

function confirmFanCommand(name, values, title, message, destructive = false) {
  if (!fanConnectionAvailable()) return toast("请先在整车页连接 CANB（真实 PCAN）", true);
  state.pendingCommand = null;
  state.pendingIvtAction = null;
  state.pendingFanAction = null;
  state.pendingFanCommand = { name, values };
  const conn = state.vehicleSnapshot.connection;
  text("#confirmTitle", title);
  text("#confirmMessage", message);
  setConfirmModeBadge(destructive ? "风扇高危操作 · 需二次确认" : "CANB 风扇命令", destructive ? "bad" : "");
  text("#confirmPayload", `通道：${conn.channel || "PCAN"}\n`
    + `总线：CANB · ${(conn.bitrate || 500000) / 1000} kbit/s\n`
    + `命令：${name}`);
  $("#confirmCheck").checked = false;
  $("#doConfirm").disabled = true;
  $("#doConfirm").className = destructive ? "danger-button" : "action-button";
  $("#confirmDialog").showModal();
}

/**
 * 复用同一个确认对话框执行一个自定义动作（例如启动自动扫频标定）。
 * 必须勾选 checkLabel 指定的确认项后才会调用 run，避免直接触发高风险流程。
 */
function confirmFanAction(title, message, checkLabel, run, destructive = false) {
  if (!fanConnectionAvailable()) return toast("请先在整车页连接 CANB（真实 PCAN）", true);
  state.pendingCommand = null;
  state.pendingIvtAction = null;
  state.pendingFanCommand = null;
  state.pendingFanAction = { run };
  const conn = state.vehicleSnapshot.connection;
  text("#confirmTitle", title);
  text("#confirmMessage", message);
  setConfirmModeBadge(destructive ? "风扇高危操作 · 需二次确认" : "CANB 风扇动作", destructive ? "bad" : "");
  text("#confirmPayload", `通道：${conn.channel || "PCAN"}\n`
    + `总线：CANB · ${(conn.bitrate || 500000) / 1000} kbit/s\n`
    + `操作：${title}`);
  text("#confirmCheckLabel", checkLabel);
  $("#confirmCheck").checked = false;
  $("#doConfirm").disabled = true;
  $("#doConfirm").className = destructive ? "danger-button" : "action-button";
  $("#confirmDialog").showModal();
}

/* 过期只作用于整个读数单元：单元格退到次级灰、单位与尾注转告警色。
   直接给单元格里的 <b> 加 data-stale 不会命中任何规则，必须落到单元上。 */
function markFanCellStale(idOrNode, stale) {
  const node = typeof idOrNode === "string" ? $(idOrNode) : idOrNode;
  if (!node) return;
  const cell = node.closest(".readout-grid > *") || node;
  markStaleData(cell, stale);
}

function renderFanControlFields() {
  const mode = $("#fanModeSelect").value;
  const manual = mode === "1";
  $("#fanDuty1Input").disabled = !manual;
  $("#fanDuty2Input").disabled = !manual;
  $("#fanLeaseInput").disabled = mode === "0";
}

function renderBatteryFanControlFields() {
  const mode = $("#batteryFanModeSelect")?.value || "0";
  if ($("#batteryFanDutyInput")) $("#batteryFanDutyInput").disabled = mode !== "1";
  if ($("#batteryFanLeaseInput")) $("#batteryFanLeaseInput").disabled = mode === "0";
}

function renderBatteryFanCalibFields() {
  const action = $("#batteryFanCalibAction")?.value || "1";
  const stopped = action === "3";
  if ($("#batteryFanCalibDutyInput")) $("#batteryFanCalibDutyInput").disabled = stopped;
  if ($("#batteryFanCalibLeaseInput")) $("#batteryFanCalibLeaseInput").disabled = stopped;
}

function renderFan() {
  const snapshot = state.vehicleSnapshot || {};
  const connection = snapshot.connection || {};
  const available = fanConnectionAvailable();
  const fan = snapshot.fan || {};
  const profile = fan.profile_status || {};
  const profileFresh = isFresh(fan.profile_status_age, 1.5) && profile.supported === true;
  text("#fanProfileReport", profileFresh ? `请求 ${profile.profile_name} · 剩余 ${profile.lease_remaining_s} s`
    : profile.protocol_version != null && profile.protocol_version !== 4 ? "固件版本不支持，请升级至 V4"
    : fan.calib_limits?.protocol_version === 3 ? "检测到旧版固件，请升级至 V4" : "等待 V4 策略状态");
  text("#fanEffectiveReport", profileFresh ? `PWM1：${profile.effective_names[0]}，上限 ${profile.cap_pct[0]}% · PWM2：${profile.effective_names[1]}，上限 ${profile.cap_pct[1]}%` : "PWM1 / PWM2：等待数据");
  const protections = profileFresh ? [
    profile.locked[0] ? "PWM1 停转锁存" : "", profile.locked[1] ? "PWM2 停转锁存" : "",
    profile.low_voltage ? "电池低压" : "", profile.invalid_temperature ? "温度输入异常" : "",
  ].filter(Boolean) : [];
  text("#fanProtectionReport", profileFresh ? protections.join(" · ") || "无锁存或输入异常" : "保护状态：等待数据");
  setClass("#fanProtectionReport", "bad", protections.length > 0);
  text("#fanDerateReport", profileFresh ? (profile.derate_requested ? "已请求整车降功率" : "无降功率请求") : "");
  $("#fanDerateReport")?.classList.toggle("bad", profileFresh && profile.derate_requested);
  const status = fan.status || {};
  const diag = fan.diagnostic || {};
  const power = fan.power_status || {};
  const statusKnown = hasDataAge(fan.status_age) && Object.keys(status).length > 0;
  const diagKnown = hasDataAge(fan.diagnostic_age) && Object.keys(diag).length > 0;
  const powerKnown = hasDataAge(fan.power_status_age) && Object.keys(power).length > 0;
  const statusFresh = isFresh(fan.status_age);
  const diagFresh = isFresh(fan.diagnostic_age);
  const powerFresh = isFresh(fan.power_status_age);
  const rpm = status.rpm || [];
  const duty = status.duty_pct || [];
  const target = diagKnown ? (diag.target_pct || []) : [];
  const faults = diagKnown ? (diag.faults || 0) : 0;
  const receiving = statusKnown || diagKnown;
  const pdmBus = snapshot.pdm?.bus || {};
  const pdmBattery = snapshot.pdm?.battery || {};
  const pdmValuesValid = [pdmBus.voltage_v, pdmBus.current_a, pdmBus.power_w]
    .every(value => value != null && Number.isFinite(Number(value)));
  const pdmFresh = !pdmBus.offline && isFresh(pdmBus.age, 1.5) && pdmValuesValid;
  const pdmBatteryFresh = !pdmBattery.offline && isFresh(pdmBattery.age, 1.5)
    && [pdmBattery.voltage_v, pdmBattery.current_a]
      .every(value => value != null && Number.isFinite(Number(value)));
  const pack = snapshot.pack || {};
  const packFresh = isFresh(pack.age, 1.5);

  // Render RPM and Tachometer card states
  const duty1 = duty[0] ?? 0;
  const duty2 = duty[1] ?? 0;
  const tachDefs = [
    { cardId: "#fanTachCard1", rpmId: "#fanRpm1", stateId: "#fanState1", rpm: rpm[0] ?? 0, duty: duty1, faultBit: 0 },
    { cardId: "#fanTachCard2", rpmId: "#fanRpm2", stateId: "#fanState2", rpm: rpm[1] ?? 0, duty: duty1, faultBit: 1 },
    { cardId: "#fanTachCard3", rpmId: "#fanRpm3", stateId: "#fanState3", rpm: rpm[2] ?? 0, duty: duty2, faultBit: 2 },
  ];

  tachDefs.forEach(item => {
    text(item.rpmId, statusKnown ? String(item.rpm) : "—");
    const card = $(item.cardId);
    const stateNode = $(item.stateId);
    if (!card || !stateNode) return;
    card.classList.remove("running", "stalled", "starting", "idle", "data-stale");
    stateNode.className = "cell-tail fan-tach-state";
    if (!statusKnown) {
      stateNode.textContent = "—";
    } else if (!statusFresh) {
      stateNode.textContent = "已过期";
      stateNode.classList.add("data-stale");
      card.classList.add("data-stale");
    } else if (diagFresh && (faults & (1 << item.faultBit))) {
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
    if (bar) {
      bar.style.width = statusKnown ? `${Math.max(0, Math.min(100, duty[index] ?? 0))}%` : "0%";
      bar.classList.toggle("data-stale", statusKnown && !statusFresh);
    }
    text(`#fanDuty${index + 1}Text`, statusKnown ? String(duty[index] ?? 0) : "等待数据");
    // 目标值贴在读数右端：实测值本身就是当前输出，两者不再串在一个字符串里。
    text(`#fanDuty${index + 1}Target`, statusKnown && target.length
      ? `${diagFresh ? "目标" : "上次目标"} ${target[index] ?? 0}%` : "");
    markFanCellStale(`#fanDuty${index + 1}Text`, statusKnown && (!statusFresh || (target.length && !diagFresh)));
  }

  // Render Temperatures & Source Indicators
  text("#fanModeText", diagKnown ? diag.mode_name || "未知" : "等待数据");
  text("#fanMotorTemp", !diagKnown ? "等待数据"
    : diag.motor_temp_c == null ? "失联" : fmt(diag.motor_temp_c, 1));
  text("#fanControllerTemp", !diagKnown ? "等待数据"
    : diag.controller_temp_c == null ? "失联" : fmt(diag.controller_temp_c, 1));
  ["#fanModeText", "#fanMotorTemp", "#fanControllerTemp"].forEach(id =>
    markFanCellStale(id, diagKnown && !diagFresh));

  const tempChips = [
    ["#fanTempChip506", diag.motor_temp_valid],
    ["#fanTempChip507", diag.inverter_temp_valid],
    ["#fanTempChip508", diag.igbt_temp_valid],
  ];
  tempChips.forEach(([id, valid]) => {
    const node = $(id);
    if (node) node.className = `fan-source-chip${diagFresh && valid ? " on" : ""}`;
  });

  // Render 0x5A8 Power Status & Arbitration
  text("#fanPowerStatusFresh", powerKnown ? dataAgeText(fan.power_status_age) : "等待数据");
  markFanCellStale("#fanPowerSupplyState", powerKnown && !powerFresh);
  text("#fanPowerSupplyState", powerKnown ? (power.power_supply_name || "未知") : "—");
  text("#fanPowerLimitReason", powerKnown ? (power.power_limit_name || "未知") : "—");
  text("#fanCurrentBudget", powerKnown && power.current_budget_a != null ? String(power.current_budget_a) : "—");
  text("#fanPredictedCurrent", powerKnown && power.predicted_current_a != null ? String(power.predicted_current_a) : "—");
  ["#fanPowerSupplyState", "#fanPowerLimitReason", "#fanCurrentBudget", "#fanPredictedCurrent"].forEach(id =>
    markFanCellStale(id, powerKnown && !powerFresh));
  // Render Fault Badges
  $$("#page-fan [data-fan-fault]").forEach(chip => {
    const bit = +chip.dataset.fanFault;
    chip.classList.toggle("on", diagFresh && !!(faults & (1 << bit)));
  });

  const diagSummaryNode = $("#fanDiagStatus");
  if (diagSummaryNode) {
    diagSummaryNode.className = "fan-diag-summary " + (!diagKnown ? "" : !diagFresh ? "data-stale" : faults !== 0 ? "bad" : "ok");
    const diagText = faults === 0 ? "上次自检全部通过" : `上次有 ${diag.fault_names.length} 项故障`;
    diagSummaryNode.textContent = !diagKnown ? "等待数据"
      : diagFresh ? (faults === 0 ? "自检全部通过 (正常)" : `${diag.fault_names.length} 项故障活动`)
        : `已过期 · ${diagText} · ${fmt(fan.diagnostic_age, 1)} s 前`;
  }

  const fanAges = [fan.status_age, fan.diagnostic_age].filter(hasDataAge);
  const fanTelemetryStale = (statusKnown && !statusFresh) || (diagKnown && !diagFresh);
  const fanDisplayAge = fanAges.length ? (fanTelemetryStale ? Math.max(...fanAges) : Math.min(...fanAges)) : null;
  text("#fanFreshTag", receiving ? `${fanTelemetryStale ? "部分已过期 · 最旧 " : ""}${fmt(fanDisplayAge, 1)} s 前` : "未收到状态帧");
  markStaleData("#fanFreshTag", fanTelemetryStale);

  // ACK stream
  const acks = fan.ack_history || [];
  // 模式命令应答码：只在收到应答后显示，避免常驻一个未使用的操作码。
  text("#fanModeStatusTag", acks.length ? (acks[0].accepted ? "已接受" : "已拒绝") : "—");
  setClass("#fanModeStatusTag", "ok", Boolean(acks.length && acks[0].accepted));
  setClass("#fanModeStatusTag", "bad", Boolean(acks.length && !acks[0].accepted));
  // 一条应答只留结论：模式/保底与输出/目标各归一组，不在窄卡片里堆成分行文字。
  $("#fanAckList").innerHTML = acks.length ? acks.slice(0, 8).map(item =>
    `<div class="event-item"><time>${item.time}</time><b>${item.opcode_name} · 序号 ${item.sequence}</b>`
    + `<p class="${item.accepted ? "ok" : "bad"}">${item.result_name} · ${item.mode_name}`
    + ` · 输出 ${item.duty_pct[0]}/${item.duty_pct[1]}% → 目标 ${item.target_pct[0]}/${item.target_pct[1]}%</p></div>`
  ).join("") : '<div class="empty-state">尚未收到应答。</div>';

  const batteryFan = snapshot.battery_fan || {};
  const batteryStatus = batteryFan.status || {};
  const batteryCalib = batteryFan.calibration || {};
  const batteryKnown = hasDataAge(batteryFan.status_age) && Object.keys(batteryStatus).length > 0;
  const batteryFresh = isFresh(batteryFan.status_age, BATTERY_FAN_STATUS_FRESH_S);
  text("#batteryFanFreshTag", batteryKnown
    ? dataAgeText(batteryFan.status_age, BATTERY_FAN_STATUS_FRESH_S) : "等待 CANB 0x5AA");
  markStaleData("#batteryFanFreshTag", batteryKnown && !batteryFresh);
  // 一个读数一个单元：原先挤在一行说明里的值各自落到自己的单元上。
  const batteryCells = [
    ["#batteryFanStatusValue", batteryStatus.actual_duty_pct],
    ["#batteryFanLimitValue", batteryStatus.active_limit_pct],
    ["#batteryFanRpmValue", batteryStatus.rpm],
  ];
  batteryCells.forEach(([id, value]) => {
    text(id, batteryKnown && value != null ? String(value) : "等待");
    markFanCellStale(id, batteryKnown && !batteryFresh);
  });
  text("#batteryFanModeValue", batteryKnown ? (batteryStatus.mode_name || "未知") : "等待");
  text("#batteryFanSourceValue", batteryKnown ? (batteryStatus.power_source_name || "未知") : "等待");
  ["#batteryFanModeValue", "#batteryFanSourceValue"].forEach(id =>
    markFanCellStale(id, batteryKnown && !batteryFresh));
  const batteryCalibKnown = hasDataAge(batteryFan.calibration_age) && Object.keys(batteryCalib).length > 0;
  const batteryCalibFresh = isFresh(batteryFan.calibration_age, BATTERY_FAN_CALIB_FRESH_S);
  if (!available) pendingBatteryFanSave = null;
  if (pendingBatteryFanSave && batteryCalibFresh
      && Number(batteryFan.calibration_generation) > pendingBatteryFanSave.generation
      && Boolean(batteryCalib.calibrated) === pendingBatteryFanSave.calibrated
      && Number(batteryCalib.chroma_cap_pct) === pendingBatteryFanSave.chroma
      && Number(batteryCalib.hv_cap_pct) === pendingBatteryFanSave.hv
      && !batteryCalib.save_pending) {
    pendingBatteryFanSave = null;
    state.dirty.batteryFanCaps = false;
  }
  text("#batteryFanCalibSourceTag", batteryFresh
    ? `供电 · ${batteryStatus.power_source_name || "未知"}`
    : batteryKnown ? "供电状态已过期" : "等待 CANB 0x5AA");
  if (batteryCalibKnown && !state.dirty.batteryFanCaps) {
    if (document.activeElement !== $("#batteryFanChromaCapInput")) {
      $("#batteryFanChromaCapInput").value = batteryCalib.chroma_cap_pct;
    }
    if (document.activeElement !== $("#batteryFanHvCapInput")) {
      $("#batteryFanHvCapInput").value = batteryCalib.hv_cap_pct;
    }
  }
  const saveNode = $("#batteryFanSaveState");
  if (saveNode) {
    saveNode.textContent = pendingBatteryFanSave
      ? (!batteryCalibKnown ? "等待 CANB 0x5AD"
        : !batteryCalibFresh ? "已过期 · 等待 0x5AD"
        : Number(batteryFan.calibration_generation) > pendingBatteryFanSave.generation
          && batteryCalib.save_pending ? "等待 Flash 保存" : "等待新 0x5AD 核对")
      : !batteryCalibKnown ? "等待 CANB 0x5AD"
      : !batteryCalibFresh ? "已过期"
      : batteryCalib.save_pending ? "等待 Flash 保存" : batteryCalib.calibrated ? "已保存" : "未保存";
    // 过期一档交给单元底色表达，这里只区分新鲜数据的三种结论。
    saveNode.className = pendingBatteryFanSave ? "warn"
      : !(batteryCalibKnown && batteryCalibFresh) ? ""
      : batteryCalib.save_pending ? "warn" : batteryCalib.calibrated ? "ok" : "";
    // 保存状态来自标定帧，与状态帧的时效分开判定。
    markFanCellStale("#batteryFanSaveState", batteryCalibKnown && !batteryCalibFresh);
  }
  const batterySession = batteryFan.calib_session || {};
  const batteryRecords = batterySession.records || [];
  const suggested = batterySession.suggested_caps || {};
  // 子标题右侧只放一句结论：细节（复核 CSV、检查硬件）留在悬浮提示与按钮上。
  text("#batteryFanCalibProgress", batterySession.status === "running"
    ? `扫频中 · 步骤 ${batterySession.current_step || 0}/${batterySession.total_steps || 0} · 已记录 ${batteryRecords.length} 点`
    : batterySession.status === "completed"
      ? ((batterySession.quality_warnings || []).length
        ? `扫频完成 · ${(batterySession.quality_warnings || []).length} 项波动记录已保留并排除出推荐`
        : suggested.chroma_cap_pct == null || suggested.hv_cap_pct == null
        ? "扫频完成，但没有同时满足转速与功率预算的有效点"
        : `扫频完成 · 建议 35W ${suggested.chroma_cap_pct}% / 70W ${suggested.hv_cap_pct}%`)
      : batterySession.status === "aborted" ? `已中止：${batterySession.abort_reason || "未知原因"}`
        : batterySession.status === "stale" ? "连接已更换，旧记录仅可导出" : "尚未运行自动扫频");
  if (batterySession.status === "completed" && !state.dirty.batteryFanCaps
      && suggested.chroma_cap_pct != null && suggested.hv_cap_pct != null) {
    $("#batteryFanChromaCapInput").value = suggested.chroma_cap_pct;
    $("#batteryFanHvCapInput").value = suggested.hv_cap_pct;
  }
  const batteryAutoRunning = batterySession.status === "running";
  const batterySource = batteryStatus.power_source;
  if (isFresh(batteryFan.status_age, BATTERY_FAN_STATUS_FRESH_S) && (batterySource === 0 || batterySource === 2)
      && batterySource !== lastBatteryCalibSource && !batteryAutoRunning) {
    $("#batteryFanAutoCurrentInput").value = batterySource === 0 ? "8" : "18";
    lastBatteryCalibSource = batterySource;
  }
  const batteryMaxCurrent = Number($("#batteryFanAutoCurrentInput")?.value);
  const batteryPdmFresh = !pdmBus.offline
    && isFresh(pdmBus.age, BATTERY_FAN_PDM_FRESH_S) && pdmValuesValid;
  const batteryPackFresh = isFresh(pack.age, BATTERY_FAN_PACK_FRESH_S);
  const batteryCurrentReady = Number.isFinite(batteryMaxCurrent)
    && (batterySource !== 0 || batteryMaxCurrent <= 8)
    && typeof pdmBus.current_a === "number" && Number.isFinite(pdmBus.current_a)
    && pdmBus.current_a <= batteryMaxCurrent;
  const batterySupplyReady = (pack.state === 3 && batterySource === 0)
    || (pack.state === 5 && batterySource === 2);
  const standbyChargeKnown = pack.state !== 3
    || isFresh(snapshot.fault?.age, BATTERY_FAN_PACK_FRESH_S);
  const standbyCharging = pack.state === 3 && standbyChargeKnown
    && snapshot.fault?.flags?.charge_mode === true;
  const batteryStartReady = available && isFresh(batteryFan.status_age, BATTERY_FAN_STATUS_FRESH_S)
    && isFresh(batteryFan.calibration_age, BATTERY_FAN_CALIB_FRESH_S)
    && batteryPdmFresh && batteryPackFresh
    && batterySupplyReady && standbyChargeKnown && !standbyCharging && pack.temperature_complete === true
    && batteryStatus.protocol_version === 1
    && batteryCurrentReady && batteryStatus.flags?.hardware_ready === true
    && !batteryStatus.flags?.stall_confirmed
    && batteryCalib.chroma_budget_w === 35 && batteryCalib.hv_budget_w === 70;
  let batteryStartHint = "";
  if (!available) batteryStartHint = "请先连接真实 CANB 500 kbit/s";
  else if (!batteryPackFresh) batteryStartHint = "等待 CANB 0x4B0 BMS 状态";
  else if (![3, 5].includes(pack.state)) batteryStartHint = "BMS 需处于待机或高压接通状态";
  else if (!standbyChargeKnown) batteryStartHint = "等待 CANB BMS 充电状态";
  else if (standbyCharging) batteryStartHint = "BMS 正在充电，待机低压不可标定";
  else if (pack.temperature_complete !== true) batteryStartHint = "BMS 温度采样不完整";
  else if (!batteryPdmFresh) batteryStartHint = "等待 PDM 低压功率数据";
  else if (!isFresh(batteryFan.status_age, BATTERY_FAN_STATUS_FRESH_S)) batteryStartHint = "等待 CANB 0x5AA 实时风扇状态";
  else if (!isFresh(batteryFan.calibration_age, BATTERY_FAN_CALIB_FRESH_S)) batteryStartHint = "等待 CANB 0x5AD 实时标定状态";
  else if (!batterySupplyReady) batteryStartHint = "供电与 BMS 状态不一致，或正在充电";
  else if (batteryStatus.protocol_version !== 1
      || batteryCalib.chroma_budget_w !== 35 || batteryCalib.hv_budget_w !== 70) {
    batteryStartHint = "电池箱风扇协议或功率预算版本不匹配";
  } else if (!batteryCurrentReady) batteryStartHint = batterySource === 0 && batteryMaxCurrent > 8
    ? "低压待机时总线电流保护最多 8 A" : "基础总线电流超过保护值或保护值无效";
  else if (batteryStatus.flags?.hardware_ready !== true) batteryStartHint = "PWM/TACH 硬件未就绪";
  else if (batteryStatus.flags?.stall_confirmed) batteryStartHint = "存在已确认停转";
  if (batterySession.status === "idle" && !batteryStartReady) {
    text("#batteryFanCalibProgress", `未就绪：${batteryStartHint}`);
  }
  const batteryProgress = $("#batteryFanCalibProgressBar");
  const batteryStep = Number(batterySession.current_step || 0);
  const batteryTotal = Number(batterySession.total_steps || 0);
  const batteryPct = batterySession.status === "completed" ? 100
    : batteryTotal > 0 ? Math.max(0, Math.min(100, Math.round(batteryStep / batteryTotal * 100))) : 0;
  batteryProgress?.setAttribute("aria-valuenow", String(batteryPct));
  if (batteryProgress?.firstElementChild) batteryProgress.firstElementChild.style.width = batteryPct + "%";
  batteryProgress?.classList.toggle("running", batteryAutoRunning);
  text("#batteryFanCalibProgressLabel", batteryAutoRunning
    ? `自动扫描中 · 步骤 ${batteryStep}/${batteryTotal} · ${batteryPct}%`
    : batterySession.status === "completed" ? "扫描完成 · 核对下方建议上限后再保存"
      : batterySession.status === "aborted" ? `已中止：${batterySession.abort_reason || "未知原因"}`
        : batterySession.status === "stale" ? "旧连接记录仅可导出"
          : batteryStartReady ? "尚未开始扫描" : `未就绪：${batteryStartHint}`);
  const batteryTable = $("#batteryFanCalibTableBody");
  if (batteryTable) {
    batteryTable.innerHTML = batteryRecords.length ? batteryRecords.map(record => `
      <tr>
        <td title="${escapeHtml(record.quality_note || "")}"><strong>#${escapeHtml(record.step)}${record.quality_ok === false ? " · 待复核" : ""}</strong></td>
        <td>${escapeHtml(record.duty_pct)}%</td><td>${escapeHtml(record.rpm)}</td>
        <td>${escapeHtml(record.voltage_v)} V</td><td>${escapeHtml(record.current_a)} A</td>
        <td>${escapeHtml(record.power_w)} W</td>
        <td class="ok"><strong>+${escapeHtml(record.delta_current_a)} A</strong></td>
        <td class="ok"><strong>+${escapeHtml(record.delta_power_w)} W</strong></td>
      </tr>`).join("")
      : `<tr><td colspan="8" class="empty-state">${batteryAutoRunning
        ? "正在测量基线并准备扫频…" : "尚未运行标定。"}</td></tr>`;
  }
  ["#batteryFanControlButton", "#batteryFanCalibButton", "#batteryFanClearButton"]
    .forEach(id => { if ($(id)) $(id).disabled = !available || batteryAutoRunning; });
  ["#sendFanControl", "#fanRestoreButton", "#sendFanProfile", "#clearFanFaults"]
    .forEach(id => { if ($(id)) $(id).disabled = !available || !profileFresh || batteryAutoRunning; });
  $("#fanQueryButton").disabled = !available || batteryAutoRunning;
  // 只根据新鲜 0x5AD 的完成状态放行提交；复位或断线后等待新状态。
  const batteryFirmwareCompleted = batteryCalibFresh && Number(batteryCalib.calib_state) === 3;
  if ($("#batteryFanCommitButton")) {
    $("#batteryFanCommitButton").disabled = !available || batteryAutoRunning || !batteryFirmwareCompleted;
    $("#batteryFanCommitButton").title = batteryFirmwareCompleted ? "" : "需先完成并停止F405标定会话";
  }
  if ($("#batteryFanAutoStartButton")) {
    $("#batteryFanAutoStartButton").disabled = !batteryStartReady || batteryAutoRunning;
    $("#batteryFanAutoStartButton").title = batteryStartReady ? "" : batteryStartHint;
  }
  if ($("#batteryFanAutoStopButton")) $("#batteryFanAutoStopButton").disabled = batterySession.status !== "running";
  ["#batteryFanExportButton", "#batteryFanExportJsonButton"].forEach(id => {
    if ($(id)) $(id).disabled = !(batterySession.export_available === true || batteryRecords.length > 0);
  });
  renderVehicleCalibration(fan, available && profileFresh, batteryAutoRunning);

}

function renderVehicleCalibration(fan, available, batteryRunning) {
  const session = fan.calib_session || {};
  const running = session.status === "running";
  const records = session.records || [];
  const ready = available && isFresh(fan.calib_status_age, 1.5)
    && fan.calib_status?.param_version === 4;
  const progress = vehicleCalibStarting ? "正在核对供电与状态…"
    : running ? (session.pause_reason ? `已暂停：${session.pause_reason}`
      : `扫描 ${session.current_step || 0}/${session.total_steps || 0} · 已记录 ${records.length} 点`)
    : session.status === "completed" ? `扫描完成 · ${records.length} 点 · 请导出结果复核功率表和起转占空比`
    : session.status === "aborted" ? `已中止：${session.abort_reason || "未知原因"}；已有数据可导出`
    : session.status === "stale" ? "连接已更换，旧记录可导出"
    : !ready ? "等待支持标定的 V4 固件状态"
    : batteryRunning ? "电池箱标定进行中" : "已就绪 · 扫频结果不自动改写运行策略";
  text("#vehicleCalibProgress", progress);
  $("#vehicleCalibStart").disabled = !ready || running || batteryRunning || vehicleCalibStarting;
  $("#vehicleCalibStop").disabled = !running;
  for (const id of ["#vehicleCalibChannel", "#vehicleCalibTier", "#vehicleCalibHold", "#vehicleCalibCurrent"])
    $(id).disabled = running || vehicleCalibStarting;
  for (const id of ["#vehicleCalibCsv", "#vehicleCalibJson"])
    $(id).disabled = !(session.export_available || records.length);
  $("#vehicleCalibRecords").innerHTML = records.length ? records.map(rec => `<tr>
    <td>${escapeHtml(rec.step)}</td><td>${rec.direction === "up" ? "上升" : "下降"}</td>
    <td>${escapeHtml(rec.duty1_pct)}% / ${escapeHtml(rec.duty2_pct)}%</td>
    <td>${[rec.rpm1, rec.rpm2, rec.rpm3].map(escapeHtml).join(" / ")}</td>
    <td>${escapeHtml(rec.current_a)} A</td><td>${escapeHtml(rec.delta_power_w)} W</td>
    <td title="${escapeHtml(rec.quality_note || "")}">${rec.quality_ok === false ? "待复核" : "有效"}</td>
  </tr>`).join("") : '<tr><td colspan="7" class="empty-state">尚无完整测点。</td></tr>';
  if (running || vehicleCalibStarting) {
    for (const id of ["#sendFanControl", "#fanRestoreButton", "#sendFanProfile", "#clearFanFaults",
      "#batteryFanControlButton", "#batteryFanCalibButton", "#batteryFanClearButton", "#batteryFanCommitButton", "#batteryFanAutoStartButton"])
      if ($(id)) $(id).disabled = true;
  }
}
