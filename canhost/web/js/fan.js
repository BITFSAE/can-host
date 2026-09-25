/* 整车风扇页面模块：FanController 遥测与配置，通过统一 CANB 连接收发。 */

let lastBatteryCalibSource = null;

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

function bindFanControls() {
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
  $("#sendFanCurve")?.addEventListener("click", () => {
    const targetCh = $("#fanCurveTargetSelect")?.value || "1";
    const cmdName = targetCh === "2" ? "fan_curve_ch2" : "fan_curve";
    const values = {
      temp_off_c: readFanNumber("#fanTempOffInput", "关闭温度"),
      temp_on_c: readFanNumber("#fanTempOnInput", "启动温度"),
      temp_full_c: readFanNumber("#fanTempFullInput", "全速温度"),
      min_duty_pct: readFanNumber("#fanMinDutyInput", "最低运行占空比"),
      ramp_up_pct_per_s: readFanNumber("#fanRampUpInput", "上升斜率"),
    };
    if (Object.values(values).some(value => value == null)) return;
    const chName = targetCh === "2" ? "回路 2 (PWM2 · 逆变器/IGBT)" : "回路 1 (PWM1 · 电机水套)";
    confirmFanCommand(cmdName, values, `写入风扇温控曲线 (${chName})`,
      `目标 ${chName}\n关闭 ${values.temp_off_c} °C · 启动 ${values.temp_on_c} °C · 全速 ${values.temp_full_c} °C\n`
      + `最低运行 ${values.min_duty_pct}% · 上升 ${values.ramp_up_pct_per_s}%/s\n策略只保存在 RAM，复位后恢复默认。`);
  });
  $("#sendFanFailsafe")?.addEventListener("click", () => {
    const values = {
      strategy: Number($("#fanStrategySelect").value),
      fallback1_duty_pct: readFanNumber("#fanFallback1Input", "PWM1保底占空比"),
      fallback2_duty_pct: readFanNumber("#fanFallback2Input", "PWM2保底占空比"),
      stale_hold_s: readFanNumber("#fanHoldInput", "失联保持时间"),
      ramp_down_pct_per_s: readFanNumber("#fanRampDownInput", "下降斜率"),
    };
    if (Object.values(values).some(value => value == null || !Number.isFinite(value))) return;
    const strategyNames = { 0: "保持最后目标", 1: "固定保底", 2: "全速" };
    confirmFanCommand("fan_failsafe", values, "写入风扇失联策略",
      `策略 ${strategyNames[values.strategy]} · 保底 ${values.fallback1_duty_pct}% / ${values.fallback2_duty_pct}%\n`
      + `保持 ${values.stale_hold_s} s · 下降 ${values.ramp_down_pct_per_s}%/s\n策略只保存在 RAM，复位后恢复默认。`);
  });
  $("#fanQueryButton")?.addEventListener("click", () => {
    confirmFanCommand("fan_query", {}, "查询风扇当前策略", "读取 FanController 的温控曲线和失联策略，结果随后回报到本页。");
  });
  $("#fanRestoreButton")?.addEventListener("click", () => {
    confirmFanCommand("fan_restore_defaults", {}, "恢复风扇默认策略",
      "恢复默认温控曲线（35/40/60℃ · 30% · 20%/s）和失联策略（固定保底 50%/50% · 保持 5s），并回到自动温控模式。", true);
  });

  // Calibration controls
  const fanCalibTierPicked = (resetCurrentLimit = true) => {
    const tier = $("#fanCalibTierSelect")?.value || "dcdc";
    if (resetCurrentLimit) {
      $("#fanCalibMaxCurrentInput").value = tier === "battery" ? "8" : "18";
    }
    return tier;
  };
  $("#fanCalibTierSelect")?.addEventListener("change", () => {
    fanCalibTierPicked();
  });
  $("#startFanCalibButton")?.addEventListener("click", () => {
    if (!fanConnectionAvailable()) return toast("请先连接 CANB", true);
    const channel = +$("#fanCalibChannelSelect").value || 1;
    const hold_s = readFanNumber("#fanCalibHoldInput", "稳态保持时间");
    const tier = fanCalibTierPicked(false);
    const max_current_a = readFanNumber("#fanCalibMaxCurrentInput", "总线电流保护");
    if (hold_s == null || max_current_a == null) return;
    const chName = channel === 2 ? "回路 2 (PWM2 · 单 2H6P)" : "回路 1 (PWM1 · 双 2H4PU)";
    // 计划 11.1：开始标定前必须由操作者逐次确认现场安全条件。
    confirmFanAction(
      "启动风扇自动扫频标定",
      `标定通道 ${chName}\n供电档位 ${tier === "battery" ? "低压电池" : "DCDC 高压"}\n稳态保持 ${hold_s} s · 总线电流保护 ${max_current_a} A\n\n`
      + "标定期间所选回路会按阶梯从 0% 扫到 100% 再扫回，风扇会高速运转。\n"
      + "任一安全条件（温度、PDM、供电、停转、电流）触发都会自动中止并恢复自动温控。",
      "我已确认车辆静止、车轮安全、风道无遮挡，且人员远离旋转部件。",
      async () => {
        const res = await pywebview.api.start_fan_calibration({ channel, hold_s, max_current_a, tier });
        if (res && res.ok) {
          state.dirty.fanCaps = false;
          toast("风扇自动扫频标定已启动");
        } else {
          toast(`启动标定失败：${res?.error || "未知原因"}`, true);
        }
      });
  });

  $("#commitFanCapsButton")?.addEventListener("click", () => {
    const battery_cap_pct = readFanNumber("#fanBatteryCapInput", "电池档上限");
    const dcdc_cap_pct = readFanNumber("#fanDcdcCapInput", "DCDC档上限");
    if (battery_cap_pct == null || dcdc_cap_pct == null
        || !(5 <= battery_cap_pct && battery_cap_pct <= dcdc_cap_pct && dcdc_cap_pct <= 100)) {
      return toast("两档上限必须满足 5% ≤ 电池档 ≤ DCDC档 ≤ 100%", true);
    }
    confirmFanCommand("fan_calib", { action: 5, battery_cap_pct, dcdc_cap_pct },
      "保存整车风扇两档上限", `低压电池 ${battery_cap_pct}% · DCDC ${dcdc_cap_pct}%\n将写入 FanController 双页 Flash。`);
  });
  $("#clearFanCapsButton")?.addEventListener("click", () => {
    confirmFanCommand("fan_calib", { action: 6 }, "清除整车风扇标定",
      "清除两档保存值并恢复未标定 15% 上限。", true);
  });

  const sendBatteryFan = (name, values, title, message, destructive = false) => {
    confirmFanAction(title, message, "我已核对电池箱风扇、供电状态和旋转部件安全。", async () => {
      const res = await pywebview.api.send_battery_fan_command(name, values, true);
      if (res?.ok && ["battery_fan_commit", "battery_fan_clear"].includes(name)) {
        state.dirty.batteryFanCaps = false;
      }
      toast(res?.ok ? (res.message || "电池箱风扇命令已执行") : `命令失败：${res?.error || "未知原因"}`, !res?.ok);
    }, destructive);
  };
  $("#batteryFanQueryButton")?.addEventListener("click", () => sendBatteryFan(
    "battery_fan_query", {}, "查询电池箱风扇", "开启 5 秒状态上报窗口。"));
  $("#batteryFanCalibQueryButton")?.addEventListener("click", () => sendBatteryFan(
    "battery_fan_query", {}, "查询电池箱风扇", "开启 5 秒状态上报窗口。"));
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
    confirmFanAction("确认电池箱风扇自动扫频", "将先查询状态，再采集0%基线并逐档计算增量功率，完成后给出35W/70W建议上限。",
      "我已确认车辆静止、供电状态稳定、风道无遮挡且人员远离旋转部件。", async () => {
        const query = await pywebview.api.send_battery_fan_command("battery_fan_query", {}, true);
        if (!query?.ok) return toast(`查询失败：${query?.error || "未知原因"}`, true);
        await new Promise(resolve => setTimeout(resolve, 700));
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
  $("#batteryFanExportButton")?.addEventListener("click", async () => {
    try {
      const res = await state.api.choose_export_battery_fan_calibration();
      if (res?.ok) toast(`标定记录已导出：${res.path}`);
      else if (!res?.cancelled) toast(res?.error || "标定记录导出失败", true);
    } catch (e) {
      toast(`导出失败：${e}`, true);
    }
  });
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
    const chroma_cap_pct = readFanNumber("#batteryFanChromaCapInput", "35W Chroma上限");
    const hv_cap_pct = readFanNumber("#batteryFanHvCapInput", "70W 高压上限");
    if (chroma_cap_pct == null || hv_cap_pct == null) return;
    if (chroma_cap_pct > hv_cap_pct) return toast("上限必须满足 35W Chroma ≤ 70W 高压", true);
    sendBatteryFan("battery_fan_commit", { chroma_cap_pct, hv_cap_pct },
      "提交电池箱风扇功率上限", `35W Chroma ${chroma_cap_pct}% · 70W 高压 ${hv_cap_pct}%\n停止有效标定会话后才能提交。`);
  });
  $("#batteryFanClearButton")?.addEventListener("click", () => sendBatteryFan(
    "battery_fan_clear", {}, "清除电池箱风扇标定", "清除保存值并恢复两档 55% 上限；只允许未上高压且非充电时执行。", true));

  $("#abortFanCalibButton")?.addEventListener("click", async () => {
    try {
      await pywebview.api.stop_fan_calibration();
      toast("已请求中止标定");
    } catch (e) {
      toast(`中止标定异常：${e}`, true);
    }
  });

  $("#exportFanCalibCsv")?.addEventListener("click", async () => {
    try {
      const res = await state.api.choose_export_fan_calibration("csv");
      if (res?.ok) toast(`CSV 已导出：${res.path}`);
      else if (!res?.cancelled) toast(res?.error || "CSV 导出失败", true);
    } catch (e) {
      toast(`导出失败：${e}`, true);
    }
  });

  $("#exportFanCalibJson")?.addEventListener("click", async () => {
    try {
      const res = await state.api.choose_export_fan_calibration("json");
      if (res?.ok) toast(`JSON 已导出：${res.path}`);
      else if (!res?.cancelled) toast(res?.error || "JSON 导出失败", true);
    } catch (e) {
      toast(`导出失败：${e}`, true);
    }
  });

  ["#fanTempOffInput", "#fanTempOnInput", "#fanTempFullInput", "#fanMinDutyInput", "#fanRampUpInput",
   "#fanFallback1Input", "#fanFallback2Input", "#fanHoldInput", "#fanRampDownInput", "#fanStrategySelect"]
    .forEach(id => $(id)?.addEventListener("input", () => state.dirty.fan = true));
  ["#fanBatteryCapInput", "#fanDcdcCapInput"]
    .forEach(id => $(id)?.addEventListener("input", () => state.dirty.fanCaps = true));
  ["#batteryFanChromaCapInput", "#batteryFanHvCapInput"]
    .forEach(id => $(id)?.addEventListener("input", () => state.dirty.batteryFanCaps = true));
  $("#batteryFanModeSelect")?.addEventListener("change", renderBatteryFanControlFields);
  $("#batteryFanCalibAction")?.addEventListener("change", renderBatteryFanCalibFields);
  fanCalibTierPicked();
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
  // 标定会话状态在前面就要使用（推荐上限区域），必须先于其声明。
  // 命令按钮的禁用状态统一在函数末尾按全部条件设置一次。
  const calibSession = (snapshot.fan || {}).calib_session || {};
  const calibStatus = calibSession.status || "idle";

  const fan = snapshot.fan || {};
  const status = fan.status || {};
  const diag = fan.diagnostic || {};
  const power = fan.power_status || {};
  const limits = fan.calib_limits || {};
  const calib = calibSession;
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
  const limitsKnown = hasDataAge(fan.calib_limits_age) && Object.keys(limits).length > 0;
  const limitsFresh = isFresh(fan.calib_limits_age, SLOW_DATA_FRESH_MAX_S);
  const fanSuggested = (calib && calib.suggested_caps) || {};
  const fanChannelCaps = (calib && calib.channel_caps) || {};
  const capsPendingText = "等待上限回报";
  let capsReportText = limitsKnown
    ? (limits.calibrated ? "已标定" : "未标定") + " · 电池 " + limits.battery_cap_pct + "% · DCDC " + limits.dcdc_cap_pct + "% · 当前 " + limits.active_cap_pct + "%" + (limits.flash_error ? " · Flash错误" : "")
      + (limitsFresh ? "" : ` · ${dataAgeText(fan.calib_limits_age, SLOW_DATA_FRESH_MAX_S)}`)
    : capsPendingText;
  if (calibStatus === "completed") {
    const suggestions = [];
    if (fanSuggested.battery_cap_pct != null) suggestions.push("电池 " + fanSuggested.battery_cap_pct + "%");
    if (fanSuggested.dcdc_cap_pct != null) suggestions.push("DCDC " + fanSuggested.dcdc_cap_pct + "%");
    const missing = [];
    const missingLoops = (tier) => ["1", "2"].filter(channel => fanChannelCaps[tier]?.[channel] == null);
    const batteryMissing = missingLoops("battery");
    const dcdcMissing = missingLoops("dcdc");
    if (fanSuggested.battery_cap_pct == null) {
      missing.push(`电池待完成回路 ${batteryMissing.join("/") || "有效数据"}`);
    }
    if (fanSuggested.dcdc_cap_pct == null) {
      missing.push(`DCDC待完成回路 ${dcdcMissing.join("/") || "有效数据"}`);
    }
    if (suggestions.length > 0 || missing.length > 0) {
      const statusParts = [];
      if (suggestions.length > 0) statusParts.push("推荐: " + suggestions.join(" / "));
      if (missing.length > 0) statusParts.push(missing.join("、"));
      capsReportText += " (" + statusParts.join("；") + ")。";
      const batteryActive = document.activeElement === $("#fanBatteryCapInput");
      const dcdcActive = document.activeElement === $("#fanDcdcCapInput");
      if (!state.dirty.fanCaps && !batteryActive && fanSuggested.battery_cap_pct != null) {
        $("#fanBatteryCapInput").value = fanSuggested.battery_cap_pct;
      }
      if (!state.dirty.fanCaps && !dcdcActive && fanSuggested.dcdc_cap_pct != null) {
        $("#fanDcdcCapInput").value = fanSuggested.dcdc_cap_pct;
      }
    }
  } else if (limitsKnown && !state.dirty.fanCaps) {
    if (document.activeElement !== $("#fanBatteryCapInput")) $("#fanBatteryCapInput").value = limits.battery_cap_pct;
    if (document.activeElement !== $("#fanDcdcCapInput")) $("#fanDcdcCapInput").value = limits.dcdc_cap_pct;
  }
  text("#fanCapsReport", capsReportText);
  markStaleData("#fanCapsReport", limitsKnown && !limitsFresh);

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

  // Policies (Curves & Failsafe)
  const curve = fan.curve || {};
  const curveKnown = hasDataAge(fan.curve_age) && Object.keys(curve).length > 0;
  const curveFresh = isFresh(fan.curve_age, SLOW_DATA_FRESH_MAX_S);
  const failsafe = fan.failsafe || {};
  const failsafeKnown = hasDataAge(fan.failsafe_age) && Object.keys(failsafe).length > 0;
  const failsafeFresh = isFresh(fan.failsafe_age, SLOW_DATA_FRESH_MAX_S);
  text("#fanCurveReport", curveKnown
    ? `${curve.temp_off_c}/${curve.temp_on_c}/${curve.temp_full_c} ℃ · ${curve.min_duty_pct}% · ${curve.ramp_up_pct_per_s}%/s${curveFresh ? "" : ` · ${dataAgeText(fan.curve_age, SLOW_DATA_FRESH_MAX_S)}`}`
    : "当前值未读取");
  text("#fanFailsafeReport", failsafeKnown
    ? `${failsafe.failsafe_name} · 保底 ${failsafe.fallback1_duty_pct}/${failsafe.fallback2_duty_pct}% · 保持 ${failsafe.stale_hold_s}s${failsafeFresh ? "" : ` · ${dataAgeText(fan.failsafe_age, SLOW_DATA_FRESH_MAX_S)}`}`
    : "当前值未读取");
  markStaleData("#fanCurveReport", curveKnown && !curveFresh);
  markStaleData("#fanFailsafeReport", failsafeKnown && !failsafeFresh);

  // Autofill form if user hasn't edited
  const fill = (id, value) => { if (document.activeElement !== $(id)) $(id).value = value; };
  if (curveKnown && !state.dirty.fan) {
    fill("#fanTempOffInput", curve.temp_off_c);
    fill("#fanTempOnInput", curve.temp_on_c);
    fill("#fanTempFullInput", curve.temp_full_c);
    fill("#fanMinDutyInput", curve.min_duty_pct);
    fill("#fanRampUpInput", curve.ramp_up_pct_per_s);
  }
  if (failsafeKnown && !state.dirty.fan) {
    fill("#fanStrategySelect", String(failsafe.failsafe));
    fill("#fanFallback1Input", failsafe.fallback1_duty_pct);
    fill("#fanFallback2Input", failsafe.fallback2_duty_pct);
    fill("#fanHoldInput", failsafe.stale_hold_s);
    fill("#fanRampDownInput", failsafe.ramp_down_pct_per_s);
  }
  if (!curveKnown && !failsafeKnown && !state.dirty.fan) {
    ["#fanTempOffInput", "#fanTempOnInput", "#fanTempFullInput", "#fanMinDutyInput", "#fanRampUpInput",
     "#fanFallback1Input", "#fanFallback2Input", "#fanHoldInput", "#fanRampDownInput"].forEach(id => { if ($(id)) $(id).value = ""; });
    $("#fanStrategySelect").value = "1";
  }

  // ACK stream
  const acks = fan.ack_history || [];
  // 模式命令应答码：只在收到应答后显示，避免常驻一个未使用的操作码。
  text("#fanModeStatusTag", acks.length ? (acks[0].accepted ? "已接受" : "已拒绝") : "—");
  setClass("#fanModeStatusTag", "ok", Boolean(acks.length && acks[0].accepted));
  setClass("#fanModeStatusTag", "bad", Boolean(acks.length && !acks[0].accepted));
  // 一条应答只留结论：模式/保底与输出/目标各归一组，不在窄卡片里堆成分行文字。
  $("#fanAckList").innerHTML = acks.length ? acks.slice(0, 8).map(item =>
    `<div class="event-item"><time>${item.time}</time><b>${item.opcode_name} · 序号 ${item.sequence}</b>`
    + `<p class="${item.accepted ? "ok" : "bad"}">${item.result_name} · ${item.mode_name} · 保底 ${item.failsafe_name}`
    + ` · 输出 ${item.duty_pct[0]}/${item.duty_pct[1]}% → 目标 ${item.target_pct[0]}/${item.target_pct[1]}%</p></div>`
  ).join("") : '<div class="empty-state">尚未收到应答。</div>';

  // Calibration session state & table
  const calibRunning = calibStatus === "running";
  const calibPaused = calibRunning && Boolean(calib.pause_reason);
  const tagMap = {
    idle: { text: "未激活", cls: "neutral" },
    // 扫频中的绿色与旁边进度条的运行色一致。
    running: calibPaused
      ? { text: `安全暂停 · 第 ${calib.recovery_count || 1} 次`, cls: "warn" }
      : { text: `扫频中 (${calib.current_step || 0}/${calib.total_steps || 0})`, cls: "ok" },
    aborted: { text: `已中止：${calib.abort_reason || "未知"}`, cls: "bad" },
    completed: { text: (calib.quality_warnings || []).length
      ? `已完成 · ${(calib.quality_warnings || []).length} 项待复核` : "已完成", cls: "ok" },
    stale: { text: "旧连接记录", cls: "neutral" },
  };
  const tagInfo = tagMap[calibStatus] || { text: calibStatus, cls: "neutral" };
  const calibTag = $("#fanCalibStateTag");
  if (calibTag) {
    if (calibTag.textContent !== tagInfo.text) calibTag.textContent = tagInfo.text;
    calibTag.className = `tag ${tagInfo.cls}`;
  }

  // 标定会话进度：状态、当前步骤、当前/目标总电流与保护值集中在一行，避免
  // 操作者只看到“扫描中”而不知道当前处于阶梯的哪一段。
  renderFanCalibProgress(calibSession, calibRunning);

  const startBtn = $("#startFanCalibButton");
  const abortBtn = $("#abortFanCalibButton");
  if (abortBtn) abortBtn.disabled = !calibRunning;
  const records = calib.records || [];
  const firmwareCalibCompleted = isFresh(fan.calib_status_age)
    && Number(fan.calib_status?.calib_state) === 3;
  const selectedTier = $("#fanCalibTierSelect")?.value || "dcdc";
  const selectedMaxCurrent = Number($("#fanCalibMaxCurrentInput")?.value);
  const firmwareTierMatches = powerFresh && Number(power.power_supply_state) === (selectedTier === "battery" ? 1 : 3);
  const dcdcMeasuredReady = selectedTier !== "dcdc" || (pdmBatteryFresh
    && Number(pdmBus.voltage_v) - Number(pdmBattery.voltage_v) >= 0.30
    && Number(pdmBattery.current_a) <= 0.50);
  const temperaturesReady = diag.motor_temp_c != null && Number.isFinite(Number(diag.motor_temp_c))
    && diag.controller_temp_c != null && Number.isFinite(Number(diag.controller_temp_c))
    && Number(diag.motor_temp_c) < 70 && Number(diag.controller_temp_c) < 65;
  const startCurrentReady = Number.isFinite(selectedMaxCurrent)
    && Number(pdmBus.current_a) <= Math.min(8, selectedMaxCurrent);
  const fanFramesFresh = isFresh(fan.status_age, 1.5)
    && isFresh(fan.diagnostic_age, 1.5) && isFresh(fan.power_status_age, 1.5)
    && isFresh(fan.calib_limits_age, 1.5);
  const fanStartReady = available && fanFramesFresh && pdmFresh
    && Number(limits.protocol_version) === 3 && firmwareTierMatches && dcdcMeasuredReady
    && startCurrentReady && faults === 0 && temperaturesReady;
  let fanStartHint = "";
  if (!available) fanStartHint = "请先连接真实 CANB 500 kbit/s";
  else if (!pdmFresh) fanStartHint = "等待 PDM 低压功率数据";
  else if (!fanFramesFresh) {
    fanStartHint = "等待风扇遥测与标定上限帧";
  } else if (Number(limits.protocol_version) !== 3) fanStartHint = "标定上限帧协议版本必须为 3";
  else if (!firmwareTierMatches) fanStartHint = "当前供电与所选标定档位不匹配";
  else if (!dcdcMeasuredReady) fanStartHint = "PDM 实测未证明 DCDC 已接管";
  else if (!startCurrentReady) fanStartHint = "基础总线电流超过开始门槛或保护值无效";
  else if (faults !== 0) fanStartHint = "请先排除风扇故障";
  else if (!temperaturesReady) fanStartHint = "温度输入无效或已达到标定门槛";
  if (startBtn) {
    startBtn.title = fanStartReady ? "" : fanStartHint;
  }
  if (calibStatus === "idle" && !fanStartReady) {
    text("#fanCalibProgressLabel", `未就绪：${fanStartHint}`);
  }
  if ($("#commitFanCapsButton")) {
    $("#commitFanCapsButton").title = firmwareCalibCompleted ? "" : "需先完成并停止固件标定会话";
  }
  if ($("#clearFanCapsButton")) $("#clearFanCapsButton").disabled = !available || calibRunning;
  const fanExportAvailable = calib.export_available === true || records.length > 0;
  if ($("#exportFanCalibCsv")) $("#exportFanCalibCsv").disabled = !fanExportAvailable;
  if ($("#exportFanCalibJson")) $("#exportFanCalibJson").disabled = !fanExportAvailable;

  const tbody = $("#fanCalibTableBody");
  if (tbody) {
    if (records.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" class="empty-state">${calibPaused
        ? `安全暂停：${calib.pause_reason}`
        : calibRunning ? "正在测量基准并准备阶梯扫频…" : "尚未运行标定"}</td></tr>`;
    } else {
      tbody.innerHTML = records.map(r => `
        <tr>
          <td title="${r.quality_note || ""}"><strong>#${r.step}${r.quality_ok === false ? " · 待复核" : ""}</strong></td>
          <td>${r.duty1_pct}%</td>
          <td>${r.duty2_pct}%</td>
          <td>${r.rpm1} / ${r.rpm2} / ${r.rpm3}</td>
          <td>${r.voltage_v} V</td>
          <td>${r.current_a} A</td>
          <td>${r.power_w} W</td>
          <td class="ok"><strong>+${r.delta_current_a} A</strong></td>
          <td class="ok"><strong>+${r.delta_power_w} W</strong></td>
        </tr>
      `).join("");
    }
  }

  const batteryFan = snapshot.battery_fan || {};
  const batteryStatus = batteryFan.status || {};
  const batteryCalib = batteryFan.calibration || {};
  const batteryKnown = hasDataAge(batteryFan.status_age) && Object.keys(batteryStatus).length > 0;
  const batteryFresh = isFresh(batteryFan.status_age, SLOW_DATA_FRESH_MAX_S);
  text("#batteryFanFreshTag", batteryKnown
    ? dataAgeText(batteryFan.status_age, SLOW_DATA_FRESH_MAX_S) : "等待查询");
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
  const batteryCalibFresh = isFresh(batteryFan.calibration_age, SLOW_DATA_FRESH_MAX_S);
  text("#batteryFanCalibSourceTag", batteryFresh
    ? `供电 · ${batteryStatus.power_source_name || "未知"}`
    : batteryKnown ? "供电状态已过期" : "等待查询");
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
    saveNode.textContent = batteryCalibKnown
      ? (batteryCalib.save_pending ? "等待 Flash 保存" : batteryCalib.calibrated ? "已保存" : "未保存")
      : "等待";
    // 过期一档交给单元底色表达，这里只区分新鲜数据的三种结论。
    saveNode.className = !(batteryCalibKnown && batteryCalibFresh) ? ""
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
  if (isFresh(batteryFan.status_age, 1.0) && (batterySource === 0 || batterySource === 2)
      && batterySource !== lastBatteryCalibSource && !batteryAutoRunning) {
    $("#batteryFanAutoCurrentInput").value = batterySource === 0 ? "8" : "18";
    lastBatteryCalibSource = batterySource;
  }
  const batteryMaxCurrent = Number($("#batteryFanAutoCurrentInput")?.value);
  const batteryCurrentReady = Number.isFinite(batteryMaxCurrent)
    && (batterySource !== 0 || batteryMaxCurrent <= 8)
    && typeof pdmBus.current_a === "number" && Number.isFinite(pdmBus.current_a)
    && pdmBus.current_a <= batteryMaxCurrent;
  const batterySupplyReady = (pack.state === 3 && batterySource === 0)
    || (pack.state === 5 && batterySource === 2);
  const standbyCharging = pack.state === 3 && isFresh(snapshot.fault?.age, 1.5)
    && snapshot.fault?.flags?.charge_mode === true;
  const batteryStartReady = available && isFresh(batteryFan.status_age, 1.0)
    && isFresh(batteryFan.calibration_age, 1.0)
    && pdmFresh && packFresh
    && batterySupplyReady && !standbyCharging && pack.temperature_complete === true
    && batteryStatus.protocol_version === 1
    && batteryCurrentReady && batteryStatus.flags?.hardware_ready === true
    && !batteryStatus.flags?.stall_confirmed
    && batteryCalib.chroma_budget_w === 35 && batteryCalib.hv_budget_w === 70;
  let batteryStartHint = "";
  if (!available) batteryStartHint = "请先连接真实 CANB 500 kbit/s";
  else if (!packFresh || ![3, 5].includes(pack.state)) batteryStartHint = "等待 BMS 待机或高压接通状态";
  else if (standbyCharging) batteryStartHint = "BMS 正在充电，待机低压不可标定";
  else if (pack.temperature_complete !== true) batteryStartHint = "BMS 温度采样不完整";
  else if (!pdmFresh) batteryStartHint = "等待 PDM 低压功率数据";
  else if (!(isFresh(batteryFan.status_age, 1.0)
      && isFresh(batteryFan.calibration_age, 1.0))) {
    batteryStartHint = "请先查询电池箱风扇状态与标定上限";
  } else if (!batterySupplyReady) batteryStartHint = "供电与 BMS 状态不一致，或正在 Chroma 充电";
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
  if ($("#commitFanCapsButton")) {
    $("#commitFanCapsButton").disabled = !available || calibRunning || batteryAutoRunning || !firmwareCalibCompleted;
  }
  ["#batteryFanQueryButton", "#batteryFanCalibQueryButton", "#batteryFanControlButton", "#batteryFanCalibButton", "#batteryFanClearButton"]
    .forEach(id => { if ($(id)) $(id).disabled = !available || batteryAutoRunning || calibRunning; });
  ["#sendFanControl", "#sendFanCurve", "#sendFanFailsafe", "#fanQueryButton", "#fanRestoreButton", "#clearFanCapsButton"]
    .forEach(id => { if ($(id)) $(id).disabled = !available || calibRunning || batteryAutoRunning; });
  // F405 保持“已完成”直到提交/清除/复位，而 0x5AD 只在查询窗口内发送；
  // 按最后已知状态判定，避免操作者复核建议值期间按钮因帧过期被禁用。
  // 状态若已变化（如复位），提交仍会被固件应答拒绝并提示。
  const batteryFirmwareCompleted = Number(batteryCalib.calib_state) === 3;
  if ($("#batteryFanCommitButton")) {
    $("#batteryFanCommitButton").disabled = !available || batteryAutoRunning || calibRunning || !batteryFirmwareCompleted;
    $("#batteryFanCommitButton").title = batteryFirmwareCompleted ? "" : "需先完成并停止F405标定会话";
  }
  if (startBtn) startBtn.disabled = !fanStartReady || calibRunning || batteryAutoRunning;
  if ($("#batteryFanAutoStartButton")) {
    $("#batteryFanAutoStartButton").disabled = !batteryStartReady || batteryAutoRunning || calibRunning;
    $("#batteryFanAutoStartButton").title = batteryStartReady ? "" : batteryStartHint;
  }
  if ($("#batteryFanAutoStopButton")) $("#batteryFanAutoStopButton").disabled = batterySession.status !== "running";
  if ($("#batteryFanExportButton")) {
    $("#batteryFanExportButton").disabled = !(batterySession.export_available === true || batteryRecords.length > 0);
  }
}

function renderFanCalibProgress(session, running) {
  const progress = $("#fanCalibProgress");
  if (!progress) return;
  const step = Number(session?.current_step || 0);
  const total = Number(session?.total_steps || 0);
  const status = session?.status || "idle";
  const pct = status === "completed" ? 100
    : total > 0 ? Math.max(0, Math.min(100, Math.round(step / total * 100))) : 0;
  progress.setAttribute("aria-valuenow", String(pct));
  const fill = progress.firstElementChild;
  if (fill) fill.style.width = pct + "%";
  progress.classList.toggle("running", running);
  const label = $("#fanCalibProgressLabel");
  if (!label) return;
  let labelText;
  if (running) {
    if (session?.pause_reason) {
      const recovered = Number(session?.recovery_count || 0);
      labelText = `安全暂停 · ${session.pause_reason} · 数据稳定后重做当前测点`
        + (recovered > 0 ? ` · 本轮第 ${recovered} 次` : "");
    } else {
      const states = { running: "自动扫描中", aborted: "已安全中止", completed: "扫描完成" };
      labelText = `${states[status] || "扫描中"} · 步骤 ${step}/${total} · ${pct}%`;
    }
  } else if (status === "aborted") {
    labelText = `已中止：${session?.abort_reason || "未知原因"}`
      + (session?.last_diagnostic_path ? ` · 诊断：${session.last_diagnostic_path}` : "");
  } else if (status === "completed") {
    const warningCount = Number(session?.quality_warnings?.length || 0);
    labelText = warningCount > 0
      ? `扫描完成 · ${warningCount} 项波动记录已保留并排除出自动推荐，请导出复核`
      : "扫描完成 · 核对下方推荐上限后再保存";
  } else {
    labelText = "尚未开始扫描";
  }
  // role=status 只在语义状态真正变化时更新，避免轮询重复播报同一句话。
  if (label.textContent !== labelText) label.textContent = labelText;
}
