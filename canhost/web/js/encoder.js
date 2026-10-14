/* BRT 编码器页面模块：独立 PCAN 连接，读取、标定、配置单圈绝对值 CAN 编码器并绘制曲线。
 * 协议、应答核对和曲线样本由后端 canhost/encoder/ 提供，本模块只负责渲染与发起操作。 */

const ENC_PREFS_KEY = "canHostEncoderPreferences";
const ENC_WINDOWS = [5, 10, 30, 60, 0];   // 秒；0 = 全部样本
const ENC_ANGLE_MODES = ["single", "signed", "accum"];
const ENC_PLOT_POINTS = 5000;        // 一次绘制的点数上限，超过则抽稀；完整数据以 CSV 导出为准
const ENC_STORE_CAP = 150000;        // 前端最多保留的样本数
const ENC_FRAME_BITS = 115;          // 7 字节标准帧含位填充的典型位数，只用于估算总线占用
const ENC_KIND_NAMES = {
  position: "单圈位置", ack: "应答", value: "数值应答", unknown: "未识别布局", invalid: "帧头无效", other: "其他节点",
  pedal: "ECU 踏板与油压",
};
const ENC_SERIES_VARS = { angle: "--series-1", speed: "--series-2", pct: "--series-3", kpa: "--series-4" };
const ENC_MOTION_NAMES = { increasing: "角度增大", decreasing: "角度减小", still: "静止" };

const enc = {
  prefs: { channel: "", bitrate: 500000, address: "0x01", pollHz: 50, windowSec: 10, angleMode: "single", sampleMs: 100 },
  catalog: null,
  data: { epoch: -1, cursor: 0, t: [], raw: [], pos: [], spd: [], oil: [], brk: [] },
  origin: null,
  markers: [],
  view: { min: 0, max: 10 },
  plots: { angle: null, oil: null, speed: null },
  plotKey: "",
  drawKey: "",
  paused: false,
  fetching: false,
  reads: {},
  painted: new Map(),
  noticeAction: null,
};

function encLoadPrefs() {
  try {
    const saved = JSON.parse(localStorage.getItem(ENC_PREFS_KEY) || "{}");
    if (saved && typeof saved === "object") Object.assign(enc.prefs, saved);
  } catch { /* 存储不可用时使用默认值 */ }
  if (!ENC_WINDOWS.includes(enc.prefs.windowSec)) enc.prefs.windowSec = 10;
  if (!ENC_ANGLE_MODES.includes(enc.prefs.angleMode)) enc.prefs.angleMode = "single";
}

function encSavePrefs() {
  try { localStorage.setItem(ENC_PREFS_KEY, JSON.stringify(enc.prefs)); } catch { /* 存储不可用时忽略 */ }
}

function encHex(value) { return "0x" + Number(value).toString(16).toUpperCase().padStart(2, "0"); }

function encBitrateText(bitrate) {
  return bitrate >= 1000000 ? `${bitrate / 1000000} Mbit/s` : `${bitrate / 1000} kbit/s`;
}

function encParseAddress(raw, label = "节点地址") {
  const textValue = String(raw ?? "").trim();
  const value = /^0x/i.test(textValue) ? Number.parseInt(textValue.slice(2), 16) : Number(textValue);
  if (!textValue || !Number.isInteger(value) || value < 1 || value > 255) {
    toast(`${label}必须是 0x01–0xFF`, true);
    return null;
  }
  return value;
}

function encParseInt(id, label, minimum, maximum) {
  const raw = $(id)?.value?.trim() ?? "";
  const value = Number(raw);
  if (!raw || !Number.isInteger(value) || value < minimum || value > maximum) {
    toast(`${label}必须是 ${minimum}–${maximum} 的整数`, true);
    return null;
  }
  return value;
}

function encPaint(key, node, html) {
  if (!node || enc.painted.get(key) === html) return;
  enc.painted.set(key, html);
  node.innerHTML = html;
}

function encSnapshot() { return state.toolSnapshots.encoder || null; }
function encConnected() { return encSnapshot()?.connection?.connected === true; }

/* ---------------- 初始化与连接 ---------------- */

