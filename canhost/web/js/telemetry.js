/* MQTT 遥测页：独立订阅 fsae/telemetry，聚焦 BMS 故障码与 Alarm。 */

const TELEMETRY_FRESH_MAX_S = 3.0;

function bindTelemetryControls() {
  const openButton = $("#telemetryConnectButton");
  if (!openButton) return;
  restoreTelemetrySettings();
  openButton.addEventListener("click", () => {
    const connectionState = state.telemetrySnapshot?.connection?.state;
    const active = !!connectionState && connectionState !== "disconnected";
    $("#disconnectTelemetryButton").classList.toggle("hidden", !active);
    $("#telemetryConnectDialog").showModal();
  });
  $("#doConnectTelemetry").addEventListener("click", connectTelemetry);
  $("#disconnectTelemetryButton").addEventListener("click", disconnectTelemetry);
  $("#telemetryTls").addEventListener("change", event => {
    const port = Number($("#telemetryPort").value);
    if (event.target.checked && port === 1883) $("#telemetryPort").value = "8883";
    if (!event.target.checked && port === 8883) $("#telemetryPort").value = "1883";
  });
}

function restoreTelemetrySettings() {
  try {
    const saved = JSON.parse(localStorage.getItem("canHostTelemetryEndpoint") || "null");
    if (!saved) return;
    if (saved.host) $("#telemetryHost").value = saved.host;
    if (saved.port) $("#telemetryPort").value = String(saved.port);
    if (saved.topic) $("#telemetryTopic").value = saved.topic;
    if (saved.username) $("#telemetryUsername").value = saved.username;
    $("#telemetryTls").checked = saved.tls === true;
  } catch { /* local storage may be disabled or contain an old value */ }
}

function saveTelemetrySettings(config) {
  try {
    localStorage.setItem("canHostTelemetryEndpoint", JSON.stringify({
      host: config.host,
      port: config.port,
      topic: config.topic,
      username: config.username,
      tls: config.tls,
    }));
  } catch { /* local storage is optional; passwords are never persisted */ }
}

async function connectTelemetry() {
  if (!state.api) return toast("应用后端未就绪", true);
  const form = $("#telemetryConnectDialog form");
  if (!form.reportValidity()) return;
  const config = {
    host: $("#telemetryHost").value.trim(),
    port: Number($("#telemetryPort").value),
    topic: $("#telemetryTopic").value.trim(),
    username: $("#telemetryUsername").value.trim(),
    password: $("#telemetryPassword").value,
    tls: $("#telemetryTls").checked,
  };
  const button = $("#doConnectTelemetry");
  button.disabled = true;
  button.textContent = "连接中…";
  $("#telemetryConnectError").classList.add("hidden");
  const result = await state.api.connect_telemetry(config);
  button.disabled = false;
  button.textContent = "订阅";
  if (!result.ok) {
    text("#telemetryConnectError", result.error || "订阅启动失败");
    $("#telemetryConnectError").classList.remove("hidden");
    return;
  }
  saveTelemetrySettings(config);
  $("#telemetryPassword").value = "";
  $("#telemetryConnectDialog").close();
  toast(result.message || "MQTT 遥测订阅正在连接");
  await poll();
}

async function disconnectTelemetry() {
  if (!state.api) return;
  await state.api.disconnect_telemetry();
  $("#telemetryPassword").value = "";
  $("#telemetryConnectDialog").close();
  toast("MQTT 遥测订阅已断开");
  await poll();
}

function telemetryStateName(connection) {
  const names = {
    disconnected: "遥测未连接",
    connecting: "正在连接遥测",
    subscribed: "遥测订阅中",
    reconnecting: "遥测正在重连",
    error: "遥测连接失败",
  };
  return names[connection.state] || "遥测状态未知";
}

function renderTelemetryBadge() {
  const snapshot = state.telemetrySnapshot || {};
  const latest = snapshot.latest || null;
  const payloadFresh = latest !== null && isFresh(snapshot.last_message_age, TELEMETRY_FRESH_MAX_S);
  const faultFresh = payloadFresh && latest.fault?.valid === true;
  const count = faultFresh ? (latest.fault?.active || []).length : 0;
  const badge = $("#telemetryNavBadge");
  if (!badge) return;
  badge.textContent = String(count);
  badge.classList.toggle("hidden", !faultFresh || count === 0);
}

