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
  ["#fanTempOffInput", "#fanTempOnInput", "#fanTempFullInput", "#fanMinDutyInput", "#fanRampUpInput",
   "#fanFallback1Input", "#fanFallback2Input", "#fanHoldInput", "#fanRampDownInput", "#fanStrategySelect"]
    .forEach(id => $(id).addEventListener("input", () => state.dirty.fan = true));
  renderFanControlFields();
}

function confirmFanCommand(name, values, title, message, destructive = false) {
  if (!fanConnectionAvailable()) return toast("请先在整车页连接 CANB（真实 PCAN）", true);
  state.pendingCommand = null;
  state.pendingIvtAction = null;
  state.pendingFanCommand = { name, values };
  const conn = state.vehicleSnapshot.connection;
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
  const snapshot = state.vehicleSnapshot || {};
  const connection = snapshot.connection || {};
  const available = fanConnectionAvailable();
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
}
