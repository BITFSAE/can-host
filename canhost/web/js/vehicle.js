/* 整车页面模块：CANB 整车连接 + 整车总览（SOP/赛会能量计/PDM/ECU/胎温/趋势）。 */

const VEH_IVT_CHANNELS = [
  { key: "current_a", label: "电流", unit: "A", digits: 1 },
  { key: "u1_v", label: "U1 电池侧总压", unit: "V", digits: 1 },
  { key: "u2_v", label: "U2 逆变器侧", unit: "V", digits: 1 },
  { key: "u3_v", label: "U3 预留", unit: "V", digits: 1 },
  { key: "temperature_c", label: "IVT 温度", unit: "°C", digits: 1 },
  { key: "power_w", label: "功率", unit: "W", digits: 0 },
  { key: "charge_as", label: "电荷计数", unit: "As", digits: 0 },
  { key: "energy_wh", label: "能量计数", unit: "Wh", digits: 1 },
];

const VEH_METER_CHANNELS = [
  { key: "current_a", label: "电流", unit: "A", digits: 1 },
  { key: "u1_v", label: "U1 逆变器侧总压", unit: "V", digits: 1 },
  { key: "power_w", label: "功率", unit: "W", digits: 0, optional: true },
  { key: "energy_wh", label: "能量", unit: "Wh", digits: 1, optional: true },
];

const VEH_STATE_NAMES = { 2: "自检", 3: "待机", 4: "预充", 5: "高压接通", 7: "故障保持" };
const VEH_INTERVENTION_NAMES = { 0: "正常", 1: "降额", 2: "过流归零", 3: "故障" };

function vehicleConnectionAvailable() {
  const connection = state.vehicleSnapshot?.connection;
  return connection?.connected === true
    && ["canb", "canb_legacy"].includes(connection.bus_profile);
}

function bindVehicleControls() {
  $("#vehicleConnectBitrate")?.addEventListener("change", updateVehicleDialog);
}

function populateVehicleOptions() {
  const select = $("#vehicleConnectBitrate");
  if (!select) return;
  updateVehicleDialog();
}

function updateVehicleDialog() {
  const select = $("#vehicleConnectBitrate");
  if (!select) return;
  const isSim = select.value === "simulation";
  $("#vehicleChannelField")?.classList.toggle("hidden", isSim);
}

async function connectVehicle() {
  if (!state.api) return toast("应用后端未就绪", true);
  const bitrateSelect = $("#vehicleConnectBitrate");
  const bitrateRaw = bitrateSelect?.value || "500000";
  const simulation = bitrateRaw === "simulation";
  const bitrate = simulation ? 500000 : Number(bitrateRaw);
  const channelSelect = $("#vehicleConnectChannel");
  if (!simulation && !channelSelect?.value) {
    return toast("未检测到可选择的 PCAN 通道；请连接设备后刷新", true);
  }
  if (!simulation && state.snapshot?.connection?.connected === true
      && state.snapshot.connection.mode === "simulation") {
    await state.api.disconnect_can();
    state.snapshot = await state.api.get_snapshot();
  }
  state.busMismatchPrompted.vehicle = null;
  setBusConnecting("canb_vehicle", true);
  let result;
  try {
    result = await state.api.connect_vehicle({
      mode: simulation ? "simulation" : "pcan",
      channel: simulation ? null : channelSelect?.value,
      bitrate,
      bus_profile: simulation || bitrate === 500000 ? "canb" : "canb_legacy",
      auto_record: !simulation && (typeof monitorAutoRecordEnabled === "function" ? monitorAutoRecordEnabled() : true),
    });
  } catch (error) {
    return toast(`整车连接失败：${error}`, true);
  } finally {
    setBusConnecting("canb_vehicle", false);
  }
  if (!result?.ok) return toast(result?.error || "整车连接失败", true);
  toast(result.warning || (simulation ? "整车模拟数据已启动（CANB）" : `整车连接已建立 · CANB ${bitrate / 1000} kbit/s`), !!result.warning);
  if (state.api.get_vehicle_snapshot) state.vehicleSnapshot = await state.api.get_vehicle_snapshot();
  // Select the new stream for a later monitor visit without navigating away
  // from the page where the connection was requested.
  if (!simulation) state.frameSource = "vehicle";
  await poll();
}

