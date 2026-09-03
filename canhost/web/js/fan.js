/* 整车风扇页面模块：FanController 遥测与配置，通过整车连接收发。 */

function fanConnectionAvailable() {
  return vehicleConnectionAvailable();
}

function bindFanControls() {
  $("#fanModeSelect")?.addEventListener("change", renderFanControlFields);
  $("#sendFanControl")?.addEventListener("click", () => {
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
  $("#sendFanCurve")?.addEventListener("click", () => {
    const targetCh = $("#fanCurveTargetSelect")?.value || "1";
    const cmdName = targetCh === "2" ? "fan_curve_ch2" : "fan_curve";
    const values = { temp_off_c: +$("#fanTempOffInput").value, temp_on_c: +$("#fanTempOnInput").value,
                     temp_full_c: +$("#fanTempFullInput").value, min_duty_pct: +$("#fanMinDutyInput").value,
                     ramp_up_pct_per_s: +$("#fanRampUpInput").value };
    if ([values.temp_off_c, values.temp_on_c, values.temp_full_c, values.min_duty_pct, values.ramp_up_pct_per_s].some(value => !Number.isFinite(value)))
      return toast("请完整填写温控曲线五个参数", true);
    const chName = targetCh === "2" ? "回路 2 (PWM2 · 逆变器/IGBT)" : "回路 1 (PWM1 · 电机水套)";
    confirmFanCommand(cmdName, values, `写入风扇温控曲线 (${chName})`,
      `目标 ${chName}\n关闭 ${values.temp_off_c} °C · 启动 ${values.temp_on_c} °C · 全速 ${values.temp_full_c} °C\n`
      + `最低运行 ${values.min_duty_pct}% · 上升 ${values.ramp_up_pct_per_s}%/s\n策略只保存在 RAM，复位后恢复默认。`);
  });
  $("#sendFanFailsafe")?.addEventListener("click", () => {
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
  $("#fanQueryButton")?.addEventListener("click", () => {
    confirmFanCommand("fan_query", {}, "查询风扇当前策略", "发送操作码 0x05；FanController 随后回报温控曲线 0x5A6 和失联策略 0x5A7。");
  });
  $("#fanRestoreButton")?.addEventListener("click", () => {
    confirmFanCommand("fan_restore_defaults", {}, "恢复风扇默认策略",
      "恢复默认温控曲线（35/40/60℃ · 30% · 20%/s）和失联策略（固定保底 50%/50% · 保持 5s），并回到自动温控模式。", true);
  });

  // Calibration controls
  const fanCalibTierPicked = () => {
    const tier = $("#fanCalibTierSelect")?.value || "dcdc";
    $("#fanCalibMaxCurrentInput").value = tier === "battery" ? "8" : "18";
    return tier;
  };
  $("#fanCalibTierSelect")?.addEventListener("change", () => {
    fanCalibTierPicked();
  });
  $("#startFanCalibButton")?.addEventListener("click", () => {
    if (!fanConnectionAvailable()) return toast("请先连接 CANB", true);
    const channel = +$("#fanCalibChannelSelect").value || 1;
    const hold_s = +$("#fanCalibHoldInput").value || 6;
    const tier = fanCalibTierPicked();
    const max_current_a = +$("#fanCalibMaxCurrentInput").value || (tier === "battery" ? 8 : 18);
    const chName = channel === 2 ? "回路 2 (PWM2 · 单 2H6P)" : "回路 1 (PWM1 · 双 2H4PU)";
    // 计划 11.1：开始标定前必须由操作者逐次确认现场安全条件。
    confirmFanAction(
      "启动风扇自动扫频标定",
      `标定通道 ${chName}\n供电档位 ${tier === "battery" ? "低压电池" : "DCDC 高压"}\n稳态保持 ${hold_s} s · 总线电流保护 ${max_current_a} A\n\n`
      + "标定期间该回路会按阶梯从 0% 扫到 100% 再扫回，三台风扇会高速运转。\n"
      + "任一安全条件（温度、PDM、供电、停转、电流）触发都会自动中止并恢复自动温控。",
      "我已确认车辆静止、车轮安全、风道无遮挡，且人员远离旋转部件。",
      async () => {
        const res = await pywebview.api.start_fan_calibration({ channel, hold_s, max_current_a, tier });
        if (res && res.ok) {
          toast("风扇自动扫频标定已启动");
        } else {
          toast(`启动标定失败：${res?.error || "未知原因"}`, true);
        }
      });
  });

  $("#commitFanCapsButton")?.addEventListener("click", () => {
    const battery_cap_pct = +$("#fanBatteryCapInput").value;
    const dcdc_cap_pct = +$("#fanDcdcCapInput").value;
    if (!Number.isFinite(battery_cap_pct) || !Number.isFinite(dcdc_cap_pct)
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
      toast(res?.ok ? (res.message || "电池箱风扇命令已执行") : `命令失败：${res?.error || "未知原因"}`, !res?.ok);
    }, destructive);
  };
  $("#batteryFanQueryButton")?.addEventListener("click", () => sendBatteryFan(
    "battery_fan_query", {}, "查询电池箱风扇", "开启 5 秒状态上报窗口。"));
  $("#batteryFanControlButton")?.addEventListener("click", () => {
    const mode = +$("#batteryFanModeSelect").value;
    sendBatteryFan("battery_fan_control", {
      mode, duty_pct: mode === 1 ? (+$("#batteryFanDutyInput").value || 0) : 0,
      lease_s: mode === 0 ? 0 : (+$("#batteryFanLeaseInput").value || 10),
    }, "控制电池箱风扇", `模式 ${["自动", "手动", "关闭"][mode]}；普通手动仍受当前 35W/70W 上限约束。`, mode === 2);
  });
  $("#batteryFanAutoStartButton")?.addEventListener("click", () => {
    confirmFanAction("确认电池箱风扇自动扫频", "将先查询状态，再采集0%基线并逐档计算增量功率，完成后给出35W/70W建议上限。",
      "我已确认车辆静止、高压/DCDC稳定、风道无遮挡且人员远离旋转部件。", async () => {
        const query = await pywebview.api.send_battery_fan_command("battery_fan_query", {}, true);
        if (!query?.ok) return toast(`查询失败：${query?.error || "未知原因"}`, true);
        await new Promise(resolve => setTimeout(resolve, 700));
        const res = await pywebview.api.start_battery_fan_calibration({
          hold_s: +$("#batteryFanAutoHoldInput").value || 5,
          max_current_a: +$("#batteryFanAutoCurrentInput").value || 18,
        });
        toast(res?.ok ? "电池箱风扇自动扫频已启动" : `启动失败：${res?.error || "未知原因"}`, !res?.ok);
      });
  });
  $("#batteryFanAutoStopButton")?.addEventListener("click", async () => {
    const res = await pywebview.api.stop_battery_fan_calibration();
    toast(res?.ok ? "已中止电池箱风扇标定" : `中止失败：${res?.error || "未知原因"}`, !res?.ok);
  });
  $("#batteryFanExportButton")?.addEventListener("click", async () => {
    const res = await pywebview.api.export_battery_fan_calibration();
    if (res?.ok) downloadFile(res.data, `battery_fan_calibration_${Date.now()}.csv`, "text/csv");
    else toast(`导出失败：${res?.error || "未知原因"}`, true);
  });
  $("#batteryFanCalibButton")?.addEventListener("click", () => {
    const action = +$("#batteryFanCalibAction").value;
    sendBatteryFan("battery_fan_calib", {
      action, step: +$("#batteryFanStepInput").value || 0,
      duty_pct: action === 3 ? 0 : (+$("#batteryFanCalibDutyInput").value || 0),
      lease_s: action === 3 ? 0 : (+$("#batteryFanCalibLeaseInput").value || 10),
    }, "发送电池箱风扇标定步骤", "标定会话可越过当前运行上限；离开高压、超温、停转或租约到期会立即中止。", action === 3);
  });
  $("#batteryFanCommitButton")?.addEventListener("click", () => {
    const chroma_cap_pct = +$("#batteryFanChromaCapInput").value;
    const hv_cap_pct = +$("#batteryFanHvCapInput").value;
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
      const res = await pywebview.api.export_fan_calibration("csv");
      if (res && res.ok && res.data) {
        downloadFile(res.data, `fan_calibration_${Date.now()}.csv`, "text/csv");
      } else {
        toast("无可用标定数据导出", true);
      }
    } catch (e) {
      toast(`导出失败：${e}`, true);
    }
  });

  $("#exportFanCalibJson")?.addEventListener("click", async () => {
    try {
      const res = await pywebview.api.export_fan_calibration("json");
      if (res && res.ok && res.data) {
        downloadFile(res.data, `fan_calibration_${Date.now()}.json`, "application/json");
      } else {
        toast("无可用标定数据导出", true);
      }
    } catch (e) {
      toast(`导出失败：${e}`, true);
    }
  });

  ["#fanTempOffInput", "#fanTempOnInput", "#fanTempFullInput", "#fanMinDutyInput", "#fanRampUpInput",
   "#fanFallback1Input", "#fanFallback2Input", "#fanHoldInput", "#fanRampDownInput", "#fanStrategySelect"]
    .forEach(id => $(id)?.addEventListener("input", () => state.dirty.fan = true));
  fanCalibTierPicked();
  renderFanControlFields();
}

function downloadFile(content, fileName, mimeType) {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = fileName;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
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
  const snapshot = state.vehicleSnapshot || {};
  const connection = snapshot.connection || {};
  const available = fanConnectionAvailable();
  // 标定会话状态在前面就要使用（推荐上限区域），必须先于其声明。
  const calibSession = (snapshot.fan || {}).calib_session || {};
  const calibStatus = calibSession.status || "idle";
  ["#sendFanControl", "#sendFanCurve", "#sendFanFailsafe", "#fanQueryButton", "#fanRestoreButton"]
    .forEach(id => { const node = $(id); if (node) node.disabled = !available; });

  const fan = snapshot.fan || {};
  const status = fan.status || {};
  const diag = fan.diagnostic || {};
  const power = fan.power_status || {};
  const limits = fan.calib_limits || {};
  const calib = calibSession;
  const statusFresh = isFresh(fan.status_age);
  const diagFresh = isFresh(fan.diagnostic_age);
  const powerFresh = isFresh(fan.power_status_age);
  const rpm = status.rpm || [];
  const duty = status.duty_pct || [];
  const target = diagFresh ? (diag.target_pct || []) : [];
  const faults = diagFresh ? (diag.faults || 0) : 0;
  const receiving = statusFresh || diagFresh;

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

  // Render 0x5A8 Power Status & Arbitration
  text("#fanPowerStatusFresh", powerFresh ? `0x5A8 · ${fmt(fan.power_status_age, 1)} s 前` : "未收到 0x5A8");
  text("#fanPowerSupplyState", powerFresh ? (power.power_supply_name || "未知") : "—");
  text("#fanPowerLimitReason", powerFresh ? (power.power_limit_name || "未知") : "—");
  text("#fanCurrentBudget", powerFresh && power.current_budget_a != null ? `${power.current_budget_a} A` : "—");
  text("#fanPredictedCurrent", powerFresh && power.predicted_current_a != null ? `${power.predicted_current_a} A` : "—");
  const limitsFresh = isFresh(fan.calib_limits_age, SLOW_DATA_FRESH_MAX_S);
  const fanSuggested = (calib && calib.suggested_caps) || {};
  let capsReportText = limitsFresh
    ? "0x5AE · " + (limits.calibrated ? "已标定" : "未标定") + " · 电池 " + limits.battery_cap_pct + "% · DCDC " + limits.dcdc_cap_pct + "% · 当前 " + limits.active_cap_pct + "%" + (limits.flash_error ? " · Flash错误" : "")
    : "未收到 0x5AE；保存前应先分别完成两档扫频。";
  if (calibStatus === "completed") {
    const suggestions = [];
    if (fanSuggested.battery_cap_pct != null) suggestions.push("电池 " + fanSuggested.battery_cap_pct + "%");
    if (fanSuggested.dcdc_cap_pct != null) suggestions.push("DCDC " + fanSuggested.dcdc_cap_pct + "%");
    const missing = [];
    if (fanSuggested.battery_cap_pct == null) missing.push("电池待标定");
    if (fanSuggested.dcdc_cap_pct == null) missing.push("DCDC待标定");
    if (suggestions.length > 0 || missing.length > 0) {
      const statusParts = [];
      if (suggestions.length > 0) statusParts.push("推荐: " + suggestions.join(" / "));
      if (missing.length > 0) statusParts.push(missing.join("、"));
      capsReportText += " (" + statusParts.join("；") + ")。";
      const batteryActive = document.activeElement === $("#fanBatteryCapInput");
      const dcdcActive = document.activeElement === $("#fanDcdcCapInput");
      if (!batteryActive && fanSuggested.battery_cap_pct != null) {
        $("#fanBatteryCapInput").value = fanSuggested.battery_cap_pct;
      }
      if (!dcdcActive && fanSuggested.dcdc_cap_pct != null) {
        $("#fanDcdcCapInput").value = fanSuggested.dcdc_cap_pct;
      }
    }
  } else if (limitsFresh) {
    if (document.activeElement !== $("#fanBatteryCapInput")) $("#fanBatteryCapInput").value = limits.battery_cap_pct;
    if (document.activeElement !== $("#fanDcdcCapInput")) $("#fanDcdcCapInput").value = limits.dcdc_cap_pct;
  }
  text("#fanCapsReport", capsReportText);

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
  const acks = fan.ack_history || [];
  $("#fanAckList").innerHTML = acks.length ? acks.slice(0, 8).map(item =>
    `<div class="event-item"><time>${item.time}</time><b>${item.opcode_name} · 序号 ${item.sequence}</b>`
    + `<p class="${item.accepted ? "ok" : "bad"}">${item.result_name} · 模式 ${item.mode_name} · 失联 ${item.failsafe_name}`
    + ` · 输出 ${item.duty_pct[0]}/${item.duty_pct[1]}% · 目标 ${item.target_pct[0]}/${item.target_pct[1]}%</p></div>`
  ).join("") : '<div class="empty-state">尚未收到 0x5A5 应答。</div>';

  // Calibration session state & table
  const calibRunning = calibStatus === "running";
  const tagMap = {
    idle: { text: "未激活", cls: "neutral" },
    running: { text: `扫频中 (${calib.current_step || 0}/${calib.total_steps || 0})`, cls: "active" },
    aborted: { text: `已中止: ${calib.abort_reason || "未知"}`, cls: "bad" },
    completed: { text: "已完成", cls: "ok" },
  };
  const tagInfo = tagMap[calibStatus] || { text: calibStatus, cls: "neutral" };
  const calibTag = $("#fanCalibStateTag");
  if (calibTag) {
    calibTag.textContent = tagInfo.text;
    calibTag.className = `tag ${tagInfo.cls}`;
  }

  // 标定会话进度：状态、当前步骤、当前/目标总电流与保护值集中在一行，避免
  // 操作者只看到“扫描中”而不知道当前处于阶梯的哪一段。
  renderFanCalibProgress(calibSession, calibRunning);

  const startBtn = $("#startFanCalibButton");
  if (startBtn) startBtn.disabled = !available || calibRunning;
  const abortBtn = $("#abortFanCalibButton");
  if (abortBtn) abortBtn.disabled = !calibRunning;

  const records = calib.records || [];
  const tbody = $("#fanCalibTableBody");
  if (tbody) {
    if (records.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" class="empty-state">${calibRunning ? "正在测量基准并准备阶梯扫频..." : "尚未运行标定。"}</td></tr>`;
    } else {
      tbody.innerHTML = records.map(r => `
        <tr>
          <td><strong>#${r.step}</strong></td>
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
  const batteryFresh = isFresh(batteryFan.status_age, SLOW_DATA_FRESH_MAX_S);
  text("#batteryFanFreshTag", batteryFresh ? `0x5AA · ${fmt(batteryFan.status_age, 1)} s 前` : "等待查询");
  const batteryNote = $("#batteryFanStatusText");
  if (batteryNote) batteryNote.className = batteryFresh ? "fan-inline-note is-active" : "fan-inline-note";
  text("#batteryFanStatusText", batteryFresh
    ? `${batteryStatus.mode_name} · ${batteryStatus.power_source_name} · ${batteryStatus.actual_duty_pct}% / 上限 ${batteryStatus.active_limit_pct}% · ${batteryStatus.rpm} RPM`
    : "发送查询后，F405 在限时窗口内回报状态。");
  const batteryValue = $("#batteryFanStatusValue");
  if (batteryValue) batteryValue.textContent = batteryFresh ? String(batteryStatus.actual_duty_pct) : "等待";
  const batteryCalibFresh = isFresh(batteryFan.calibration_age, SLOW_DATA_FRESH_MAX_S);
  if (batteryCalibFresh) {
    $("#batteryFanChromaCapInput").value = batteryCalib.chroma_cap_pct;
    $("#batteryFanHvCapInput").value = batteryCalib.hv_cap_pct;
  }
  const saveNode = $("#batteryFanSaveState");
  if (saveNode) {
    saveNode.textContent = batteryCalibFresh
      ? batteryCalib.save_pending ? "等待 Flash 保存" : batteryCalib.calibrated ? "已保存" : "未保存"
      : "等待回报";
    saveNode.className = "battery-fan-save-state"
      + (batteryCalibFresh && batteryCalib.save_pending ? " warn" : batteryCalibFresh && batteryCalib.calibrated ? " ok" : "");
  }
  const batterySession = batteryFan.calib_session || {};
  const batteryRecords = batterySession.records || [];
  const suggested = batterySession.suggested_caps || {};
  text("#batteryFanCalibProgress", batterySession.status === "running"
    ? `自动扫频中：步骤 ${batterySession.current_step || 0}，已记录 ${batteryRecords.length} 点`
    : batterySession.status === "completed"
      ? `自动扫频完成：建议35W ${suggested.chroma_cap_pct}% / 70W ${suggested.hv_cap_pct}%；复核CSV后再提交。`
      : batterySession.status === "aborted" ? `已中止：${batterySession.abort_reason || "未知原因"}` : "尚未运行自动扫频。");
  if (batterySession.status === "completed") {
    $("#batteryFanChromaCapInput").value = suggested.chroma_cap_pct;
    $("#batteryFanHvCapInput").value = suggested.hv_cap_pct;
  }
  ["#batteryFanQueryButton", "#batteryFanControlButton", "#batteryFanCalibButton", "#batteryFanCommitButton", "#batteryFanClearButton",
   "#batteryFanAutoStartButton", "#batteryFanAutoStopButton", "#batteryFanExportButton",
   "#commitFanCapsButton", "#clearFanCapsButton"].forEach(id => { if ($(id)) $(id).disabled = !available; });
  if ($("#batteryFanAutoStartButton")) $("#batteryFanAutoStartButton").disabled = !available || batterySession.status === "running";
  if ($("#batteryFanAutoStopButton")) $("#batteryFanAutoStopButton").disabled = batterySession.status !== "running";
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
  if (running) {
    const states = { running: "自动扫描中", aborted: "已安全中止", completed: "扫描完成" };
    label.textContent = `${states[status] || "扫描中"} · 步骤 ${step}/${total} · ${pct}%`;
  } else if (status === "aborted") {
    label.textContent = `已中止：${session?.abort_reason || "未知原因"}`;
  } else if (status === "completed") {
    label.textContent = "扫描完成 · 核对下方推荐上限后再保存";
  } else {
    label.textContent = "尚未开始扫描";
  }
}
