/* Engineering tool: local_sim2-compatible telemetry publisher. */

const TELEMETRY_SIMULATOR_PREFS_KEY = "canHostTelemetrySimulatorPreferences";

function bindTelemetrySimulatorControls() {
  const configure = $("#configureSimulatorButton");
  if (!configure) return;
  configure.addEventListener("click", openTelemetrySimulatorDialog);
  $("#startSimulatorButton").addEventListener("click", startTelemetrySimulator);
  $("#stopSimulatorButton").addEventListener("click", stopTelemetrySimulator);
  ["Mqtt", "Serial", "Pcan"].forEach(name => {
    $("#simulatorUse" + name).addEventListener("change", syncTelemetrySimulatorFields);
  });
  $("#simulatorMqttTls").addEventListener("change", event => {
    const port = Number($("#simulatorMqttPort").value);
    if (event.target.checked && port === 1883) $("#simulatorMqttPort").value = "8883";
    if (!event.target.checked && port === 8883) $("#simulatorMqttPort").value = "1883";
  });
}

function setSimulatorSectionEnabled(sectionId, enabled) {
  const section = $(sectionId);
  if (!section) return;
  section.classList.toggle("disabled", !enabled);
  section.querySelectorAll("input, select").forEach(input => { input.disabled = !enabled; });
}

function syncTelemetrySimulatorFields() {
  const mqtt = $("#simulatorUseMqtt").checked;
  const serial = $("#simulatorUseSerial").checked;
  const pcan = $("#simulatorUsePcan").checked;
  setSimulatorSectionEnabled("#simulatorMqttFields", mqtt);
  setSimulatorSectionEnabled("#simulatorSerialFields", serial);
  setSimulatorSectionEnabled("#simulatorPcanFields", pcan);
  const confirmation = $("#simulatorPcanConfirmRow");
  confirmation.classList.toggle("hidden", !pcan);
  $("#simulatorPcanConfirm").required = pcan;
}

function populateTelemetrySimulatorHardware() {
  const channel = $("#simulatorPcanChannel");
  if (channel) populatePcanSelect("#simulatorPcanChannel", state.bootstrap || {});
  const portList = $("#simulatorSerialPortList");
  portList.innerHTML = (state.bootstrap?.serial_ports || [])
    .map(item => `<option value="${escapeHtml(item.device)}">${escapeHtml(item.description)}</option>`).join("");
}

function restoreTelemetrySimulatorSettings() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(TELEMETRY_SIMULATOR_PREFS_KEY) || "null"); }
  catch { /* local storage is optional */ }
  if (saved) {
    $("#simulatorUseMqtt").checked = saved.mqtt !== false;
    $("#simulatorUseSerial").checked = saved.serial === true;
    $("#simulatorUsePcan").checked = saved.pcan === true;
    if (saved.mqtt_host) $("#simulatorMqttHost").value = saved.mqtt_host;
    if (saved.mqtt_port) $("#simulatorMqttPort").value = String(saved.mqtt_port);
    if (saved.mqtt_topic) $("#simulatorMqttTopic").value = saved.mqtt_topic;
    if (saved.mqtt_username !== undefined) $("#simulatorMqttUsername").value = saved.mqtt_username;
    $("#simulatorMqttTls").checked = saved.mqtt_tls === true;
    if (saved.serial_port) $("#simulatorSerialPort").value = saved.serial_port;
    if (saved.serial_baudrate) $("#simulatorSerialBaudrate").value = String(saved.serial_baudrate);
    if (saved.serial_suffix_hex !== undefined) $("#simulatorSerialSuffix").value = saved.serial_suffix_hex;
    if (saved.pcan_channel) $("#simulatorPcanChannel").value = saved.pcan_channel;
    if (saved.pcan_bitrate) $("#simulatorPcanBitrate").value = String(saved.pcan_bitrate);
  }
  $("#simulatorMqttPassword").value = "";
  $("#simulatorPcanConfirm").checked = false;
  syncTelemetrySimulatorFields();
}

function simulatorConfigFromForm() {
  return {
    mqtt: $("#simulatorUseMqtt").checked,
    serial: $("#simulatorUseSerial").checked,
    pcan: $("#simulatorUsePcan").checked,
    mqtt_host: $("#simulatorMqttHost").value.trim(),
    mqtt_port: Number($("#simulatorMqttPort").value),
    mqtt_topic: $("#simulatorMqttTopic").value.trim(),
    mqtt_username: $("#simulatorMqttUsername").value.trim(),
    mqtt_password: $("#simulatorMqttPassword").value,
    mqtt_tls: $("#simulatorMqttTls").checked,
    serial_port: $("#simulatorSerialPort").value.trim(),
    serial_baudrate: Number($("#simulatorSerialBaudrate").value),
    serial_suffix_hex: $("#simulatorSerialSuffix").value.trim(),
    pcan_channel: $("#simulatorPcanChannel").value,
    pcan_bitrate: Number($("#simulatorPcanBitrate").value),
  };
}

function saveTelemetrySimulatorSettings(config) {
  const saved = { ...config };
  delete saved.mqtt_password;
  try { localStorage.setItem(TELEMETRY_SIMULATOR_PREFS_KEY, JSON.stringify(saved)); }
  catch { /* local storage is optional */ }
}