async function disconnectVehicle() {
  if (!state.api) return;
  state.busMismatchPrompted.vehicle = null;
  setBusConnecting("canb_vehicle", true);
  try { await state.api.disconnect_vehicle(); }
  catch (error) { return toast(`整车断开失败：${error}`, true); }
  finally { setBusConnecting("canb_vehicle", false); }
  state.vehicleSnapshot = null;
  toast("整车 CANB 已断开");
  await poll();
}

/** Build the channel grids, ECU table, and tyre grid once; polls only patch values. */
function buildVehicleStatics() {
  const channelCell = channel =>
    `<div class="veh-channel" data-channel="${channel.key}"><span>${channel.label}</span>`
    + `<b>等待数据</b><em>${channel.unit}</em><small class="veh-channel-state">未收到</small></div>`;
  $("#vehMeterGrid").innerHTML = VEH_METER_CHANNELS.map(channelCell).join("");

  const wheelRows = ["FL", "FR", "RL", "RR"].map((wheel, index) =>
    `<div class="veh-ecu-row" data-wheel="${index}"><b>${wheel}</b>`
    + `<span data-field="torque_pct">—</span><span data-field="velocity_rpm">—</span>`
    + `<span data-field="motor_temp_c">—</span><span data-field="inverter_temp_c">—</span>`
    + `<span data-field="igbt_temp_c">—</span></div>`).join("");
  $("#vehEcuTable").innerHTML = `<div class="veh-ecu-head"><b>轮位</b><span>扭矩 %Mn</span><span>转速 rpm</span>`
    + `<span>电机 °C</span><span>逆变器 °C</span><span>IGBT °C</span></div>` + wheelRows;

  const tireGroups = ["0x071", "0x072", "0x073", "0x074"].map((frameId, group) =>
    `<div class="veh-tire-group" data-frame="${frameId}"><div class="sub-head veh-tire-head">`
    + `<b>${frameId} · 测点 ${group * 4 + 1}–${group * 4 + 4}</b><span class="veh-tire-state">未收到</span></div>`
    + `<div class="readout-grid cols-2 veh-tire-points">` + [0, 1, 2, 3].map(point =>
      `<div class="veh-tire-point" data-point="${point}"><small>${group * 4 + point + 1}</small><b>—</b><em>°C</em></div>`).join("")
    + `</div></div>`).join("");
  $("#vehTireGrid").innerHTML = tireGroups;
}

function setVehChannel(gridId, channels, data, freshLimit) {
  const grid = $(gridId);
  channels.forEach(channel => {
    const node = grid.querySelector(`[data-channel="${channel.key}"]`);
    if (!node) return;
    const entry = data?.[channel.key] || {};
    const known = hasDataAge(entry.age);
    const fresh = isFresh(entry.age, freshLimit);
    const stale = isStaleData(entry.age, freshLimit);
    const value = known && entry.value != null ? fmt(entry.value, channel.digits) : "等待数据";
    node.querySelector("b").textContent = channel.optional && !known ? "未发送" : value;
    const stateNode = node.querySelector(".veh-channel-state");
    const status = entry.status;
    stateNode.textContent = !known ? "未收到" : stale
      ? `${dataAgeText(entry.age, freshLimit)}${status === 0 ? "" : " · 上次结果异常"}`
      : status === 0 ? `${fmt(entry.age, 1)} s 前` : "结果异常";
    stateNode.className = `veh-channel-state${fresh ? (status === 0 ? " ok" : " bad") : stale ? " data-stale" : ""}`;
    node.classList.toggle("stale", stale);
  });
}