function bindEncoderControls() {
  encLoadPrefs();
  $("#encConnectButton")?.addEventListener("click", encConnect);
  $("#encDisconnectButton")?.addEventListener("click", encDisconnect);
  $("#encPollToggle")?.addEventListener("click", encTogglePoll);
  $("#encPollHz")?.addEventListener("change", () => {
    enc.prefs.pollHz = Number($("#encPollHz").value);
    encSavePrefs();
    if (encSnapshot()?.stream?.polling) encRun("poll_start", { hz: enc.prefs.pollHz });
  });
  $("#encReportStart")?.addEventListener("click", () => encRun("report_start"));
  $("#encReportStop")?.addEventListener("click", () => encRun("report_stop"));
  $("#encPeriod")?.addEventListener("input", renderEncoderPeriodHint);
  $("#encPeriodWrite")?.addEventListener("click", () => {
    const period = encParseInt("#encPeriod", "上报周期", ...(enc.catalog?.period_us_range || [5000, 65535]));
    if (period == null) return;
    encWrite("set_period", { period_us: period }, "写入上报周期",
      "上报周期写入编码器并掉电记忆。厂商提示周期不能短于 5 ms，否则总线堵塞，编码器不再响应后续配置指令。",
      [`上报周期：${period} µs（约 ${(1e6 / period).toFixed(period < 1000 ? 0 : 1)} 帧/s）`,
       "写入后以“到达速率”核对是否生效"]);
  });
  $("#encZero")?.addEventListener("click", () => encWrite("set_zero", {}, "当前位置置零",
    "编码器把当前位置记为 0°，原来的零位随之丢失。确认转轴已转到机械零位并固定。",
    ["操作：当前位置 → 0°（计数 0）", `写入后自动读回比较，偏离超过 ${enc.catalog?.zero_tolerance ?? 3} counts 会提示`]));
  $("#encMidpoint")?.addEventListener("click", () => encWrite("set_midpoint", {}, "当前位置设为中点",
    "编码器把当前位置记为 180°（计数 2048），原来的零位随之改变。",
    ["操作：当前位置 → 180°（计数 2048）", "写入后自动读回比较"]));
  $$("[data-enc-direction]").forEach(button => button.addEventListener("click", () => {
    const counterClockwise = button.dataset.encDirection === "ccw";
    const label = counterClockwise ? "逆时针增大" : "顺时针增大";
    encWrite("set_direction", { counter_clockwise: counterClockwise }, `递增方向：${label}`,
      "改变读数增大的旋转方向，当前读数会随之翻转；改完需要重新确认零位。",
      [`操作：递增方向 → ${label}`, "写入前后读数会记录在“本次写入记录”"]);
  }));
  $$("[data-enc-read]").forEach(button => button.addEventListener("click", () => encRead(button.dataset.encRead)));
  $("#encReadAll")?.addEventListener("click", async () => {
    for (const name of ["read_position", "read_multiturn", "read_turns", "read_speed"]) {
      const result = await encRead(name);
      if (!result?.ok) break;
    }
  });
  $("#encAddressWrite")?.addEventListener("click", () => {
    const address = encParseAddress($("#encNewAddress").value, "新节点地址");
    if (address == null) return;
    const current = encSnapshot()?.connection?.address;
    encWrite("set_address", { address }, "修改节点地址",
      "新地址立即生效，CAN ID 随之改变；记不住新地址就无法再通信。总线上应只有这一个编码器。",
      [`节点地址：${encHex(current)} → ${encHex(address)}（CAN ID 0x${address.toString(16).toUpperCase().padStart(3, "0")}）`,
       "写入后连接会自动跟随新地址并读回位置"]);
  });
  $("#encBitrateWrite")?.addEventListener("click", () => {
    const bitrate = Number($("#encNewBitrate").value);
    encWrite("set_bitrate", { bitrate }, "修改 CAN 位率",
      "写入后编码器按新位率通信，当前连接会收不到应答；需要以新位率重新连接，必要时给编码器重新上电。",
      [`位率：${encBitrateText(encSnapshot()?.connection?.bitrate)} → ${encBitrateText(bitrate)}`,
       "写入应答后会停止轮询，避免向无人应答的总线持续发帧"]);
  });
  $("#encSampleWrite")?.addEventListener("click", () => {
    const sampleMs = encParseInt("#encSampleMs", "角速度采样时间", 1, 65535);
    if (sampleMs == null) return;
    enc.prefs.sampleMs = sampleMs;
    encSavePrefs();
    encWrite("set_speed_time", { sample_ms: sampleMs }, "写入角速度采样时间",
      "采样时间写入编码器并掉电记忆。说明书示例的字节序与通用说明不一致，写入后用“读取角速度计数”核对。",
      [`采样时间：${sampleMs} ms`]);
  });
  $("#encCountWrite")?.addEventListener("click", () => {
    const count = encParseInt("#encCount", "位置计数", 0, 4095);
    if (count == null) return;
    encWrite("set_count", { count }, "设置当前位置计数",
      "编码器把当前位置改为指定计数，掉电后归零。写入后自动读回比较。",
      [`操作：当前位置 → ${count}（${(count * 360 / 4096).toFixed(3)}°）`]);
  });
  $("#encSampleMs")?.addEventListener("change", () => {
    const value = Number($("#encSampleMs").value);
    if (Number.isInteger(value) && value >= 1) { enc.prefs.sampleMs = value; encSavePrefs(); }
  });
  $("#encWindow")?.addEventListener("click", event => {
    const button = event.target.closest("button[data-window]");
    if (!button) return;
    enc.prefs.windowSec = Number(button.dataset.window);
    encSavePrefs();
    enc.drawKey = "";
    renderEncoder();
  });
  $("#encAngleMode")?.addEventListener("click", event => {
    const button = event.target.closest("button[data-mode]");
    if (!button) return;
    enc.prefs.angleMode = button.dataset.mode;
    encSavePrefs();
    enc.drawKey = "";
    renderEncoder();
  });
  $("#encPause")?.addEventListener("change", event => {
    enc.paused = event.target.checked;
    enc.drawKey = "";
    renderEncoder();
  });
  $("#encClearCurves")?.addEventListener("click", async () => {
    await encRun("clear_curves");
    encResetData();
  });
  $("#encExport")?.addEventListener("click", encExport);
  $("#encExportImage")?.addEventListener("click", encExportImage);
  $("#encClearLog")?.addEventListener("click", () => encRun("clear_log"));
  $("#encNoticeAction")?.addEventListener("click", () => enc.noticeAction?.());
  $("#encChannelSelect")?.addEventListener("change", () => { enc.prefs.channel = $("#encChannelSelect").value; encSavePrefs(); });
  $("#encBitrate")?.addEventListener("change", () => { enc.prefs.bitrate = Number($("#encBitrate").value); encSavePrefs(); });
  $("#encAddress")?.addEventListener("change", () => { enc.prefs.address = $("#encAddress").value.trim(); encSavePrefs(); });
}

/** bootstrap 到达后调用：按后端目录填充下拉并恢复上次的选择。 */
function initEncoder() {
  enc.catalog = state.bootstrap?.encoder || null;
  const catalog = enc.catalog || { bitrates: [500000], poll_rates_hz: [50] };
  const bitrateOptions = catalog.bitrates.map(rate => `<option value="${rate}">${encBitrateText(rate)}</option>`).join("");
  $("#encBitrate").innerHTML = bitrateOptions;
  $("#encNewBitrate").innerHTML = bitrateOptions;
  $("#encBitrate").value = String(catalog.bitrates.includes(enc.prefs.bitrate) ? enc.prefs.bitrate : 500000);
  $("#encNewBitrate").value = "250000";
  $("#encPollHz").innerHTML = catalog.poll_rates_hz.map(rate => `<option value="${rate}">${rate} Hz</option>`).join("");
  $("#encPollHz").value = String(catalog.poll_rates_hz.includes(enc.prefs.pollHz) ? enc.prefs.pollHz : 50);
  $("#encAddress").value = enc.prefs.address || "0x01";
  $("#encSampleMs").value = String(enc.prefs.sampleMs || 100);
  $("#encPreviewWrap")?.classList.toggle("hidden", state.bootstrap?.encoder_preview !== true);
  const [low, high] = catalog.period_us_range || [5000, 65535];
  $("#encPeriod").min = String(low);
  $("#encPeriod").max = String(high);
  restoreEncoderChannel();
  renderEncoderPeriodHint();
}

