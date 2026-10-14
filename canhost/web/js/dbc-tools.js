/* DBC 工作台·工具：按信号发送与总线对账。
 * 发送只负责把信号值编码成数据，真正的发送仍走 CAN 监视器的原始帧接口：
 * 同样的确认弹窗、同样的受保护 ID 拒绝、模拟与回放不可写。 */

/* ---------- 按信号发送 ---------- */

const dbcSend = { key: null, source: null, revision: -1, form: null, values: {}, cycle: 100, payload: null, error: "", timer: null, loading: false, token: 0 };

function dbcSendKey() { return dbcSend.key; }

function dbcSendWritable() {
  const connection = dbcLiveFor()?.connection;
  return connection?.connected === true && connection.mode === "pcan";
}

function dbcSendUid() { return `dbc:${dbcUi.source}:${dbcSend.key}`; }

function dbcSendTask() {
  const snapshot = dbcUi.source === "vehicle" ? state.vehicleSnapshot?.monitor : state.mainSnapshot?.monitor;
  return (snapshot?.periodic || []).find(task => task.id === dbcSendUid()) || null;
}

async function dbcSendLoadForm() {
  if (!dbcSend.key || dbcSend.loading) return;
  dbcSend.loading = true;
  const key = dbcSend.key, source = dbcUi.source;
  try {
    const reply = await state.api.dbc_form(source, key);
    if (key !== dbcSend.key || source !== dbcUi.source) return;
    if (!reply?.ok) { dbcSend.form = null; dbcSend.error = reply?.error || "无法读取报文"; }
    else {
      dbcSend.form = reply.message;
      dbcSend.values = {};
      for (const signal of reply.message.signals) {
        const initial = reply.values[signal.name];
        dbcSend.values[signal.name] = dbcSendInitialValue(signal, initial);
      }
      dbcSend.error = "";
      dbcSendEncode();
    }
    dbcSend.source = source;
    dbcSend.revision = dbcLiveFor(source)?.revision ?? -1;
    dbcUi.signatures.sender = "";
  } finally { dbcSend.loading = false; }
  renderDbcSender();
}

function dbcSendInitialValue(signal, physical) {
  if (!signal.choices.length) return physical;
  const raw = Math.round((physical - signal.offset) / (signal.factor || 1));
  return signal.choices.find(choice => choice.value === raw)?.label ?? physical;
}

function dbcSendEncode() {
  clearTimeout(dbcSend.timer);
  dbcSend.timer = setTimeout(async () => {
    const token = ++dbcSend.token;
    const reply = await state.api.dbc_encode(dbcUi.source, dbcSend.key, dbcSend.values);
    if (token !== dbcSend.token) return;
    dbcSend.payload = reply?.ok ? reply : null;
    dbcSend.error = reply?.ok ? "" : (reply?.error || "编码失败");
    dbcUi.signatures.sendPayload = "";
    renderDbcSendPayload();
  }, 120);
}