function renderVehicle() {
  const snapshot = state.vehicleSnapshot || {};
  const connection = snapshot.connection || {};
  const available = vehicleConnectionAvailable();
  const simulation = connection.mode === "simulation";
  const currentBitrate = Number(connection.bitrate || 0);
  const bitrateSelect = $("#vehicleConnectBitrate");
  if (bitrateSelect && document.activeElement !== bitrateSelect) {
    const targetValue = simulation ? "simulation" : String(currentBitrate);
    const match = [...bitrateSelect.options].find(option => option.value === targetValue);
    if (match) bitrateSelect.value = match.value;
  }
  // -- SOP ---------------------------------------------------------------
  const sop = snapshot.sop || {};
  const limits = sop.limits || {};
  const sopStatus = sop.status || {};
  const limitsKnown = hasDataAge(sop.limits_age) && Object.keys(limits).length > 0;
  text("#vehSopDisA", limitsKnown ? fmt(limits.discharge_current_a, 1) : "等待数据");
  text("#vehSopChgA", limitsKnown ? fmt(limits.charge_current_a, 1) : "等待数据");
  text("#vehSopDisKw", limitsKnown ? fmt(limits.discharge_power_kw, 1) : "等待数据");
  text("#vehSopChgKw", limitsKnown ? fmt(limits.charge_power_kw, 1) : "等待数据");
  const statusKnown = hasDataAge(sop.status_age) && Object.keys(sopStatus).length > 0;
  const statusFresh = isFresh(sop.status_age);
  const sopAges = [sop.limits_age, sop.status_age].filter(hasDataAge);
  const sopStale = isStaleData(sop.limits_age) || isStaleData(sop.status_age);
  const sopDisplayAge = sopAges.length ? (sopStale ? Math.max(...sopAges) : Math.min(...sopAges)) : null;
  text("#vehSopAge", sopAges.length
    ? `${sopStale ? "部分已过期 · 最旧 " : ""}${fmt(sopDisplayAge, 1)} s 前`
    : "等待数据");
  markStaleData("#vehSopAge", sopStale);
  const flagText = value => statusKnown ? (value ? "是" : "否") : "—";
  text("#vehSopLimitsValid", flagText(sopStatus.limits_valid));
  text("#vehSopDrive", flagText(sopStatus.drive_allowed));
  text("#vehSopRegen", flagText(sopStatus.regen_allowed));
  text("#vehSopIntervention", statusKnown ? VEH_INTERVENTION_NAMES[sopStatus.intervention_level] ?? "—" : "—");
  text("#vehSopCrc", statusKnown ? (sopStatus.crc_valid ? "通过" : "失败") : "—");
  setClass("#vehSopCrc", "bad", statusFresh && sopStatus.crc_valid === false);
  setClass("#vehSopCrc", "ok", statusFresh && sopStatus.crc_valid === true);
  markStaleData("#vehSopCrc", statusKnown && !statusFresh);
  text("#vehSopBmsState", statusKnown ? VEH_STATE_NAMES[sopStatus.bms_state] ?? sopStatus.bms_state ?? "—" : "—");
  ["#vehSopLimitsValid", "#vehSopDrive", "#vehSopRegen", "#vehSopIntervention", "#vehSopBmsState"]
    .forEach(id => markStaleData(id, statusKnown && !statusFresh));
  const ack = sop.ecu_ack || {};
  const ackKnown = hasDataAge(sop.ecu_ack_age) && Object.keys(ack).length > 0;
  const ackFresh = isFresh(sop.ecu_ack_age);
  const ackStateBase = ack.pair_valid && ack.limits_applied ? "已采用新限值"
    : ack.ecu_fault ? "ECU 故障" : ack.pair_valid ? "校验通过 · 未确认采用" : "校验未通过";
  const ackState = !ackKnown ? "等待数据"
    : ackFresh ? ackStateBase : `已过期 · ${ackStateBase}`;
  text("#vehEcuAckState", ackState);
  setClass("#vehEcuAckState", "ok", ackFresh && ack.pair_valid && ack.limits_applied);
  setClass("#vehEcuAckState", "bad", ackFresh && (ack.ecu_fault || (ack.pair_valid === false)));
  markStaleData("#vehEcuAckState", ackKnown && !ackFresh);
  text("#vehEcuPowers", ackKnown ? `${fmt(ack.discharge_power_kw, 1)} / ${fmt(ack.regen_power_kw, 1)} kW` : "—");
  text("#vehEcuMeta", ackKnown ? `序号 ${ack.sequence ?? "—"} · 来源 ${ack.limit_source ?? "—"} · ${dataAgeText(sop.ecu_ack_age)}` : "—");
  markStaleData("#vehEcuPowers", ackKnown && !ackFresh);
  markStaleData("#vehEcuMeta", ackKnown && !ackFresh);

  // -- BMS mirror ---------------------------------------------------------
  const pack = snapshot.pack || {};
  const packKnown = hasDataAge(pack.age);
  const packFresh = isFresh(pack.age);
  text("#vehPackV", packKnown && pack.voltage_valid ? fmt(pack.voltage_v, 1) : "等待数据");
  text("#vehPackI", packKnown && pack.current_valid ? fmt(pack.current_a, 1) : "等待数据");
  text("#vehPackSoc", packKnown && pack.soc_valid ? fmt(pack.soc_pct, 0) : "等待数据");
  const packStateNode = $("#vehPackState");
  text("#vehPackState", packKnown ? VEH_STATE_NAMES[pack.state] ?? pack.state ?? "—" : "等待数据");
  packStateNode.className = `veh-state-text ${packFresh ? (pack.state === 7 ? "bad" : pack.state === 5 ? "ok" : "") : ""}`;
  markStaleData(packStateNode, packKnown && !packFresh);
  text("#vehPackAge", packKnown ? dataAgeText(pack.age) : "等待数据");
  markStaleData("#vehPackAge", packKnown && !packFresh);
  const fault = snapshot.fault || {};
  const faultKnown = fault.received === true && hasDataAge(fault.age);
  const faultFresh = faultKnown && isFresh(fault.age);
  text("#vehPackAlarm", faultKnown ? fault.alarm_level_name || "—" : "—");
  text("#vehFaultCode", faultKnown ? `${fault.code_hex}${faultFresh ? "" : ` · ${fmt(fault.age, 1)} s 前`}` : "等待数据");
  markStaleData("#vehPackAlarm", faultKnown && !faultFresh);
  markStaleData("#vehFaultCode", faultKnown && !faultFresh);

  // -- Competition meter ---------------------------------------------------
  setVehChannel("#vehMeterGrid", VEH_METER_CHANNELS, snapshot.meter, SLOW_DATA_FRESH_MAX_S);
  const meterAges = VEH_METER_CHANNELS.map(channel => snapshot.meter?.[channel.key]?.age).filter(hasDataAge);
  const meterStale = meterAges.some(ageValue => isStaleData(ageValue, SLOW_DATA_FRESH_MAX_S));
  // 帧来源写在标题的悬浮提示里，这里只报告数据本身是否可用。
  text("#vehMeterNote", meterAges.length
    ? meterStale ? `部分已过期 · 最旧 ${fmt(Math.max(...meterAges), 1)} s 前` : `${fmt(Math.min(...meterAges), 1)} s 前`
    : "等待数据");
  markStaleData("#vehMeterNote", meterStale);

  // -- PDM -----------------------------------------------------------------
  const pdm = snapshot.pdm || {};
  const renderPdmSide = (side, prefix) => {
    const entry = pdm[side] || {};
    const received = hasDataAge(entry.age);
    const known = received && !entry.offline;
    const stale = received && isStaleData(entry.age, SLOW_DATA_FRESH_MAX_S);
    text(`#${prefix}V`, known ? fmt(entry.voltage_v, 1) : "等待数据");
    text(`#${prefix}I`, known ? fmt(entry.current_a, 1) : "等待数据");
    text(`#${prefix}P`, known ? fmt(entry.power_w, 0) : "等待数据");
    text(`#${prefix}Wh`, known ? fmt(entry.energy_wh, 2) : "等待数据");
    const stateNode = $(`#${prefix}State`);
    if (stateNode) {
      const label = entry.offline
        ? stale ? `${dataAgeText(entry.age, SLOW_DATA_FRESH_MAX_S)} · 上次 INA226 离线` : "INA226 离线"
        : known ? dataAgeText(entry.age, SLOW_DATA_FRESH_MAX_S) : "等待数据";
      stateNode.textContent = label;
      stateNode.className = `state-text${stale ? " data-stale" : entry.offline ? " bad" : ""}`;
    }
    const sideNode = stateNode?.closest(".vehicle-pdm-side");
    if (sideNode) sideNode.classList.toggle("data-stale", stale);
  };
  renderPdmSide("bus", "vehPdmBus");
  renderPdmSide("battery", "vehPdmBat");
  const pdmAges = ["bus", "battery"].map(side => pdm[side]?.age).filter(hasDataAge);
  const pdmStale = pdmAges.some(ageValue => isStaleData(ageValue, SLOW_DATA_FRESH_MAX_S));
  text("#vehPdmNote", pdmAges.length
    ? pdmStale ? "部分数据已过期" : `${fmt(Math.min(...pdmAges), 1)} s 前`
    : "等待数据");
  markStaleData("#vehPdmNote", pdmStale);

  // -- ECU ------------------------------------------------------------------
  const ecu = snapshot.ecu || {};
  const ecuAges = Object.values(ecu.age || {}).filter(hasDataAge);
  const ecuStale = ecuAges.some(ageValue => isStaleData(ageValue));
  $$("#vehEcuTable .veh-ecu-row").forEach(row => {
    const index = +row.dataset.wheel;
    ["torque_pct", "velocity_rpm", "motor_temp_c", "inverter_temp_c", "igbt_temp_c"].forEach(field => {
      const node = row.querySelector(`[data-field="${field}"]`);
      const fieldAge = ecu.age?.[ecuAgeKey(field)];
      const known = hasDataAge(fieldAge);
      const values = ecu[field] || [];
      node.textContent = known && values[index] != null ? fmt(values[index], field === "velocity_rpm" ? 0 : 1) : "等待数据";
      markStaleData(node, isStaleData(fieldAge));
    });
  });
  const ecuStatus = ecu.status || {};
  const statusKnownEcu = hasDataAge(ecu.age?.status) && Object.keys(ecuStatus).length > 0;
  const statusFreshEcu = isFresh(ecu.age?.status);
  const wheelFlagText = flags => ["FR", "FL", "RR", "RL"].map(wheel => `${wheel}${flags?.[wheel] ? "✓" : "—"}`).join(" ");
  text("#vehEcuReady", statusKnownEcu ? wheelFlagText(ecuStatus.system_ready) : "—");
  text("#vehEcuEnable", statusKnownEcu ? wheelFlagText(ecuStatus.enable) : "—");
  text("#vehEcuError", statusKnownEcu ? wheelFlagText(ecuStatus.error) : "—");
  setClass("#vehEcuError", "bad", statusFreshEcu && Object.values(ecuStatus.error || {}).some(Boolean));
  ["#vehEcuReady", "#vehEcuEnable", "#vehEcuError"].forEach(id => markStaleData(id, statusKnownEcu && !statusFreshEcu));
  text("#vehEcuNote", !ecuAges.length ? "等待数据"
    : ecuStale ? `部分已过期 · 最旧 ${fmt(Math.max(...ecuAges), 1)} s 前`
      : `${fmt(Math.min(...ecuAges), 1)} s 前`);
  markStaleData("#vehEcuNote", ecuStale);

  // -- Tyres ------------------------------------------------------------------
  const tires = snapshot.tires || {};
  const tireFresh = isFresh(tires.age, SLOW_DATA_FRESH_MAX_S);
  const tireKnown = hasDataAge(tires.age);
  $$("#vehTireGrid .veh-tire-group").forEach(group => {
    const frameId = group.dataset.frame;
    const values = tires[frameId];
    const stateNode = group.querySelector(".veh-tire-state");
    if (!Array.isArray(values)) {
      stateNode.textContent = "未收到";
      stateNode.className = "veh-tire-state";
      group.classList.remove("data-stale");
      group.querySelectorAll(".veh-tire-point b").forEach(node => node.textContent = "—");
      return;
    }
    stateNode.textContent = dataAgeText(tires.age, SLOW_DATA_FRESH_MAX_S);
    stateNode.className = `veh-tire-state${tireFresh ? " ok" : " data-stale"}`;
    group.classList.toggle("data-stale", !tireFresh);
    group.querySelectorAll(".veh-tire-point").forEach(point => {
      const value = values[+point.dataset.point];
      point.querySelector("b").textContent = value == null ? "—" : fmt(value, 2);
    });
  });
  text("#vehTireNote", tireKnown
    ? dataAgeText(tires.age, SLOW_DATA_FRESH_MAX_S)
    : "等待数据");
  markStaleData("#vehTireNote", tireKnown && !tireFresh);

  drawVehicleTrend();
}