function restoreEncoderChannel() {
  const select = $("#encChannelSelect");
  if (select && enc.prefs.channel && [...select.options].some(option => option.value === enc.prefs.channel)) {
    select.value = enc.prefs.channel;
  }
}

async function encConnect() {
  if (!state.api) return toast("应用后端未就绪", true);
  const address = encParseAddress($("#encAddress").value);
  if (address == null) return;
  const preview = !$("#encPreviewWrap").classList.contains("hidden") && $("#encPreview").checked;
  const channel = $("#encChannelSelect").value;
  if (!preview && !channel) return toast("未检测到 PCAN 通道", true);
  const button = $("#encConnectButton");
  button.disabled = true;
  button.textContent = "连接中…";
  let result;
  try {
    result = await state.api.connect_encoder({ channel, bitrate: Number($("#encBitrate").value), address, preview });
  } finally {
    button.disabled = false;
    button.textContent = "连接";
  }
  if (!result?.ok) return toast(result?.error || "编码器连接失败", true);
  enc.prefs.channel = channel;
  enc.prefs.bitrate = Number($("#encBitrate").value);
  enc.prefs.address = encHex(address);
  encSavePrefs();
  encResetData();
  enc.reads = {};
  toast(result.message || "编码器已连接");
  await poll();
}

async function encDisconnect() {
  if (!state.api) return;
  await state.api.disconnect_encoder();
  toast("编码器已断开");
  await poll();
}

/* ---------------- 操作 ---------------- */

async function encRun(name, args = {}) {
  if (!state.api) return { ok: false, error: "应用后端未就绪" };
  const result = await state.api.encoder_command(name, args, false);
  if (!result?.ok) toast(result?.error || "操作失败", true);
  else if (result.message) toast(result.message);
  await poll();
  return result;
}

async function encRead(name) {
  const args = name === "read_speed" ? { sample_ms: Number($("#encSampleMs").value) || enc.prefs.sampleMs } : {};
  const result = await state.api.encoder_command(name, args, false);
  if (!result?.ok) toast(result?.error || "读取失败", true);
  else enc.reads[name] = result;
  await poll();
  return result;
}

async function encTogglePoll() {
  const polling = encSnapshot()?.stream?.polling === true;
  if (polling) return encRun("poll_stop");
  enc.prefs.pollHz = Number($("#encPollHz").value);
  encSavePrefs();
  return encRun("poll_start", { hz: enc.prefs.pollHz });
}

function encWrite(name, args, title, message, lines) {
  if (!encConnected()) return toast("请先连接编码器", true);
  const connection = encSnapshot().connection;
  state.pendingCommand = null;
  state.pendingFanCommand = null;
  state.pendingFanAction = null;
  state.pendingIvtAction = null;
  state.pendingMonitorAction = null;
  state.pendingEncoderAction = { run: () => state.api.encoder_command(name, args, true) };
  text("#confirmTitle", title);
  text("#confirmMessage", message);
  setConfirmModeBadge("编码器写入 · 需确认", "bad");
  text("#confirmPayload", [
    `通道：${connection.mode === "preview" ? "模拟编码器（预览）" : connection.channel} · ${encBitrateText(connection.bitrate)}`,
    `编码器：节点地址 ${encHex(connection.address)}`,
    ...lines,
  ].join("\n"));
  text("#confirmCheckLabel", "我已核对编码器连接和参数，本次写入由我确认。");
  $("#confirmCheck").checked = false;
  $("#doConfirm").disabled = true;
  $("#doConfirm").className = "danger-button";
  $("#confirmDialog").showModal();
}

async function encExport() {
  if (!state.api?.choose_export_encoder_csv) return;
  const result = await state.api.choose_export_encoder_csv();
  if (result?.cancelled) return;
  if (!result?.ok) return toast(result?.error || "导出失败", true);
  toast(`已导出 ${Number(result.samples).toLocaleString()} 个样本：${result.path}`);
}

/** 把窗口内的三张曲线拼成一张图：uPlot 的图例是 DOM，不在画布里，所以标题和油压图例由这里重画。 */
function encFigureCanvas() {
  const blocks = [
    { plot: enc.plots.angle, title: $("#encAngleTitle").textContent },
    { plot: enc.plots.oil, title: $("#encOilTitle").textContent, legend: true },
    { plot: enc.plots.speed, title: "转速 rpm" },
  ];
  if (blocks.some(block => !block.plot)) return null;
  const ratio = blocks[0].plot.ctx.canvas.width / blocks[0].plot.width;
  const pad = 16 * ratio, head = 84 * ratio, titleHeight = 26 * ratio;
  const width = Math.max(...blocks.map(block => block.plot.ctx.canvas.width)) + pad * 2;
  const height = head + blocks.reduce((sum, block) => sum + titleHeight + block.plot.ctx.canvas.height, 0) + pad;
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(width);
  canvas.height = Math.round(height);
  const ctx = canvas.getContext("2d");
  const textColor = cssVar("--text", "#e6ecef");
  const mutedColor = cssVar("--text-3", "#87969c");
  ctx.fillStyle = cssVar("--surface-1", "#151719");
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.textBaseline = "alphabetic";

  const connection = encSnapshot()?.connection || {};
  const source = connection.mode === "preview" ? "模拟编码器（预览，非实测）" : `节点 ${encHex(connection.address ?? 1)}`;
  const span = enc.prefs.windowSec > 0 ? `最近 ${enc.prefs.windowSec} s` : "全部样本";
  const map = enc.catalog?.brake_map;
  ctx.fillStyle = textColor;
  ctx.font = `600 ${16 * ratio}px sans-serif`;
  ctx.fillText("BRT 编码器 · 制动踏板转角与制动油压", pad, pad + 14 * ratio);
  ctx.fillStyle = mutedColor;
  ctx.font = `${12 * ratio}px sans-serif`;
  const stamp = new Date().toLocaleString("sv-SE").replace("T", " ");
  ctx.fillText(`${source} · ${span}（${fmt(enc.view.min, 1)}–${fmt(enc.view.max, 1)} s，相对首个样本）· ${stamp}`, pad, pad + 33 * ratio);
  if (map) {
    ctx.fillText(`油压开度按车手屏刹车条算法：${fmt(map.zero_kpa, 1)} kPa → 0 %，${fmt(map.full_kpa, 1)} kPa → ${map.full_pct} %，线性并限幅到 100 %`,
      pad, pad + 49 * ratio);
  }

  let y = head;
  for (const block of blocks) {
    ctx.fillStyle = textColor;
    ctx.font = `600 ${13 * ratio}px sans-serif`;
    const titleWidth = ctx.measureText(block.title).width;
    ctx.fillText(block.title, pad, y + 17 * ratio);
    if (block.legend) {
      const kpa = cssVar(ENC_SERIES_VARS.kpa, "#b392f0");
      const pct = cssVar(ENC_SERIES_VARS.pct, "#3ecf8e");
      let x = pad + titleWidth + 24 * ratio;
      ctx.font = `${12 * ratio}px sans-serif`;
      for (const [color, dash, label] of [[kpa, [], "制动油压（左轴）"], [pct, [6 * ratio, 4 * ratio], "油压开度（右轴）"]]) {
        ctx.strokeStyle = color;
        ctx.lineWidth = 2 * ratio;
        ctx.setLineDash(dash);
        ctx.beginPath();
        ctx.moveTo(x, y + 13 * ratio);
        ctx.lineTo(x + 26 * ratio, y + 13 * ratio);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = mutedColor;
        ctx.fillText(label, x + 32 * ratio, y + 17 * ratio);
        x += 32 * ratio + ctx.measureText(label).width + 20 * ratio;
      }
    }
    ctx.drawImage(block.plot.ctx.canvas, pad, y + titleHeight);
    y += titleHeight + block.plot.ctx.canvas.height;
  }
  return canvas;
}