function renderDbcSender() {
  const root = $("#dbcSender");
  const live = dbcLiveFor();
  const messages = (live?.messages || []);
  if (!messages.length) {
    dbcPaint("sender", root, '<div class="empty-state large">当前数据源的总线上没有绑定任何 DBC。在左侧文件卡里把 DBC 绑定到 CAN1 或 CANB 后再来发送。</div>');
    return;
  }
  if (!dbcSend.key || !messages.some(message => message.key === dbcSend.key)) {
    const preferred = messages.find(message => message.key === dbcUi.selected.key);
    dbcSend.key = (preferred || messages[0]).key;
    dbcSend.form = null;
  }
  if ((!dbcSend.form || dbcSend.source !== dbcUi.source || dbcSend.revision !== live.revision) && !dbcSend.loading) {
    dbcSendLoadForm();
  }
  const form = dbcSend.form;
  const options = [...messages].sort((a, b) => a.id - b.id).map(message =>
    `<option value="${message.key}" ${message.key === dbcSend.key ? "selected" : ""}>${escapeHtml(message.id_text)} · ${escapeHtml(message.name)}</option>`).join("");
  const writable = dbcSendWritable();
  const signature = JSON.stringify([dbcSend.key, dbcUi.source, form?.name, writable, live.revision, dbcSend.error && !form]);
  if (dbcUi.signatures.sender !== signature) {
    dbcUi.signatures.sender = signature;
    const rows = (form?.signals || []).map(signal => {
      const value = dbcSend.values[signal.name];
      let input;
      if (signal.choices.length) {
        const known = signal.choices.some(choice => choice.label === value);
        input = `<select data-sig="${escapeHtml(signal.name)}">${known ? "" : `<option value="${escapeHtml(String(value))}" selected>数值 ${escapeHtml(dbcNumber(value))}</option>`}`
          + signal.choices.map(choice => `<option value="${escapeHtml(choice.label)}" ${choice.label === value ? "selected" : ""}>${escapeHtml(choice.label)} (${choice.value})</option>`).join("") + "</select>";
      } else {
        input = `<input data-sig="${escapeHtml(signal.name)}" value="${escapeHtml(dbcNumber(value))}" inputmode="decimal" aria-label="${escapeHtml(signal.name)}">`;
      }
      const range = signal.minimum === 0 && signal.maximum === 0 ? "" : `${dbcNumber(signal.minimum)} … ${dbcNumber(signal.maximum)}`;
      const mux = signal.mux.role === "multiplexor" ? " · 选择信号" : signal.mux.role === "multiplexed" ? ` · 仅当选择值=${signal.mux.value}` : "";
      return `<div class="dbc-send-row"><label title="${escapeHtml(signal.comment)}">${escapeHtml(signal.name)}<br><small>${escapeHtml(range)}${escapeHtml(mux)}</small></label>${input}<small>${escapeHtml(signal.unit)}</small></div>`;
    }).join("");
    root.innerHTML = `<div class="dbc-send-grid"><article class="panel"><header class="panel-head"><h3>信号值</h3>`
      + `<div class="head-actions"><select id="dbcSendMessage" aria-label="报文">${options}</select><button class="dbc-mini" type="button" data-act="send-reset">恢复初值</button></div></header>`
      + `<div class="dbc-send-signals">${rows || `<p class="note">${escapeHtml(dbcSend.error || "读取中…")}</p>`}</div></article>`
      + `<article class="panel"><header class="panel-head"><h3>发送</h3><span class="tag ${writable ? "ok" : "neutral"}">${writable ? "可发送 · 操作前确认" : "不可发送"}</span></header>`
      + `<div id="dbcSendPayload"></div>`
      + `<div class="panel-body"><div class="dbc-form-grid"><label class="field"><span class="field-label">周期 <small>ms，20..60000</small></span>`
      + `<input id="dbcSendCycle" type="number" min="20" max="60000" value="${dbcSend.cycle}"></label></div>`
      + `<div class="panel-foot"><button class="outline-button" type="button" data-act="send-once" ${writable ? "" : "disabled"}>发送一次</button>`
      + `<button class="action-button" type="button" id="dbcSendPeriodic" data-act="send-periodic" ${writable ? "" : "disabled"}>启用周期发送</button></div>`
      + `<p class="note" style="margin-top:var(--sp-3)">受保护的命令 ID（BMS 参数、风扇、IVT、充电机请求）会被拒绝，需用对应的专用页面发送。模拟和回放不可写。</p></div></article></div>`;
    dbcUi.signatures.sendPayload = "";
  }
  renderDbcSendPayload();
}