function renderTelemetry() {
  const snapshot = state.telemetrySnapshot || {};
  const connection = snapshot.connection || {};
  const latest = snapshot.latest || null;
  const payloadFresh = latest !== null && isFresh(snapshot.last_message_age, TELEMETRY_FRESH_MAX_S);
  const fault = latest?.fault || {};
  const faultFresh = payloadFresh && fault.valid === true;
  const active = faultFresh ? (fault.active || []) : [];
  const error = connection.error || snapshot.last_parse_error;

  text("#telemetryConnectionName", telemetryStateName(connection));
  const endpoint = connection.host
    ? `${connection.host}:${connection.port || 1883} · ${connection.topic || "fsae/telemetry"}`
    : "配置 MQTT 订阅";
  text("#telemetryConnectionDetail", error || endpoint);
  $("#telemetryConnectionDetail").title = error || endpoint;
  const dot = $("#telemetryStatusDot");
  dot.className = `status-dot${connection.connected ? " connected" : ""}${connection.state === "error" || connection.state === "reconnecting" ? " fault" : ""}`;
  const connectionActive = !!connection.state && connection.state !== "disconnected";
  $("#telemetryConnectButton").textContent = connectionActive ? "订阅设置" : "配置订阅";
  $("#disconnectTelemetryButton").classList.toggle("hidden", !connectionActive);

  text("#telemetryRxCount", Number(snapshot.rx_count || 0).toLocaleString());
  text("#telemetryParseErrors", Number(snapshot.parse_error_count || 0).toLocaleString());
  $("#telemetryParseErrors").classList.toggle("bad", Number(snapshot.parse_error_count || 0) > 0);
  text("#telemetryLastAge", snapshot.last_message_age == null
    ? "等待数据" : snapshot.last_message_age <= 0.05 ? "刚刚" : `${fmt(snapshot.last_message_age, 1)} s`);

  text("#telemetryFaultCode", faultFresh ? fault.code_hex : "等待数据");
  $("#telemetryFaultCode").classList.toggle("bad", faultFresh && fault.code !== 0);
  text("#telemetryFaultSource", !payloadFresh ? "等待 TelemetryFrame"
    : !faultFresh ? "BMS 故障源未上报或已过期"
    : fault.sources_mismatch ? `${fault.source} · 两个兼容字段不一致`
    : `${fault.source} · ${latest.received_at}`);
  $("#telemetryFaultSource").classList.toggle("bad", faultFresh && fault.sources_mismatch);
  text("#telemetryActiveCount", faultFresh ? `${active.length} 项` : "等待数据");
  text("#telemetryAlarmLevel", faultFresh ? latest.bms?.alarm_level_name || "未上报" : "等待数据");
  text("#telemetryBmsState", faultFresh ? latest.bms?.state_name || "未上报" : "等待数据");
  text("#telemetrySequence", payloadFresh
    ? latest.header?.sequence ?? latest.frame_id ?? "未上报" : "等待数据");

  renderTelemetryBadge();
  text("#telemetryFaultState", !faultFresh ? "等待数据" : active.length ? `${active.length} 位活动` : "当前无故障");
  $("#telemetryFaultState").classList.toggle("bad", faultFresh && active.length > 0);
  $("#telemetryActiveFaults").innerHTML = !faultFresh
    ? `<div class="empty-state">遥测数据无效或已超时，等待新数据。</div>`
    : active.length
      ? active.map(item => `<div class="telemetry-fault-item"><span>BIT ${item.bit}</span><b>${escapeHtml(item.name)}</b></div>`).join("")
      : `<div class="telemetry-normal-state"><b>无活动故障</b><span>32 位 BMS 故障字均未置位</span></div>`;

  const alarms = payloadFresh ? (latest.alarms || []) : [];
  text("#telemetryAlarmCount", !payloadFresh ? "等待数据" : alarms.length ? `${alarms.length} 条` : "本帧未携带");
  $("#telemetryAlarmList").innerHTML = !payloadFresh
    ? `<div class="empty-state">遥测数据无效或已超时，等待新数据。</div>`
    : alarms.length
      ? alarms.map(item => `<div class="telemetry-alarm-item severity-${item.severity}"><span><b>${escapeHtml(item.id_hex)}</b><small>${escapeHtml(item.message || "未附消息")}</small></span><em>${escapeHtml(item.severity_name)}</em></div>`).join("")
      : `<div class="telemetry-normal-state neutral"><b>本帧未携带 Alarm</b><span>以 BMS 故障字和告警等级为准</span></div>`;

  const history = snapshot.fault_history || [];
  $("#telemetryFaultHistory").innerHTML = history.length
    ? history.map(event => `<div class="event-item"><time>${escapeHtml(event.time)}</time><b>${escapeHtml(event.previous)} → ${escapeHtml(event.code)}</b><p>${event.added.length ? `<span class="added">进入：${event.added.map(escapeHtml).join("、")}</span>` : ""}${event.added.length && event.cleared.length ? "<br>" : ""}${event.cleared.length ? `<span class="cleared">清除：${event.cleared.map(escapeHtml).join("、")}</span>` : ""}</p></div>`).join("")
    : `<div class="empty-state">本次订阅发生故障进入或清除后在这里显示。</div>`;
}