async function encExportImage() {
  if (!state.api?.choose_export_encoder_image) return;
  const canvas = encFigureCanvas();
  if (!canvas) return toast("曲线里还没有数据可导出", true);
  const result = await state.api.choose_export_encoder_image(canvas.toDataURL("image/png"));
  if (result?.cancelled) return;
  if (!result?.ok) return toast(result?.error || "导出失败", true);
  toast(`已导出曲线图：${result.path}`);
}

/* ---------------- 曲线数据 ---------------- */

function encClearColumns(data) {
  data.t = []; data.raw = []; data.pos = []; data.spd = []; data.oil = []; data.brk = [];
}

function encResetData() {
  const data = enc.data;
  data.epoch = -1;
  data.cursor = 0;
  encClearColumns(data);
  enc.origin = null;
  enc.drawKey = "";
}

/** 轮询里调用：按游标增量取回样本，界面刷新间隔不影响采样密度。 */
async function encFetchSeries() {
  if (enc.fetching || !state.api?.get_encoder_series) return;
  enc.fetching = true;
  try {
    const data = enc.data;
    const reply = await state.api.get_encoder_series(data.cursor, data.epoch);
    if (!reply?.ok) return;
    if (reply.reset || reply.epoch !== data.epoch) {
      encClearColumns(data);
      enc.origin = null;
    }
    data.epoch = reply.epoch;
    data.cursor = reply.cursor;
    for (let index = 0; index < reply.t.length; index += 1) {
      data.t.push(reply.t[index]);
      data.raw.push(reply.raw[index]);
      data.pos.push(reply.position[index]);
      data.spd.push(reply.speed[index]);
      data.oil.push(reply.oil_kpa[index]);
      data.brk.push(reply.brake_pct[index]);
    }
    if (enc.origin === null && data.t.length) enc.origin = data.t[0];
    if (data.t.length > ENC_STORE_CAP) {
      const drop = data.t.length - ENC_STORE_CAP;
      for (const column of [data.t, data.raw, data.pos, data.spd, data.oil, data.brk]) column.splice(0, drop);
    }
  } finally {
    enc.fetching = false;
  }
}

function encBisect(times, value) {
  let low = 0, high = times.length;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (times[mid] < value) low = mid + 1; else high = mid;
  }
  return low;
}

/** 取窗口内的样本并抽稀；单圈角度在跨圈处插入空值，避免画出 360°→0° 的竖线。 */
function encPlotTables() {
  const data = enc.data;
  const count = data.t.length;
  if (!count) return null;
  const latest = data.t[count - 1];
  const from = enc.prefs.windowSec > 0 ? latest - enc.prefs.windowSec : data.t[0];
  const start = encBisect(data.t, from);
  const stride = Math.max(1, Math.ceil((count - start) / ENC_PLOT_POINTS));
  const origin = enc.origin ?? data.t[0];
  const mode = enc.prefs.angleMode;
  const wrapped = mode !== "accum";
  const degrees = 360 / 4096;
  const angleX = [], angleY = [], speedX = [], speedY = [], oilY = [], brakeY = [];
  let oilCount = 0;
  let previousAngle = null, previousX = null;
  for (let index = start; index < count; index += stride) {
    const x = data.t[index] - origin;
    let angle = (wrapped ? data.raw[index] : data.pos[index]) * degrees;
    if (mode === "signed" && angle > 180) angle -= 360;
    if (wrapped && previousAngle !== null && Math.abs(angle - previousAngle) > 180) {
      angleX.push((x + previousX) / 2);
      angleY.push(null);
    }
    angleX.push(x); angleY.push(angle);
    previousAngle = angle; previousX = x;
    speedX.push(x);
    speedY.push(data.spd[index] == null ? null : data.spd[index] / 6);
    // 没有新鲜油压的样本留空，不用 0 或旧值补。
    const oil = data.oil[index] ?? null;
    oilY.push(oil);
    brakeY.push(data.brk[index] ?? null);
    if (oil !== null) oilCount += 1;
  }
  return {
    angle: [angleX, angleY], speed: [speedX, speedY], oil: [speedX, oilY, brakeY], oilCount,
    from: Math.max(from - origin, 0), latest: latest - origin,
  };
}

function encPlotDestroy() {
  for (const key of ["angle", "oil", "speed"]) {
    if (enc.plots[key]) { enc.plots[key].destroy(); enc.plots[key] = null; }
  }
  enc.plotKey = "";
  enc.drawKey = "";
}

