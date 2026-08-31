/* IVT 能量计配置页面模块：独立 CAN1 配置连接，与主监视连接互不影响。 */

const IVT_PERIOD_CHANNELS = ["I", "U1", "U2", "U3", "T", "W", "As", "Wh"];

function bindIvtControls() {
  $("#connectIvtButton").addEventListener("click", connectIvt);
  $("#disconnectIvtButton").addEventListener("click", disconnectIvt);
  $("#readIvtConfig").addEventListener("click", readIvtConfig);
  $("#configureIvt").addEventListener("click", () => {
    const options = readIvtOptions();
    if (!options) return;
    confirmIvtAction("configure", options, "配置 IVT 为 BMS CAN1",
      "将停止 IVT，写入 8 个通道和 10 个 CAN ID，保存到 IVT 非易失存储，重启后再逐项读回核对。执行前确认配置总线上只有目标 IVT。", true);
  });
  $("#switchIvt250").addEventListener("click", () => confirmIvtBitrate(250000));
  $("#switchIvt500").addEventListener("click", () => confirmIvtBitrate(500000));
  $("#loadIvtReadbackPeriods").addEventListener("click", loadIvtReadbackPeriods);
  document.querySelectorAll("[data-ivt-period]").forEach(input => {
    input.addEventListener("input", () => {
      renderIvtPeriodRate();
      renderIvtConfigureButton();
    });
  });
  renderIvtPeriodRate();
}

