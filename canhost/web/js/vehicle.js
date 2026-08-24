/* 整车页面模块：CANB 整车连接 + 整车总览（SOP/IVT/赛会能量计/PDM/ECU/胎温/趋势）。 */

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
  $("#connectVehicleButton")?.addEventListener("click", connectVehicle);
  $("#disconnectVehicleButton")?.addEventListener("click", disconnectVehicle);
  $("#vehicleConnectBitrate")?.addEventListener("change", updateVehicleDialog);
}

function populateVehicleOptions() {
  const select = $("#vehicleConnectBitrate");
  if (!select) return;
  if (state.bootstrap?.vehicle_simulation_enabled === true
      && ![...select.options].some(option => option.value === "simulation")) {
    const simOption = new Option("内置模拟数据 · CANB / 开发测试", "simulation");
    select.insertBefore(simOption, select.firstChild);
    if (!state.vehicleSnapshot?.connection?.connected) {
      select.value = "simulation";
    }
  }
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
  const button = $("#connectVehicleButton");
  if (button) { button.disabled = true; button.textContent = "连接中…"; }
  text("#vehicleConnectError", "");
  $("#vehicleConnectError")?.classList.add("hidden");

  const result = await state.api.connect_vehicle({
    mode: simulation ? "simulation" : "pcan",
    channel: simulation ? null : channelSelect?.value,
    bitrate,
    bus_profile: simulation || bitrate === 500000 ? "canb" : "canb_legacy",
  });
  if (button) { button.disabled = false; button.textContent = "连接"; }
  if (!result.ok) {
    text("#vehicleConnectError", result.error || "整车连接失败");
    $("#vehicleConnectError")?.classList.remove("hidden");
    return toast(result.error || "整车连接失败", true);
  }
  $("#vehicleConnectDialog")?.close();
  toast(simulation ? "整车模拟数据已启动（CANB）" : `整车连接已建立 · CANB ${bitrate / 1000} kbit/s`);
  await poll();
}

async function disconnectVehicle() {
  if (!state.api) return;
  await state.api.disconnect_vehicle();
  state.vehicleSnapshot = null;
  $("#vehicleConnectDialog")?.close();
  toast("整车连接已断开");
  await poll();
}