function renderDbcSendPayload() {
  const box = $("#dbcSendPayload");
  if (!box) return;
  const task = dbcSendTask();
  const payload = dbcSend.payload;
  const html = (payload
    ? `<div class="dbc-payload">${escapeHtml(payload.data || "（空）")}</div><div class="dbc-live-meta"><dl class="dbc-fact"><dt>CAN ID</dt><dd>${escapeHtml(dbcIdText(payload.id, payload.extended))} · ${payload.extended ? "扩展" : "标准"}</dd></dl>`
      + `<dl class="dbc-fact"><dt>DLC</dt><dd>${payload.dlc}</dd></dl>`
      + `<dl class="dbc-fact"><dt>周期发送</dt><dd class="${task?.active ? "warn" : ""}">${task?.active ? `运行中 · 已发 ${Number(task.count).toLocaleString()} 帧` : task?.last_error ? escapeHtml(task.last_error) : "未启用"}</dd></dl></div>`
    : `<div class="panel-body"><span class="tag bad">${escapeHtml(dbcSend.error || "等待编码")}</span></div>`);
  if (dbcUi.signatures.sendPayload !== html) { dbcUi.signatures.sendPayload = html; box.innerHTML = html; }
  const periodic = $("#dbcSendPeriodic");
  if (periodic) periodic.textContent = task?.active ? "停止周期发送" : "启用周期发送";
}

function dbcSendSpec() {
  const payload = dbcSend.payload;
  return { name: payload.name, id: dbcIdText(payload.id, payload.extended), extended: payload.extended,
    data: payload.data, cycle_ms: Number($("#dbcSendCycle")?.value || dbcSend.cycle) };
}

function dbcSendConfirm(periodic) {
  if (!dbcSend.payload) return toast(dbcSend.error || "还没有可发送的数据", true);
  const spec = dbcSendSpec();
  if (periodic && !(Number.isInteger(spec.cycle_ms) && spec.cycle_ms >= 20 && spec.cycle_ms <= 60000)) return toast("周期必须是 20..60000 ms 的整数", true);
  const source = dbcUi.source;
  const uid = dbcSendUid();
  state.pendingMonitorAction = {
    run: async () => periodic
      ? state.api.configure_monitor_periodic(source, uid, spec, true, true)
      : state.api.send_monitor_frame(source, spec, true),
    success: periodic ? `${spec.name} 已开始周期发送` : `${spec.name} 已发送`,
  };
  text("#confirmTitle", periodic ? "确认启用周期发送" : "确认原始帧发送");
  setConfirmModeBadge(`${dbcSourceLabel()} · 当前 PCAN 连接`, "warn");
  text("#confirmMessage", `${periodic ? `启用 ${spec.cycle_ms} ms 周期发送` : "发送一次"}。项目内受保护命令仍需从对应专用页面发送。`);
  text("#confirmPayload", `${spec.id} · ${spec.extended ? "扩展" : "标准"} · DLC ${dbcSend.payload.dlc}\n${spec.data || "无数据"}`);
  text("#confirmCheckLabel", "我已核对当前物理总线、帧 ID、信号值和周期，本次操作由我确认。");
  $("#confirmCheck").checked = false;
  $("#doConfirm").disabled = true;
  $("#confirmDialog").showModal();
}

async function dbcSendStopPeriodic() {
  const result = await state.api.configure_monitor_periodic(dbcUi.source, dbcSendUid(), dbcSendSpec(), false, true);
  if (!result?.ok) toast(result?.error || "停止周期发送失败", true); else toast("已停止周期发送");
  schedulePoll(0);
}

function bindDbcSendControls() {
  const root = $("#dbcSender");
  root?.addEventListener("input", event => {
    const node = event.target.closest("[data-sig]"); if (!node) return;
    const signal = dbcSend.form?.signals.find(item => item.name === node.dataset.sig);
    if (signal?.choices.length) dbcSend.values[node.dataset.sig] = node.value;
    else {
      const value = Number(node.value);
      dbcSend.values[node.dataset.sig] = node.value.trim() !== "" && Number.isFinite(value) ? value : node.value;
      node.classList.toggle("invalid", !(node.value.trim() !== "" && Number.isFinite(value)));
    }
    dbcSendEncode();
  });
  root?.addEventListener("change", event => {
    if (event.target.id === "dbcSendMessage") {
      dbcSend.key = event.target.value; dbcSend.form = null; dbcSend.payload = null; dbcUi.signatures.sender = "";
      renderDbcSender();
    } else if (event.target.id === "dbcSendCycle") dbcSend.cycle = Number(event.target.value);
    else if (event.target.matches("select[data-sig]")) { dbcSend.values[event.target.dataset.sig] = event.target.value; dbcSendEncode(); }
  });
  root?.addEventListener("click", event => {
    const button = event.target.closest("[data-act]"); if (!button) return;
    if (button.dataset.act === "send-reset") { dbcSend.form = null; dbcUi.signatures.sender = ""; renderDbcSender(); }
    else if (button.dataset.act === "send-once") dbcSendConfirm(false);
    else if (button.dataset.act === "send-periodic") { if (dbcSendTask()?.active) dbcSendStopPeriodic(); else dbcSendConfirm(true); }
  });
}