async function openTelemetrySimulatorDialog() {
  if (state.bootstrap?.telemetry_simulator_enabled !== true) {
    return toast("当前发布版本未包含本地遥测模拟器", true);
  }
  await refreshPcanChannels(false);
  populateTelemetrySimulatorHardware();
  restoreTelemetrySimulatorSettings();
  $("#simulatorConfigError").classList.add("hidden");
  $("#telemetrySimulatorDialog").showModal();
}

async function startTelemetrySimulator() {
  if (!state.api) return toast("应用后端未就绪", true);
  const form = $("#telemetrySimulatorDialog form");
  if (!form.reportValidity()) return;
  const config = simulatorConfigFromForm();
  if (!config.mqtt && !config.serial && !config.pcan) {
    text("#simulatorConfigError", "至少选择一种输出路径");
    $("#simulatorConfigError").classList.remove("hidden");
    return;
  }
  const button = $("#startSimulatorButton");
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = "启动中…";
  $("#simulatorConfigError").classList.add("hidden");
  let result;
  try { result = await state.api.start_telemetry_simulator(config); }
  catch (error) { result = { ok: false, error: String(error) }; }
  button.disabled = false;
  button.removeAttribute("aria-busy");
  button.textContent = "启动模拟";
  if (!result?.ok) {
    text("#simulatorConfigError", result?.error || "模拟器启动失败");
    $("#simulatorConfigError").classList.remove("hidden");
    return;
  }
  saveTelemetrySimulatorSettings(config);
  $("#simulatorMqttPassword").value = "";
  $("#telemetrySimulatorDialog").close();
  toast(result.message || "本地遥测模拟器正在启动");
  await poll();
}

async function stopTelemetrySimulator() {
  if (!state.api) return;
  const button = $("#stopSimulatorButton");
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  const result = await state.api.stop_telemetry_simulator();
  button.disabled = false;
  button.removeAttribute("aria-busy");
  toast(result.ok ? "本地遥测模拟器已停止" : result.error || "停止失败", !result.ok);
  await poll();
}

function simulatorStateText(snapshot) {
  return ({
    stopped: "已停止", starting: "启动中", running: "发送中",
    stopping: "停止中", error: "运行失败", unavailable: "不可用",
  })[snapshot.state] || "状态未知";
}

function renderTelemetrySimulator() {
  const snapshot = state.toolSnapshots.simulator || {};
  const config = snapshot.config || {};
  const latest = snapshot.latest || null;
  const running = snapshot.running === true;
  const failed = snapshot.state === "error";
  const status = $("#simulatorStatus");
  status.textContent = simulatorStateText(snapshot);
  status.className = `tag ${failed ? "bad" : running ? "ok" : "neutral"}`;
  $("#configureSimulatorButton").classList.toggle("hidden", running);
  $("#stopSimulatorButton").classList.toggle("hidden", !running);

  const outputNames = [config.mqtt && "MQTT", config.serial && "串口", config.pcan && "PCAN"].filter(Boolean);
  text("#simulatorOutputSummary", outputNames.length ? outputNames.join(" + ") : "等待配置");
  const setOutput = (name, enabled, detail) => {
    const card = $(`#simulator${name}Card`);
    card.classList.toggle("active", enabled && running);
    card.classList.toggle("failed", enabled && failed);
    text(`#simulator${name}State`, enabled ? failed ? "已停止" : running ? "正在输出" : "已配置" : "未启用");
    text(`#simulator${name}Detail`, enabled ? detail : "—");
  };
  setOutput("Mqtt", config.mqtt, `${config.mqtt_host || "—"}:${config.mqtt_port || "—"} · ${config.mqtt_topic || "—"}`);
  setOutput("Serial", config.serial, `${config.serial_port || "—"} · ${config.serial_baudrate || "—"} bit/s`);
  setOutput("Pcan", config.pcan, `${config.pcan_channel || "—"} · ${Number(config.pcan_bitrate || 0) / 1000} kbit/s`);

  text("#simulatorRunAge", snapshot.run_age == null ? "等待启动"
    : `${running ? "已运行" : "上次运行"} ${fmt(snapshot.run_age, 1)} s`);
  text("#simulatorVehicleState", latest?.state || "等待数据");
  text("#simulatorSequence", latest ? `SEQ ${latest.sequence} · ${latest.payload_bytes} B` : "SEQ —");
  text("#simulatorRpm", latest ? Number(latest.rpm).toLocaleString() : "—");
  text("#simulatorSpeed", latest?.speed_kmh ?? "—");
  text("#simulatorVoltage", latest ? fmt(latest.hv_voltage_v, 1) : "—");
  text("#simulatorCurrent", latest ? fmt(latest.hv_current_a, 1) : "—");
  text("#simulatorSoc", latest?.soc_pct ?? "—");
  text("#simulatorGeneratedCount", Number(snapshot.protobuf_frames || 0).toLocaleString());
  text("#simulatorMqttCount", Number(snapshot.mqtt_frames || 0).toLocaleString());
  text("#simulatorSerialCount", Number(snapshot.serial_frames || 0).toLocaleString());
  text("#simulatorPcanCount", Number(snapshot.pcan_frames || 0).toLocaleString());
  const error = $("#simulatorRuntimeError");
  error.textContent = snapshot.error || "";
  error.classList.toggle("hidden", !snapshot.error);
}