async function connectIvt() {
  if (!state.api) return toast("应用后端未就绪", true);
  const bitrate = Number($("#ivtBitrateSelect").value || 500000);
  const button = $("#connectIvtButton");
  button.disabled = true; button.textContent = "连接中…";
  const result = await state.api.connect_ivt({
    channel: $("#ivtChannelSelect").value,
    bitrate,
    bus_profile: "can1",
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
  const periods = readIvtPeriods();
  if ([positive, positiveReset, negative, negativeReset, periods].some(value => value == null)) return null;
  return {
    ...(cmdId == null ? {} : { cmd_id: cmdId }),
    ...(rspId == null ? {} : { rsp_id: rspId }),
    startup: $("#ivtStartupMode").value,
    positive_threshold_a: positive,
    positive_reset_threshold_a: positiveReset,
    negative_threshold_a: negative,
    negative_reset_threshold_a: negativeReset,
    channel_periods_ms: periods,
  };
}

function readIvtPeriods(showError = true) {
  const periods = {};
  for (const name of IVT_PERIOD_CHANNELS) {
    const input = document.querySelector(`[data-ivt-period="${name}"]`);
    const value = Number(input?.value);
    if (!Number.isInteger(value) || value < 1 || value > 65535) {
      if (showError) toast(`通道 ${name} 周期必须是 1..65535 ms 的整数`, true);
      return null;
    }
    periods[name] = value;
  }
  return periods;
}

function renderIvtPeriodRate() {
  const periods = readIvtPeriods(false);
  const node = $("#ivtPeriodRate");
  if (!node) return;
  if (!periods) {
    node.textContent = "周期输入有误";
    return;
  }
  const framesPerSecond = Object.values(periods).reduce((sum, period) => sum + 1000 / period, 0);
  node.textContent = `约 ${framesPerSecond.toFixed(framesPerSecond >= 100 ? 0 : 1)} 帧/s`;
}

function ivtPeriodsMatchReadback(readback) {
  const periods = readIvtPeriods(false);
  if (!periods || !Array.isArray(readback?.channels) || readback.channels.length !== IVT_PERIOD_CHANNELS.length) {
    return false;
  }
  return readback.channels.every(channel => periods[channel.name] === Number(channel.period_ms));
}

function ivtEffectiveDifferences(readback) {
  const differences = (readback?.comparison?.differences || [])
    .filter(item => !item.field.endsWith(".period_ms"));
  const periods = readIvtPeriods(false);
  if (!periods) return differences;
  for (const channel of readback?.channels || []) {
    const actual = Number(channel.period_ms);
    const expected = periods[channel.name];
    if (expected != null && actual !== expected) {
      differences.push({
        field: `channel.${channel.name}.period_ms`,
        actual, expected, actual_text: String(actual), expected_text: String(expected),
      });
    }
  }
  return differences;
}

function loadIvtReadbackPeriods() {
  const channels = state.toolSnapshots.ivt?.ivt_config?.channels || [];
  if (channels.length !== IVT_PERIOD_CHANNELS.length) return toast("请先读取完整 IVT 配置", true);
  for (const channel of channels) {
    const input = document.querySelector(`[data-ivt-period="${channel.name}"]`);
    if (input) input.value = String(channel.period_ms);
  }
  renderIvtConfig();
  toast("已载入当前 IVT 通道周期");
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
    && connection.bus_profile === "can1";
}

function confirmIvtAction(kind, options, title, message, destructive = false) {
  if (!ivtConnectionAvailable()) return toast("请先连接真实 CAN1 配置 PCAN", true);
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
  const periodText = kind === "configure"
    ? "\n周期：" + IVT_PERIOD_CHANNELS.map(name => `${name} ${options.channel_periods_ms[name]} ms`).join(" · ")
    : "";
  text("#confirmPayload", "通道：" + (conn.channel || "PCAN") + "\n"
    + "目标：CAN1 · " + ((conn.bitrate || 500000) / 1000) + " kbit/s\n"
    + "Command：0x" + cmdId.toString(16).toUpperCase() + "\n"
    + "Response：0x" + rspId.toString(16).toUpperCase() + "\n"
    + "操作：" + operation + periodText);
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
    "IVT 会停止并重启到目标位率。上位机会关闭当前 PCAN，再用目标位率重新打开并等待 Alive。执行前确认配置总线上只有目标 IVT。正式接入 CAN1 前必须切回 500 kbit/s。", true);
}

function renderIvtConfig() {
  const snapshot = state.toolSnapshots.ivt || {};
  const connection = snapshot.connection || {};
  const available = ivtConnectionAvailable();
  const currentBitrate = Number(connection.bitrate || 0);
  if (currentBitrate && document.activeElement !== $("#ivtBitrateSelect")) {
    $("#ivtBitrateSelect").value = String(currentBitrate);
  }
  const readback = snapshot?.ivt_config;
  const comparison = readback?.comparison;
  $("#connectIvtButton").classList.toggle("hidden", available);
  $("#disconnectIvtButton").classList.toggle("hidden", !available);
  if ($("#readIvtConfig")) $("#readIvtConfig").disabled = !available;
  if ($("#loadIvtReadbackPeriods")) $("#loadIvtReadbackPeriods").disabled = !readback;
  if ($("#switchIvt250")) $("#switchIvt250").disabled = !available || currentBitrate === 250000;
  if ($("#switchIvt500")) $("#switchIvt500").disabled = !available || currentBitrate === 500000;
  renderIvtConfigureButton();
  renderIvtPeriodRate();

  const statusNode = $("#ivtConfigStatus");
  if (!statusNode) return;
  const readbackNode = $("#ivtReadback");
  if (!readback) {
    statusNode.className = "tag neutral";
    statusNode.textContent = "未读取";
    text("#ivtCheckSummary", available ? "点击读取，自动尝试出厂地址和 BMS 目标地址。" : "连接后读取设备信息、通道和 CAN ID。");
    readbackNode?.classList.add("hidden");
    return;
  }
  const effectiveDifferences = ivtEffectiveDifferences(readback);
  const periodTargetChanged = Boolean(readback) && !ivtPeriodsMatchReadback(readback);
  const hasNonPeriodDifference = effectiveDifferences.some(item => !item.field.endsWith(".period_ms"));
  const periodOnlyChange = periodTargetChanged && !hasNonPeriodDifference;
  const effectiveStatus = !effectiveDifferences.length
    ? "configured"
    : comparison?.status === "unconfigured" ? "unconfigured" : "mismatch";
  const statusClasses = { configured: "ok", unconfigured: "warn", mismatch: "bad" };
  const effectiveStatusName = { configured: "已配置且一致", unconfigured: "未配置", mismatch: "配置不符" };
  statusNode.className = "tag " + (periodOnlyChange ? "warn" : statusClasses[effectiveStatus] || "neutral");
  statusNode.textContent = periodOnlyChange ? "周期待写入" : effectiveStatusName[effectiveStatus] || "未读取";
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
  const statusText = periodOnlyChange ? "周期目标已修改" : effectiveStatusName[effectiveStatus] || "已读取";
  const summaryText = periodOnlyChange
    ? "确认各通道周期后执行 BMS CAN1 配置，写入后会自动重启并读回核对。"
    : effectiveStatus === "configured"
    ? "目标已对齐，可以断开配置通道并接入 F405。"
    : effectiveStatus === "unconfigured"
      ? "读到出厂配置；首次接入 F405 CAN1 前请执行一次 BMS CAN1 配置。"
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
  const targetPeriods = readIvtPeriods(false) || {};
  const diffFields = new Set(effectiveDifferences.map(item => item.field));
  const channelRows = (readback.channels || []).map(channel => {
    const expected = expectedChannels[channel.name] || {};
    const expectedPeriod = targetPeriods[channel.name] ?? expected.period_ms;
    const id = readback.can_ids?.[channel.name];
    const mismatch = ["db1", "period_ms", "mode_name", "byte_order", "report_errors", "invert_sign"]
      .some(key => diffFields.has("channel." + channel.name + "." + key))
      || diffFields.has("can_id." + channel.name);
    return "<tr class=\"" + (mismatch ? "mismatch" : "") + "\">"
      + "<td>" + escapeHtml(channel.name) + "</td><td>" + (channel.period_ms ?? "—") + " / " + (expectedPeriod ?? "—") + " ms</td>"
      + "<td>" + escapeHtml(channel.mode_name || "—") + "</td><td>" + escapeHtml(channel.byte_order || "—") + "</td>"
      + "<td>" + (channel.invert_sign ? "反转" : "正常") + "</td><td>" + (channel.report_errors ? "启用" : "关闭") + "</td>"
      + "<td>0x" + (id ?? 0).toString(16).toUpperCase() + "</td></tr>";
  }).join("");
  $("#ivtConfigTable").innerHTML = "<table><thead><tr><th>通道</th><th>周期 / 目标</th><th>模式</th><th>字节序</th><th>符号</th><th>报错位</th><th>CAN ID</th></tr></thead><tbody>" + channelRows + "</tbody></table>";
  $("#ivtConfigDifferences").innerHTML = effectiveDifferences.length
    ? "<b>差异 " + effectiveDifferences.length + " 项</b><ul>" + effectiveDifferences.map(item => "<li>" + escapeHtml(item.field) + "：实际 " + escapeHtml(item.actual_text) + "，期望 " + escapeHtml(item.expected_text) + "</li>").join("") + "</ul>"
    : "<span class=\"ok-text\">所有目标字段与期望值一致。</span>";
}

function renderIvtConfigureButton() {
  const snapshot = state.toolSnapshots.ivt || {};
  const readback = snapshot.ivt_config;
  const available = ivtConnectionAvailable();
  const configureButton = $("#configureIvt");
  if (!configureButton) return;
  const invalidPeriods = readIvtPeriods(false) == null;
  const alreadyConfigured = Boolean(readback) && ivtEffectiveDifferences(readback).length === 0;
  configureButton.disabled = !available || !readback || alreadyConfigured || invalidPeriods;
  configureButton.title = !available
    ? "请先连接真实 IVT-S"
    : !readback
      ? "请先读取并核对当前配置"
      : invalidPeriods
        ? "请修正通道周期"
        : alreadyConfigured
          ? "当前配置已与输入目标一致，无需重复配置"
          : "写入当前周期和 BMS CAN1 目标配置并重启 IVT";
}