function encDrawMarkers(plot) {
  if (!enc.markers.length || enc.origin === null) return;
  const { left, top, width, height } = plot.bbox;
  const ctx = plot.ctx;
  ctx.save();
  ctx.strokeStyle = cssVar("--warn", "#f0b429");
  ctx.fillStyle = cssVar("--warn", "#f0b429");
  ctx.lineWidth = Math.max(1, window.devicePixelRatio || 1);
  ctx.setLineDash([4, 4]);
  ctx.font = `${Math.round(11 * (window.devicePixelRatio || 1))}px sans-serif`;
  for (const marker of enc.markers) {
    const x = plot.valToPos(marker.t - enc.origin, "x", true);
    if (!Number.isFinite(x) || x < left || x > left + width) continue;
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, top + height);
    ctx.stroke();
    ctx.fillText(marker.text, x + 4, top + 12 * (window.devicePixelRatio || 1));
  }
  ctx.restore();
}

function encPlotOptions(kind, width) {
  const axisColor = cssVar("--chart-axis", "#87969c");
  const gridColor = cssVar("--chart-grid", "#2d2f31");
  const color = cssVar(ENC_SERIES_VARS[kind], "#4ad6ec");
  const mode = kind === "angle" ? enc.prefs.angleMode : "";
  const span = mode === "single" ? [0, 360] : mode === "signed" ? [-180, 180] : null;
  const number = (value, digits) => (value == null ? "—" : Number(value).toFixed(digits));
  return {
    width, height: 190,
    scales: {
      x: { time: false, range: () => [enc.view.min, enc.view.max] },
      y: span ? { range: [span[0] - 8, span[1] + 8] } : { range: (_u, low, high) => {
        const pad = Math.max((high - low) * 0.08, kind === "angle" ? 1 : 0.5);
        return [low - pad, high + pad];
      } },
    },
    axes: [
      { stroke: axisColor, grid: { stroke: gridColor, width: 1 }, ticks: { stroke: gridColor }, font: "11px monospace",
        values: (_u, splits) => splits.map(value => Number(value.toFixed(2))) },
      { stroke: axisColor, grid: { stroke: gridColor, width: 1 }, ticks: { stroke: gridColor }, font: "11px monospace", size: 56,
        values: span ? (_u, splits) => splits.map(value => (value >= span[0] && value <= span[1] ? value : "")) : undefined },
    ],
    legend: { show: true },
    cursor: { drag: { x: false, y: false }, points: { size: 6 }, sync: { key: "enc-plots" } },
    series: [
      { label: "时间 s", value: (_u, value) => number(value, 3) },
      { label: kind === "angle" ? (mode === "accum" ? "累计角度 °" : mode === "signed" ? "角度 ±180°" : "单圈角度 °") : "转速 rpm", stroke: color, width: 1.6,
        spanGaps: false, points: { show: false }, value: (_u, value) => number(value, kind === "angle" ? 3 : 2) },
    ],
    hooks: { draw: [encDrawMarkers] },
  };
}

/** 油压与开度共用一张图：左轴 kPa，右轴 %。 */
function encOilPlotOptions(width) {
  const axisColor = cssVar("--chart-axis", "#87969c");
  const gridColor = cssVar("--chart-grid", "#2d2f31");
  const number = (value, digits) => (value == null ? "—" : Number(value).toFixed(digits));
  const axis = extra => ({ stroke: axisColor, grid: { stroke: gridColor, width: 1 }, ticks: { stroke: gridColor }, font: "11px monospace", ...extra });
  return {
    width, height: 190,
    scales: {
      x: { time: false, range: () => [enc.view.min, enc.view.max] },
      kpa: { range: (_u, low, high) => {
        if (low == null || high == null) return [0, 6];
        const pad = Math.max((high - low) * 0.08, 0.2);
        return [Math.max(0, low - pad), high + pad];
      } },
      pct: { range: [-4, 104] },
    },
    axes: [
      axis({ values: (_u, splits) => splits.map(value => Number(value.toFixed(2))) }),
      axis({ scale: "kpa", size: 56 }),
      axis({ scale: "pct", side: 1, size: 48, grid: { show: false }, values: (_u, splits) => splits.map(value => (value >= 0 && value <= 100 ? value : "")) }),
    ],
    legend: { show: true },
    cursor: { drag: { x: false, y: false }, points: { size: 6 }, sync: { key: "enc-plots" } },
    series: [
      { label: "时间 s", value: (_u, value) => number(value, 3) },
      { label: "制动油压 kPa", scale: "kpa", stroke: cssVar(ENC_SERIES_VARS.kpa, "#b392f0"), width: 1.6, spanGaps: false,
        points: { show: false }, value: (_u, value) => number(value, 3) },
      { label: "油压开度 %", scale: "pct", stroke: cssVar(ENC_SERIES_VARS.pct, "#3ecf8e"), width: 1.6, dash: [6, 4], spanGaps: false,
        points: { show: false }, value: (_u, value) => number(value, 0) },
    ],
    hooks: { draw: [encDrawMarkers] },
  };
}

function encDrawPlots() {
  const hostAngle = $("#encPlotAngle");
  const hostOil = $("#encPlotOil");
  const hostSpeed = $("#encPlotSpeed");
  if (!hostAngle || !hostOil || !hostSpeed || hostAngle.clientWidth < 50) return;
  const tables = encPlotTables();
  if (!tables) { encPlotDestroy(); return; }
  const width = Math.max(320, hostAngle.clientWidth);
  const structure = JSON.stringify([document.documentElement.dataset.theme, width, enc.prefs.angleMode]);
  text("#encOilTitle", tables.oilCount ? "制动油压 kPa · 油压开度 %" : "制动油压 kPa · 油压开度 % · 窗口内没有油压数据");
  const lastSample = enc.data.t.length;
  const drawKey = `${lastSample}|${enc.prefs.windowSec}|${enc.prefs.angleMode}|${enc.markers.length}`;
  enc.view.min = tables.from;
  enc.view.max = Math.max(tables.latest, tables.from + 1);
  if (structure !== enc.plotKey || !enc.plots.angle) {
    encPlotDestroy();
    hostAngle.innerHTML = ""; hostOil.innerHTML = ""; hostSpeed.innerHTML = "";
    enc.plots.angle = new uPlot(encPlotOptions("angle", width), tables.angle, hostAngle);
    enc.plots.oil = new uPlot(encOilPlotOptions(width), tables.oil, hostOil);
    enc.plots.speed = new uPlot(encPlotOptions("speed", width), tables.speed, hostSpeed);
    enc.plotKey = structure;
    enc.drawKey = drawKey;
    return;
  }
  if (drawKey === enc.drawKey) return;
  enc.drawKey = drawKey;
  enc.plots.angle.setData(tables.angle);
  enc.plots.oil.setData(tables.oil);
  enc.plots.speed.setData(tables.speed);
}