/* ---------- 总线对账 ---------- */

const dbcAudit = { running: false, report: null, mode: "live", title: "", busy: false, last: 0, bus: null };

async function dbcAuditCall(action) {
  const reply = await state.api.dbc_audit(dbcUi.source, action);
  if (!reply?.ok) { toast(reply?.error || "对账失败", true); return; }
  dbcAudit.running = reply.running;
  dbcAudit.bus = reply.bus;
  dbcAudit.report = reply.report;
  dbcAudit.mode = "live";
  dbcAudit.title = "";
  dbcAudit.last = Date.now();
  dbcUi.signatures.audit = "";
  renderDbcAudit();
}

async function dbcAuditTraceRun() {
  let trace = dbcPlot.trace;
  if (!trace) {
    await dbcTraceOpen();
    trace = dbcPlot.trace;
    if (!trace) return;
  }
  const reply = await state.api.dbc_trace_audit(trace.summary.id, trace.bus);
  if (!reply?.ok) return toast(reply?.error || "记录文件对账失败", true);
  dbcAudit.report = reply.report;
  dbcAudit.mode = "trace";
  dbcAudit.title = trace.summary.name;
  dbcAudit.bus = trace.bus;
  dbcUi.signatures.audit = "";
  renderDbcAudit();
}

function dbcAuditSection(title, count, headers, rows, tone = "bad") {
  if (!count) return "";
  return `<article class="panel"><header class="panel-head"><h3>${escapeHtml(title)}<span class="dbc-count ${tone}">${count}</span></h3></header>`
    + `<div class="table-wrap dbc-signal-wrap"><table class="frame-table"><thead><tr>${headers.map(h => `<th>${escapeHtml(h)}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div></article>`;
}

function dbcAuditHtml(report) {
  const cell = value => `<td>${escapeHtml(String(value))}</td>`;
  const sections = [
    dbcAuditSection("总线上有、DBC 里没有", report.unknown.length, ["CAN ID", "长度", "周期", "帧数"],
      report.unknown.map(item => `<tr>${cell(`${item.id_text} · ${item.extended ? "扩展" : "标准"}`)}${cell(item.dlc.join("/") || "—")}${cell(item.cycle_ms != null ? `${dbcNumber(item.cycle_ms)} ms` : "—")}${cell(Number(item.count).toLocaleString())}</tr>`).join("")),
    dbcAuditSection("DBC 里有、整个统计窗口里没出现", report.missing.length, ["报文", "CAN ID"],
      report.missing.map(item => `<tr>${cell(item.name)}${cell(item.id_text)}</tr>`).join(""), "warn"),
    dbcAuditSection("DLC 与 DBC 不一致", report.dlc.length, ["报文", "CAN ID", "DBC", "实际"],
      report.dlc.map(item => `<tr>${cell(item.name)}${cell(item.id_text)}${cell(item.expected)}${cell(item.seen.join("/"))}</tr>`).join("")),
    dbcAuditSection("周期与 DBC 不一致", report.cycle.length, ["报文", "CAN ID", "DBC 周期", "实测均值", "最长间隔"],
      report.cycle.map(item => `<tr>${cell(item.name)}${cell(item.id_text)}${cell(`${item.expected_ms} ms`)}${cell(`${dbcNumber(item.measured_ms)} ms`)}${cell(`${dbcNumber(item.worst_ms)} ms`)}</tr>`).join(""), "warn"),
    dbcAuditSection("取值超出 DBC 声明的范围", report.range.length, ["信号", "单位", "实测范围", "DBC 范围", "越界次数"],
      report.range.map(item => `<tr>${cell(`${item.message}.${item.signal}`)}${cell(item.unit || "—")}${cell(`${dbcNumber(item.low)} … ${dbcNumber(item.high)}`)}${cell(`${dbcNumber(item.limit_low)} … ${dbcNumber(item.limit_high)}`)}${cell(`${item.count} / ${item.samples}`)}</tr>`).join("")),
    dbcAuditSection("取值不在值表里", report.choice.length, ["信号", "出现次数"],
      report.choice.map(item => `<tr>${cell(`${item.message}.${item.signal}`)}${cell(`${item.count} / ${item.samples}`)}</tr>`).join(""), "warn"),
  ].join("");
  const clean = !sections && report.frames > 0 && report.window_ok
    ? '<div class="panel"><div class="dbc-audit-ok">统计窗口内 DBC 与总线一致：没有未识别 ID、DLC 或周期偏差，数值都在声明范围内。</div></div>' : "";
  const wait = !report.frames ? '<div class="panel"><div class="empty-state">还没有统计到帧。连接 CAN（或载入记录文件）后点“开始对账”，窗口越长，“没出现的报文”越可信。</div></div>'
    : !report.window_ok ? '<div class="note">统计窗口不足 3 秒，“没出现的报文”暂不判断。</div>' : "";
  return wait + sections + clean + (report.overflow ? '<div class="note">不同 ID 超过上限，部分帧未统计。</div>' : "");
}

function renderDbcAudit() {
  const report = dbcAudit.report;
  const tag = $("#dbcAuditState");
  if (tag) {
    tag.textContent = dbcAudit.mode === "trace" ? `记录文件 · ${dbcAudit.title}` : dbcAudit.running ? "统计中" : report ? "已停止" : "未开始";
    tag.className = `tag ${dbcAudit.running ? "ok" : "neutral"}`;
  }
  const toggle = $("#dbcAuditToggle");
  if (toggle) toggle.textContent = dbcAudit.running ? "停止对账" : "开始对账";
  text("#dbcAuditStats", report ? `${Number(report.frames).toLocaleString()} 帧 · ${report.ids} 个 ID · 已核对 ${report.clean_messages}/${report.known_messages} 条报文无问题 · 窗口 ${dbcNumber(report.window_s)} s` : "");
  dbcPaint("audit", $("#dbcAudit"), report ? dbcAuditHtml(report)
    : '<div class="panel"><div class="empty-state">把 DBC 绑定到当前总线，点“开始对账”：上位机会统计总线上的真实情况，并与 DBC 逐项对照。固件与 DBC 不一致时以固件为准，这里给出证据。</div></div>');
  if (dbcAudit.running && dbcAudit.mode === "live" && !dbcAudit.busy && Date.now() - dbcAudit.last > 900) {
    dbcAudit.busy = true;
    state.api.dbc_audit(dbcUi.source, "report").then(reply => {
      if (reply?.ok && dbcAudit.mode === "live") { dbcAudit.report = reply.report; dbcAudit.running = reply.running; }
    }).finally(() => { dbcAudit.busy = false; dbcAudit.last = Date.now(); });
  }
}

function bindDbcAuditControls() {
  $("#dbcAuditToggle")?.addEventListener("click", () => dbcAuditCall(dbcAudit.running ? "stop" : "start"));
  $("#dbcAuditReset")?.addEventListener("click", () => dbcAuditCall("reset"));
  $("#dbcAuditTrace")?.addEventListener("click", dbcAuditTraceRun);
}

function bindDbcToolControls() {
  bindDbcSendControls();
  bindDbcAuditControls();
}