function ecuAgeKey(field) {
  return { torque_pct: "torque", velocity_rpm: "velocity", motor_temp_c: "motor_temp",
           inverter_temp_c: "inverter_temp", igbt_temp_c: "igbt_temp" }[field];
}

function vehicleTrendColors() {
  return {
    hv: cssVar("--chart-voltage", "#aeb6b2"),
    lv: cssVar("--chart-current", "#f0b429"),
  };
}

function drawVehicleTrend() {
  const canvas = $("#vehicleTrendCanvas"), trends = state.vehicleSnapshot?.trends || [];
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
  text("#vehTrendHvV", Number.isFinite(latest.hv_voltage) ? `${fmt(latest.hv_voltage, 1)} V · ${Number.isFinite(latest.hv_current) ? fmt(latest.hv_current, 1) + " A" : "—"}` : "等待数据");
  text("#vehTrendLvV", Number.isFinite(latest.lv_voltage) ? `${fmt(latest.lv_voltage, 1)} V · ${Number.isFinite(latest.lv_current) ? fmt(latest.lv_current, 1) + " A" : "—"}` : "等待数据");

  const pad = { left: 52, right: 52, top: 12, bottom: 24 };
  const plotWidth = width - pad.left - pad.right, plotHeight = height - pad.top - pad.bottom;
  const axisFont = '10px "SF Mono", "Cascadia Mono", Consolas, monospace';
  const colors = vehicleTrendColors();
  const labelColor = cssVar("--chart-label", "#7c7f7d");
  const gridColor = cssVar("--chart-grid", "#2d2f31");

  const left = trendAxisRange(trends.map(item => item.hv_voltage).filter(Number.isFinite), 8);
  const right = trendAxisRange(trends.map(item => item.lv_voltage).filter(Number.isFinite), 2);
  ctx.font = axisFont;
  for (let row = 0; row <= 4; row++) {
    const y = Math.round(pad.top + plotHeight * row / 4) + .5;
    ctx.strokeStyle = gridColor; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
    if (trends.length >= 2) {
      ctx.fillStyle = labelColor;
      if (left) {
        ctx.textAlign = "right";
        ctx.fillText(`${(left.max - (left.max - left.min) * row / 4).toFixed(1)}`, pad.left - 7, y + 3);
      }
      if (right) {
        ctx.textAlign = "left";
        ctx.fillText(`${(right.max - (right.max - right.min) * row / 4).toFixed(1)}`, width - pad.right + 7, y + 3);
      }
    }
  }
  ctx.fillStyle = labelColor; ctx.textAlign = "left"; ctx.font = axisFont;
  ctx.fillText("HV V", 2, pad.top + 3);
  ctx.textAlign = "right"; ctx.fillText("LV V", width - 2, pad.top + 3);

  if (trends.length < 2) {
    ctx.fillStyle = cssVar("--chart-axis", "#87969c");
    ctx.font = '12px "PingFang SC", "Microsoft YaHei UI", sans-serif';
    ctx.textAlign = "center";
    ctx.fillText("等待整车数据形成曲线", pad.left + plotWidth / 2, pad.top + plotHeight / 2);
    return;
  }

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
  const drawSeries = (key, color, range, fill) => {
    if (!range) return;
    const yOf = value => pad.top + plotHeight * (range.max - value) / (range.max - range.min);
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

  drawSeries("hv_voltage", colors.hv, left, cssVar("--chart-voltage-fill", "rgba(174, 182, 178, .12)"));
  drawSeries("lv_voltage", colors.lv, right, cssVar("--chart-current-fill", "rgba(240, 180, 41, .10)"));
}