/** 主题切换后由 core.js 调用，下一次绘制重建图表以取新配色。 */
function encPlotsRebuild() {
  encPlotDestroy();
  if (state.page === "encoder") encDrawPlots();
}

/* ---------------- 渲染 ---------------- */

function renderEncoder() {
  const snapshot = encSnapshot();
  const connection = snapshot?.connection || {};
  const connected = connection.connected === true;
  const stream = snapshot?.stream || {};
  const reading = snapshot?.reading || null;
  enc.markers = snapshot?.markers || [];

  renderEncoderConnection(connection, connected);
  renderEncoderReading(reading, stream, snapshot?.direction, connected);
  renderEncoderSource(stream, connected);
  renderEncoderBrake(snapshot?.oil || null, connected);
  renderEncoderNotice(connection, stream, reading, connected);
  renderEncoderCalibration(snapshot, connected);
  renderEncoderQueries(snapshot);
  renderEncoderTables(snapshot);
  renderEncoderPeriodHint();
  renderEncoderPlots(connected, stream);
}

function renderEncoderConnection(connection, connected) {
  const status = $("#encStatus");
  const stateName = connection.state || "disconnected";
  if (connected) {
    status.className = "tag " + (connection.mode === "preview" ? "warn" : "ok");
    status.textContent = connection.mode === "preview"
      ? `预览 · 模拟编码器 · ${encHex(connection.address)}`
      : `已连接 · ${connection.channel} · ${encBitrateText(connection.bitrate)} · ${encHex(connection.address)}`;
  } else if (stateName === "error") {
    status.className = "tag bad";
    status.textContent = "连接中断";
  } else {
    status.className = "tag neutral";
    status.textContent = "未连接";
  }
  const active = connected || stateName === "error";
  $("#encConnectButton").classList.toggle("hidden", active);
  $("#encDisconnectButton").classList.toggle("hidden", !active);
  for (const id of ["#encChannelSelect", "#encBitrate", "#encAddress", "#encPreview"]) {
    const node = $(id);
    if (node) node.disabled = active;
  }
  if (connected && document.activeElement !== $("#encAddress")) $("#encAddress").value = encHex(connection.address);
  const writeIds = ["#encPollToggle", "#encPollHz", "#encReportStart", "#encReportStop", "#encPeriodWrite", "#encZero",
    "#encMidpoint", "#encReadAll", "#encAddressWrite", "#encBitrateWrite", "#encSampleWrite", "#encCountWrite"];
  for (const id of writeIds) { const node = $(id); if (node) node.disabled = !connected; }
  $$("[data-enc-direction], [data-enc-read]").forEach(button => { button.disabled = !connected; });
}

function renderEncoderReading(reading, stream, direction, connected) {
  const tag = $("#encReadingState");
  const tiles = $$(".enc-tile");
  const limit = enc.catalog?.fresh_max_s ?? 1.5;
  if (!reading) {
    tag.className = "tag neutral";
    tag.textContent = connected ? "等待数据" : "未连接";
    for (const id of ["#encAngle", "#encRaw", "#encRpm", "#encAccum", "#encMotion", "#encRate"]) text(id, "—");
    text("#encAngleSub", "0–360°");
    text("#encRawSub", "/ 4095");
    text("#encDps", "°/s");
    text("#encTurns", "上位机累计");
    text("#encMotionSub", "\u00a0");
    text("#encRateSub", "\u00a0");
    tiles.forEach(node => node.classList.remove("data-stale"));
    return;
  }
  const stale = isStaleData(reading.age, limit);
  tag.className = "tag " + (stale ? "warn" : "ok");
  tag.textContent = stale ? dataAgeText(reading.age, limit) : "实时";
  tiles.forEach(node => node.classList.toggle("data-stale", stale));
  text("#encAngle", fmt(reading.angle_deg, 3) + " °");
  text("#encAngleSub", `0–360° · ${fmt(360 / 4096, 4)}°/count`);
  text("#encRaw", String(reading.raw));
  text("#encRawSub", `/ 4095 · 0x${reading.raw.toString(16).toUpperCase().padStart(3, "0")}`);
  text("#encRpm", reading.speed_rpm == null ? "—" : fmt(reading.speed_rpm, 2) + " rpm");
  text("#encDps", reading.speed_dps == null ? "°/s" : `${fmt(reading.speed_dps, 1)} °/s`);
  text("#encAccum", fmt(reading.accumulated_deg, 2) + " °");
  text("#encTurns", `${fmt(reading.turns, 3)} 圈 · 上位机累计`);
  text("#encMotion", ENC_MOTION_NAMES[reading.motion] || "—");
  const recent = direction ? ENC_MOTION_NAMES[direction.label] : null;
  text("#encMotionSub", reading.motion === "still" && recent ? `最近转动：${recent} · ${fmt(direction.age, 0)} s 前` : "\u00a0");
  if (stream.hz > 0) {
    text("#encRate", `${fmt(stream.hz, stream.hz >= 100 ? 0 : 1)} Hz`);
    text("#encRateSub", stream.interval_ms == null ? "\u00a0" : `${fmt(stream.interval_ms, 1)} ms · 最大 ${fmt(stream.max_gap_ms, 1)}`);
  } else {
    text("#encRate", "—");
    text("#encRateSub", "没有新的位置帧");
  }
}