/** Build the channel grids, ECU table, and tyre grid once; polls only patch values. */
function buildVehicleStatics() {
  const channelCell = channel =>
    `<div class="veh-channel" data-channel="${channel.key}"><span>${channel.label}</span>`
    + `<b>等待数据</b><em>${channel.unit}</em><small class="veh-channel-state">未收到</small></div>`;
  $("#vehIvtGrid").innerHTML = VEH_IVT_CHANNELS.map(channelCell).join("");
  $("#vehMeterGrid").innerHTML = VEH_METER_CHANNELS.map(channelCell).join("");

  const wheelRows = ["FL", "FR", "RL", "RR"].map((wheel, index) =>
    `<div class="veh-ecu-row" data-wheel="${index}"><b>${wheel}</b>`
    + `<span data-field="torque_pct">—</span><span data-field="velocity_rpm">—</span>`
    + `<span data-field="motor_temp_c">—</span><span data-field="inverter_temp_c">—</span>`
    + `<span data-field="igbt_temp_c">—</span></div>`).join("");
  $("#vehEcuTable").innerHTML = `<div class="veh-ecu-head"><b>轮位</b><span>扭矩 %Mn</span><span>转速 rpm</span>`
    + `<span>电机 °C</span><span>逆变器 °C</span><span>IGBT °C</span></div>` + wheelRows;

  const tireGroups = ["0x071", "0x072", "0x073", "0x074"].map((frameId, group) =>
    `<div class="veh-tire-group" data-frame="${frameId}"><div class="veh-tire-head">`
    + `<b>${frameId} · 测点 ${group * 4 + 1}–${group * 4 + 4}</b><span class="veh-tire-state">未收到</span></div>`
    + `<div class="veh-tire-points">` + [0, 1, 2, 3].map(point =>
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
    const fresh = isFresh(entry.age, freshLimit);
    const value = fresh && entry.value != null ? fmt(entry.value, channel.digits) : "等待数据";
    node.querySelector("b").textContent = channel.optional && !fresh ? "未发送" : value;
    const stateNode = node.querySelector(".veh-channel-state");
    const status = entry.status;
    stateNode.textContent = !fresh ? "未收到" : status === 0 ? `${fmt(entry.age, 1)} s 前` : "结果异常";
    stateNode.className = `veh-channel-state${fresh ? (status === 0 ? " ok" : " bad") : ""}`;
    node.classList.toggle("stale", !fresh);
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
  $("#disconnectVehicleButton")?.classList.toggle("hidden", connection.connected !== true);

  // -- SOP ---------------------------------------------------------------
  const sop = snapshot.sop || {};
  const limits = sop.limits || {};
  const sopStatus = sop.status || {};
  const limitsFresh = isFresh(sop.limits_age);
  text("#vehSopDisA", limitsFresh ? fmt(limits.discharge_current_a, 1) : "等待数据");
  text("#vehSopChgA", limitsFresh ? fmt(limits.charge_current_a, 1) : "等待数据");
  text("#vehSopDisKw", limitsFresh ? fmt(limits.discharge_power_kw, 1) : "等待数据");
  text("#vehSopChgKw", limitsFresh ? fmt(limits.charge_power_kw, 1) : "等待数据");
  text("#vehSopAge", limitsFresh ? `0x4A0/0x4A3 · ${fmt(sop.status_age, 1)} s 前` : "等待数据");
  const statusFresh = isFresh(sop.status_age);
  const flagText = value => statusFresh ? (value ? "是" : "否") : "—";
  text("#vehSopLimitsValid", flagText(sopStatus.limits_valid));
  text("#vehSopDrive", flagText(sopStatus.drive_allowed));
  text("#vehSopRegen", flagText(sopStatus.regen_allowed));
  text("#vehSopIntervention", statusFresh ? VEH_INTERVENTION_NAMES[sopStatus.intervention_level] ?? "—" : "—");
  text("#vehSopCrc", statusFresh ? (sopStatus.crc_valid ? "通过" : "失败") : "—");
  setClass("#vehSopCrc", "bad", statusFresh && sopStatus.crc_valid === false);
  setClass("#vehSopCrc", "ok", statusFresh && sopStatus.crc_valid === true);
  text("#vehSopBmsState", statusFresh ? VEH_STATE_NAMES[sopStatus.bms_state] ?? sopStatus.bms_state ?? "—" : "—");
  const ack = sop.ecu_ack || {};
  const ackFresh = isFresh(sop.ecu_ack_age);
  const ackState = !ackFresh ? "等待数据"
    : ack.pair_valid && ack.limits_applied ? "已采用新限值"
    : ack.ecu_fault ? "ECU 故障" : ack.pair_valid ? "校验通过 · 未确认采用" : "校验未通过";
  text("#vehEcuAckState", ackState);
  setClass("#vehEcuAckState", "ok", ackFresh && ack.pair_valid && ack.limits_applied);
  setClass("#vehEcuAckState", "bad", ackFresh && (ack.ecu_fault || (ack.pair_valid === false)));
  text("#vehEcuPowers", ackFresh ? `${fmt(ack.discharge_power_kw, 1)} / ${fmt(ack.regen_power_kw, 1)} kW` : "—");
  text("#vehEcuMeta", ackFresh ? `序号 ${ack.sequence ?? "—"} · 来源 ${ack.limit_source ?? "—"}` : "—");

  // -- BMS mirror ---------------------------------------------------------
  const pack = snapshot.pack || {};
  const packFresh = isFresh(pack.age);
  text("#vehPackV", packFresh && pack.voltage_valid ? fmt(pack.voltage_v, 1) : "等待数据");
  text("#vehPackI", packFresh && pack.current_valid ? fmt(pack.current_a, 1) : "等待数据");
  text("#vehPackSoc", packFresh && pack.soc_valid ? fmt(pack.soc_pct, 0) : "等待数据");
  const packStateNode = $("#vehPackState");
  text("#vehPackState", packFresh ? VEH_STATE_NAMES[pack.state] ?? pack.state ?? "—" : "等待数据");
  packStateNode.className = `veh-state-text ${packFresh ? (pack.state === 7 ? "bad" : pack.state === 5 ? "ok" : "") : ""}`;
  text("#vehPackAge", packFresh ? `0x4B0 · ${fmt(pack.age, 1)} s 前` : "0x4B0 · 等待数据");
  const fault = snapshot.fault || {};
  const faultFresh = fault.received === true && isFresh(fault.age);
  text("#vehPackAlarm", faultFresh ? fault.alarm_level_name || "—" : "—");
  text("#vehFaultCode", faultFresh ? fault.code_hex : "等待数据");

  // -- IVT + competition meter --------------------------------------------
  setVehChannel("#vehIvtGrid", VEH_IVT_CHANNELS, snapshot.ivt, SLOW_DATA_FRESH_MAX_S);
  setVehChannel("#vehMeterGrid", VEH_METER_CHANNELS, snapshot.meter, SLOW_DATA_FRESH_MAX_S);
  const ivtFresh = VEH_IVT_CHANNELS.some(channel => isFresh(snapshot.ivt?.[channel.key]?.age, SLOW_DATA_FRESH_MAX_S));
  text("#vehIvtNote", ivtFresh ? "0x512–0x519 · 小端" : "0x512–0x519 · 等待数据");
  const meterFresh = VEH_METER_CHANNELS.some(channel => isFresh(snapshot.meter?.[channel.key]?.age, SLOW_DATA_FRESH_MAX_S));
  text("#vehMeterNote", meterFresh ? "0x521/0x522 · 大端" : "0x521/0x522 · 等待数据");

  // -- PDM -----------------------------------------------------------------
  const pdm = snapshot.pdm || {};
  const renderPdmSide = (side, prefix) => {
    const entry = pdm[side] || {};
    const fresh = isFresh(entry.age, SLOW_DATA_FRESH_MAX_S) && !entry.offline;
    text(`#${prefix}V`, fresh ? fmt(entry.voltage_v, 1) : "等待数据");
    text(`#${prefix}I`, fresh ? fmt(entry.current_a, 1) : "等待数据");
    text(`#${prefix}P`, fresh ? fmt(entry.power_w, 0) : "等待数据");
    text(`#${prefix}Wh`, fresh ? fmt(entry.energy_wh, 2) : "等待数据");
    const stateNode = $(`#${prefix}State`);
    if (stateNode) {
      const label = entry.offline ? "INA226 离线" : !isFresh(entry.age, SLOW_DATA_FRESH_MAX_S) ? "数据超时" : `${fmt(entry.age, 1)} s 前`;
      stateNode.textContent = label;
      stateNode.className = `state-text${entry.offline ? " bad" : ""}`;
    }
  };
  renderPdmSide("bus", "vehPdmBus");
  renderPdmSide("battery", "vehPdmBat");
  const anyPdm = ["bus", "battery"].some(side => isFresh(pdm[side]?.age, SLOW_DATA_FRESH_MAX_S));
  text("#vehPdmNote", anyPdm ? "0x5A0/0x5A1 · 2 Hz" : "0x5A0/0x5A1 · 等待数据");

  // -- ECU ------------------------------------------------------------------
  const ecu = snapshot.ecu || {};
  const ecuAges = Object.values(ecu.age || {});
  const ecuFresh = ecuAges.some(ageValue => isFresh(ageValue));
  $$("#vehEcuTable .veh-ecu-row").forEach(row => {
    const index = +row.dataset.wheel;
    ["torque_pct", "velocity_rpm", "motor_temp_c", "inverter_temp_c", "igbt_temp_c"].forEach(field => {
      const node = row.querySelector(`[data-field="${field}"]`);
      const fresh = isFresh(ecu.age?.[ecuAgeKey(field)]);
      const values = ecu[field] || [];
      node.textContent = fresh && values[index] != null ? fmt(values[index], field === "velocity_rpm" ? 0 : 1) : "等待数据";
    });
  });
  const ecuStatus = ecu.status || {};
  const statusFreshEcu = isFresh(ecu.age?.status);
  const wheelFlagText = flags => ["FR", "FL", "RR", "RL"].map(wheel => `${wheel}${flags?.[wheel] ? "✓" : "—"}`).join(" ");
  text("#vehEcuReady", statusFreshEcu ? wheelFlagText(ecuStatus.system_ready) : "—");
  text("#vehEcuEnable", statusFreshEcu ? wheelFlagText(ecuStatus.enable) : "—");
  text("#vehEcuError", statusFreshEcu ? wheelFlagText(ecuStatus.error) : "—");
  setClass("#vehEcuError", "bad", statusFreshEcu && Object.values(ecuStatus.error || {}).some(Boolean));
  text("#vehEcuNote", ecuFresh ? "0x502–0x509 · 10 ms" : "0x502–0x509 · 等待数据");

  // -- Tyres ------------------------------------------------------------------
  const tires = snapshot.tires || {};
  const tireFresh = isFresh(tires.age, SLOW_DATA_FRESH_MAX_S);
  $$("#vehTireGrid .veh-tire-group").forEach(group => {
    const frameId = group.dataset.frame;
    const values = tires[frameId];
    const stateNode = group.querySelector(".veh-tire-state");
    if (!Array.isArray(values)) {
      stateNode.textContent = "未收到";
      stateNode.className = "veh-tire-state";
      group.querySelectorAll(".veh-tire-point b").forEach(node => node.textContent = "—");
      return;
    }
    stateNode.textContent = tireFresh ? `${fmt(tires.age, 1)} s 前` : "数据超时";
    stateNode.className = `veh-tire-state${tireFresh ? " ok" : ""}`;
    group.querySelectorAll(".veh-tire-point").forEach(point => {
      const value = values[+point.dataset.point];
      point.querySelector("b").textContent = value == null ? "—" : fmt(value, 2);
    });
  });
  text("#vehTireNote", tireFresh ? "0x071–0x074 · 轮位映射待实物确认" : "0x071–0x074 · 等待数据");

  drawVehicleTrend();
}

function ecuAgeKey(field) {
  return { torque_pct: "torque", velocity_rpm: "velocity", motor_temp_c: "motor_temp",
           inverter_temp_c: "inverter_temp", igbt_temp_c: "igbt_temp" }[field];
}

const VEH_TREND_COLORS = { hv: "#aeb6b2", lv: "#f0b429" };

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
  const labelColor = "#7c7f7d", gridColor = "#2d2f31";

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
    ctx.fillStyle = "#87969c";
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

  drawSeries("hv_voltage", VEH_TREND_COLORS.hv, left, "rgba(174, 182, 178, .12)");
  drawSeries("lv_voltage", VEH_TREND_COLORS.lv, right, "rgba(240, 180, 41, .10)");
}