function renderEncoderBrake(oil, connected) {
  const tag = $("#encBrakeState");
  const tiles = ["#encOilTile", "#encBrakeTile", "#encOilRateTile"].map(id => $(id));
  const map = enc.catalog?.brake_map;
  const limit = map?.fresh_s ?? 1;
  text("#encBrakeMap", map
    ? `油压开度沿用车手屏刹车条算法：${fmt(map.zero_kpa, 1)} kPa 及以下为 0 %，${fmt(map.full_kpa, 1)} kPa 为 ${map.full_pct} %，线性并限幅到 100 %`
    : "\u00a0");
  if (!oil) {
    tag.className = "tag neutral";
    tag.textContent = connected ? "等待数据" : "未连接";
    for (const id of ["#encOil", "#encBrakePct", "#encOilRate"]) text(id, "—");
    text("#encOilSub", connected ? "没有收到 CANB 0x305" : "\u00a0");
    text("#encBrakeSub", "\u00a0");
    text("#encOilRateSub", "\u00a0");
    tiles.forEach(node => node.classList.remove("data-stale"));
    return;
  }
  const stale = isStaleData(oil.age, limit);
  tag.className = "tag " + (stale ? "warn" : "ok");
  tag.textContent = stale ? dataAgeText(oil.age, limit) : "实时";
  tiles.forEach(node => node.classList.toggle("data-stale", stale));
  text("#encOil", fmt(oil.kpa, 3) + " kPa");
  text("#encOilSub", `原始 ${oil.raw} · 0.001 kPa/LSB`);
  text("#encBrakePct", `${oil.brake_pct} %`);
  text("#encBrakeSub", "车手屏刹车条");
  text("#encOilRate", oil.hz > 0 ? `${fmt(oil.hz, oil.hz >= 100 ? 0 : 1)} Hz` : "—");
  text("#encOilRateSub", `距今 ${fmt(oil.age, 1)} s`);
}

function renderEncoderSource(stream, connected) {
  const tag = $("#encSourceState");
  if (!connected) { tag.className = "tag neutral"; tag.textContent = "未采集"; }
  else if (stream.polling) { tag.className = "tag ok"; tag.textContent = `轮询 ${stream.poll_hz} Hz`; }
  else if (stream.report_on) { tag.className = "tag ok"; tag.textContent = "自动上报"; }
  else if (stream.hz > 0) { tag.className = "tag warn"; tag.textContent = "设备正在上报"; tag.title = "编码器主动发送位置，不是本次连接开启的"; }
  else { tag.className = "tag neutral"; tag.textContent = "未采集"; tag.title = ""; }
  const toggle = $("#encPollToggle");
  toggle.textContent = stream.polling ? "停止" : "开始";
  toggle.className = stream.polling ? "danger-outline" : "action-button";
  const wanted = String(stream.polling ? stream.poll_hz : enc.prefs.pollHz);
  if ($("#encPollHz").value !== wanted) $("#encPollHz").value = wanted;
}

function renderEncoderPeriodHint() {
  const node = $("#encPeriodHint");
  if (!node) return;
  const period = Number($("#encPeriod")?.value);
  const [low, high] = enc.catalog?.period_us_range || [5000, 65535];
  if (!Number.isInteger(period) || period < low || period > high) {
    node.textContent = `${low}–${high} µs，掉电记忆`;
    node.classList.remove("enc-warn-text");
    return;
  }
  const bitrate = encSnapshot()?.connection?.bitrate || Number($("#encBitrate")?.value) || 500000;
  const frames = 1e6 / period;
  const load = frames * ENC_FRAME_BITS / bitrate * 100;
  const rpm = 30e6 / period;
  node.textContent = `约 ${frames >= 100 ? frames.toFixed(0) : frames.toFixed(1)} 帧/s · 占用约 ${load.toFixed(load < 10 ? 1 : 0)}% · `
    + `可跟踪 ${rpm >= 1000 ? Math.round(rpm).toLocaleString() : rpm.toFixed(0)} rpm 以内`;
  node.classList.toggle("enc-warn-text", load > 50);
}

function renderEncoderNotice(connection, stream, reading, connected) {
  const notice = $("#encNotice");
  const action = $("#encNoticeAction");
  let message = null, level = "warn", actionLabel = null;
  enc.noticeAction = null;
  if (connection.state === "error") {
    message = connection.error || "PCAN 连接中断";
    level = "bad";
  } else if (connected && connection.pending_bitrate) {
    message = `位率 ${encBitrateText(connection.pending_bitrate)} 已写入编码器；以新位率重新连接，必要时给编码器重新上电。`;
    actionLabel = `以 ${encBitrateText(connection.pending_bitrate)} 重连`;
    enc.noticeAction = async () => {
      const bitrate = connection.pending_bitrate;
      await encDisconnect();
      $("#encBitrate").value = String(bitrate);
      await encConnect();
    };
  } else if (connected && (stream.polling || stream.report_on) && (stream.silent_s == null || stream.silent_s > (enc.catalog?.fresh_max_s ?? 1.5))) {
    message = stream.silent_s == null
      ? "没有收到编码器的任何帧：核对位率、节点地址、接线和总线终端电阻。"
      : `编码器已 ${fmt(stream.silent_s, 1)} s 没有应答：核对接线和供电。`;
  } else if (connected && !stream.polling && stream.hz > 0 && stream.interval_ms != null && stream.interval_ms < 4.5) {
    message = `编码器正以约 ${fmt(stream.interval_ms, 1)} ms 的间隔上报，低于 5 ms：厂商提示这会堵塞总线并使编码器不再响应配置指令。立即点“关闭”，再写入不低于 5000 µs 的周期。`;
    level = "bad";
  } else if (connected && stream.rejected > 0) {
    message = `${stream.rejected} 帧被拒收（${stream.last_reject || "格式不符"}）；到“收到的帧”核对实际布局。`;
  } else if (connected && reading?.speed_rpm != null && stream.max_rpm && Math.abs(reading.speed_rpm) > stream.max_rpm * 0.8) {
    message = `转速已接近当前上报间隔可跟踪的上限（约 ${Math.round(stream.max_rpm).toLocaleString()} rpm），累计角度会失真；缩短上报周期。`;
  }
  notice.classList.toggle("hidden", !message);
  notice.classList.toggle("bad", level === "bad");
  text("#encNoticeText", message || "");
  action.classList.toggle("hidden", !actionLabel);
  if (actionLabel) action.textContent = actionLabel;
}

function encRecordText(record) {
  if (!record) return { text: "本次连接未执行", level: "" };
  const detail = record.detail ? ` · ${record.detail}` : "";
  const level = record.verified === true ? "ok" : record.verified === false ? "warn" : "";
  return { text: `${record.time} · ${record.value}${detail}`, level };
}

function renderEncoderCalibration(snapshot, connected) {
  const applied = Object.fromEntries((snapshot?.applied || []).map(item => [item.key, item]));
  for (const [id, key] of [["#encRecZero", "zero"], ["#encRecMidpoint", "midpoint"], ["#encRecDirection", "direction"]]) {
    const { text: line, level } = encRecordText(applied[key]);
    const node = $(id);
    node.textContent = line;
    node.className = "enc-record" + (level ? " " + level : "");
  }
  const stability = snapshot?.stability;
  const tag = $("#encStabilityTag");
  if (!connected || !stability || stability.samples < 2) {
    tag.className = "tag neutral";
    tag.textContent = connected ? "等待数据" : "未连接";
    text("#encStabSamples", "—"); text("#encStabPtp", "—"); text("#encStabStd", "—");
    return;
  }
  tag.className = "tag " + (stability.moving ? "warn" : "ok");
  tag.textContent = stability.moving ? "转动中" : "静止";
  text("#encStabSamples", `${stability.samples.toLocaleString()} · ${fmt(stability.window_s, 0)} s`);
  text("#encStabPtp", `${stability.ptp_counts} count · ${fmt(stability.ptp_deg, 3)}°`);
  text("#encStabStd", `${fmt(stability.std_counts, 2)} count`);
}

function encDeviceText(value, age) {
  if (!value) return "—";
  const signed = value.signed !== value.unsigned ? ` · int32 ${value.signed}` : "";
  return `${value.unsigned}${signed}` + (age == null ? "" : ` · ${fmt(age, 0)} s 前`);
}

function renderEncoderQueries(snapshot) {
  const device = snapshot?.device || {};
  const position = enc.reads.read_position;
  text("#encQPos", position ? `${position.raw} · ${fmt(position.angle_deg, 3)}°` : "—");
  text("#encQMulti", encDeviceText(device.multiturn, device.multiturn?.age));
  text("#encQTurns", encDeviceText(device.turns, device.turns?.age));
  const speed = device.speed;
  text("#encQSpeed", !speed ? "—" : `${speed.signed}` + (speed.rpm == null ? "" : ` · ${fmt(speed.rpm, 2)} rpm（${speed.sample_ms} ms）`)
    + (speed.age == null ? "" : ` · ${fmt(speed.age, 0)} s 前`));
}

function renderEncoderTables(snapshot) {
  const applied = snapshot?.applied || [];
  encPaint("applied", $("#encApplied"), applied.length
    ? "<table class=\"enc-table\"><thead><tr><th>项目</th><th>写入值</th><th>核对</th><th>时间</th></tr></thead><tbody>"
      + applied.map(item => {
        const check = item.verified === true ? "<span class=\"enc-ok\">读回一致</span>"
          : item.verified === false ? "<span class=\"enc-bad\">读回不符</span>" : "<span class=\"enc-muted\">仅应答</span>";
        return `<tr><td>${escapeHtml(item.label)}</td><td>${escapeHtml(item.value)}<small>${escapeHtml(item.detail || "")}</small></td><td>${check}</td><td>${escapeHtml(item.time)}</td></tr>`;
      }).join("") + "</tbody></table>"
    : "<div class=\"enc-empty\">本次连接还没有写入。</div>");

  const frames = snapshot?.frames || [];
  const mine = frames.filter(item => item.mine);
  const other = frames.length - mine.length;
  const stream = snapshot?.stream || {};
  text("#encFrameSummary", frames.length
    ? `编码器 ${mine.length} 种 · 其他节点 ${other} 种 · 拒收 ${stream.rejected || 0}` : "—");
  encPaint("frames", $("#encFrames"), frames.length
    ? "<table class=\"enc-table\"><thead><tr><th>CAN ID</th><th>DLC</th><th>FUNC</th><th>识别</th><th>数量</th><th>最近一帧</th><th>距今</th></tr></thead><tbody>"
      + frames.map(item => {
        const id = "0x" + item.id.toString(16).toUpperCase().padStart(item.extended ? 8 : 3, "0");
        const func = item.func == null ? "—" : encHex(item.func);
        const kind = ENC_KIND_NAMES[item.kind] || item.kind;
        return `<tr class="${item.mine ? "" : "enc-muted-row"}"><td>${id}</td><td>${item.dlc}</td><td>${func}</td><td>${escapeHtml(kind)}</td>`
          + `<td>${item.count.toLocaleString()}</td><td class="enc-hex">${escapeHtml(item.data)}</td><td>${fmt(item.age, 1)} s</td></tr>`;
      }).join("")
      + "</tbody></table>"
      + (stream.last_reject ? `<p class="enc-hint enc-warn-text">最近一次拒收：${escapeHtml(stream.last_reject)}</p>` : "")
    : "<div class=\"enc-empty\">连接后列出总线上收到的帧。</div>");

  const events = snapshot?.events || [];
  encPaint("events", $("#encEvents"), events.length
    ? events.map(item => `<div class="enc-event ${escapeHtml(item.kind)}"><time>${escapeHtml(item.time)}</time><span>${escapeHtml(item.text)}</span></div>`).join("")
    : "<div class=\"enc-empty\">暂无事件。</div>");
}

function renderEncoderPlots(connected, stream) {
  const hasData = enc.data.t.length > 0;
  const empty = $("#encPlotEmpty");
  empty.classList.toggle("hidden", hasData);
  $$(".enc-plot-block").forEach(node => node.classList.toggle("hidden", !hasData));
  if (!hasData) {
    empty.textContent = !connected ? "连接编码器后选择数据来源，曲线从这里开始。"
      : (stream.polling || stream.report_on) ? "等待编码器数据…"
      : "已连接。在“数据来源”选择主机轮询或设备自动上报开始采集。";
    encPlotDestroy();
  }
  $$("#encWindow button").forEach(button => button.classList.toggle("active", Number(button.dataset.window) === enc.prefs.windowSec));
  $$("#encAngleMode button").forEach(button => button.classList.toggle("active", button.dataset.mode === enc.prefs.angleMode));
  text("#encAngleTitle", { single: "单圈角度 °", signed: "单圈角度 °（−180～180）", accum: "累计角度 °（上位机累计）" }[enc.prefs.angleMode]);
  $("#encExport").disabled = !hasData;
  $("#encExportImage").disabled = !hasData || !enc.plots.angle;
  $("#encClearCurves").disabled = !hasData;
  if (hasData && !(enc.paused && enc.plots.angle)) encDrawPlots();
}
