/* DBC 工作台·编辑：报文与信号表单、位布局拖选、值表、节点、校验和保存评审。
 * 每次提交都落到后端的保真文档模型，后端立即用新定义重新解码，所以改完就能在“实时”页验证。 */

const dbcEdit = { sub: "message", newOrder: "intel", drag: null, muxView: "" };

const DBC_ORDER_LABEL = { intel: "Intel @1", motorola: "Motorola @0" };
const DBC_VALUE_TYPE_LABEL = { int: "整数", float32: "float32", float64: "float64" };
const DBC_MUX_LABEL = { none: "无", multiplexor: "选择信号 M", multiplexed: "被复用 mN", both: "被复用且是选择信号" };

/* ---------- 位布局（与 canhost/dbc/layout.py 一致，测试逐位对照） ---------- */

function dbcSignalBits(start, length, order) {
  const bits = [];
  if (length <= 0) return bits;
  if (order === "intel") {
    for (let bit = start + length - 1; bit >= start; bit -= 1) bits.push(bit);
    return bits;
  }
  let bit = start;
  for (let index = 0; index < length; index += 1) {
    bits.push(bit);
    bit = bit % 8 === 0 ? bit + 15 : bit - 1;
  }
  return bits;
}

/** 拖选的两个格子 -> 起始位与长度；Motorola 沿“字节内从高到低、再进下一字节”的顺序连续。 */
function dbcRangeFromCells(first, second, order) {
  if (order === "intel") {
    const low = Math.min(first, second), high = Math.max(first, second);
    return { start: low, length: high - low + 1 };
  }
  const position = bit => Math.floor(bit / 8) * 8 + (7 - (bit % 8));
  const low = Math.min(position(first), position(second)), high = Math.max(position(first), position(second));
  return { start: Math.floor(low / 8) * 8 + (7 - (low % 8)), length: high - low + 1 };
}

/** 两个信号能否出现在同一帧里：只有被同一选择信号的不同取值选中的信号才互斥。 */
function dbcSignalsCoexist(first, second) {
  const muxed = signal => signal.mux.role === "multiplexed" || signal.mux.role === "both";
  if (!(muxed(first) && muxed(second))) return true;
  const a = first.mux.ext, b = second.mux.ext;
  if (a || b) {
    if (!(a && b) || a.signal !== b.signal) return true;
    return a.ranges.some(([low1, high1]) => b.ranges.some(([low2, high2]) => low1 <= high2 && low2 <= high1));
  }
  return first.mux.value === second.mux.value;
}

function dbcSelectedSignal() {
  return dbcMessageEntry()?.signals.find(signal => signal.name === dbcUi.selected.signal) || null;
}

function dbcReadonly() { return !!dbcCurrentFile()?.readonly; }

/* ---------- 渲染 ---------- */

function renderDbcEditBar(file) {
  const readonly = !!file?.readonly;
  const stateTag = $("#dbcEditState");
  if (stateTag) {
    stateTag.textContent = !file ? "无文件" : readonly ? "只读" : file.dirty ? "未保存" : "已保存";
    stateTag.className = `tag ${!file ? "neutral" : readonly || file.dirty ? "warn" : "ok"}`;
  }
  $("#dbcUndo").disabled = !file || !file.can_undo || readonly;
  $("#dbcRedo").disabled = !file || !file.can_redo || readonly;
  $("#dbcSave").disabled = !file || readonly || !file.dirty;
  $("#dbcReview").disabled = !file || !file.dirty;
  $("#dbcSaveAs").disabled = !file;
  $("#dbcCompare").disabled = !file || dbcUi.files.length < 2;
  const issues = dbcDetail()?.issues || [];
  const blocking = issues.filter(issue => issue.level === "error").length;
  const counter = $("#dbcCheckCount");
  if (counter) {
    counter.textContent = String(issues.filter(issue => issue.level !== "note").length);
    counter.className = `dbc-count ${blocking ? "bad" : issues.some(issue => issue.level === "warn") ? "warn" : ""}`;
  }
  $$("#dbcEditTabs button").forEach(button => button.classList.toggle("active", button.dataset.sub === dbcEdit.sub));
  const notice = $("#dbcEditNotice");
  let message = "";
  if (file?.readonly) message = "团队中央库 · 只读。另存为副本后可编辑。";
  else if (file?.disk_changed) message = "磁盘上的文件在打开后被改动过。保存会覆盖它（旧内容留在 .bak）；也可以先在左侧文件卡里“重新载入”。";
  if (notice) {
    notice.textContent = message;
    notice.classList.toggle("hidden", !message);
    notice.classList.toggle("warn", !!file?.disk_changed && !readonly);
  }
}

function renderDbcEditor() {
  const file = dbcCurrentFile();
  renderDbcEditBar(file);
  const root = $("#dbcEditor");
  if (!file || !dbcCatalog()) {
    dbcPaint("editor", root, '<div class="empty-state large">先在左侧打开或新建一个 DBC。</div>');
    return;
  }
  const signature = JSON.stringify([file.id, file.revision, file.readonly, dbcEdit.sub, dbcUi.selected, dbcEdit.newOrder, dbcEdit.muxView]);
  if (dbcUi.signatures.editor !== signature) {
    dbcUi.signatures.editor = signature;
    const active = document.activeElement;
    const focusField = active && root.contains(active) ? active.dataset.field || active.dataset.receiver || null : null;
    const focusForm = focusField ? active.closest("[data-form]")?.dataset.form : null;
    const focusBit = active && root.contains(active) ? active.dataset.bit : null;
    const focusSignal = active?.classList.contains("dbc-signal-select") ? active.closest("[data-signal]")?.dataset.signal : null;
    root.innerHTML = dbcEditorHtml();
    if (focusField) {
      const scope = focusForm ? root.querySelector(`[data-form="${focusForm}"]`) : root;
      const target = scope?.querySelector(`[data-field="${focusField}"]`) || scope?.querySelector(`[data-receiver="${focusField}"]`);
      target?.focus({ preventScroll: true });
    } else if (focusBit != null) root.querySelector(`[data-bit="${focusBit}"]`)?.focus({ preventScroll: true });
    else if (focusSignal) [...root.querySelectorAll("#dbcSignalList tr")].find(row => row.dataset.signal === focusSignal)?.querySelector("button")?.focus({ preventScroll: true });
  }
  renderDbcTrial();
}

function dbcEditorHtml() {
  const catalog = dbcCatalog();
  if (dbcEdit.sub === "nodes") return dbcNodesHtml(catalog);
  if (dbcEdit.sub === "tables") return dbcTablesHtml(catalog);
  if (dbcEdit.sub === "check") return dbcCheckHtml();
  return dbcMessageEditorHtml(catalog);
}

function dbcOptions(items, selected) {
  return items.map(item => `<option value="${escapeHtml(item.value)}" ${String(item.value) === String(selected) ? "selected" : ""}>${escapeHtml(item.label)}</option>`).join("");
}

function dbcIssueFor(issues, key, signal = null) {
  return issues.filter(issue => issue.message_key === key && (signal === null || issue.signal === signal));
}

function dbcMessageEditorHtml(catalog) {
  const entry = dbcMessageEntry();
  const readonly = dbcReadonly();
  const lock = readonly ? "disabled" : "";
  if (entry?.virtual) {
    return '<article class="panel"><header class="panel-head"><h3>未使用信号</h3></header>'
      + '<div class="panel-body note tight">这些信号尚未分配到报文，不参与总线解码。</div></article>'
      + dbcSignalPanelHtml(entry, catalog);
  }
  const actions = (names) => names.map(([act, label, danger]) =>
    `<button class="dbc-mini ${danger ? "danger" : ""}" type="button" data-act="${act}" ${lock}>${label}</button>`).join("");
  if (!entry) {
    return `<article class="panel"><div class="empty-state">这个 DBC 还没有报文。</div>`
      + `<div class="panel-foot" style="padding:0 var(--sp-3) var(--sp-3)"><button class="action-button" type="button" data-act="msg-new" ${lock}>新建报文</button></div></article>`;
  }
  const nodeNames = catalog.nodes.map(node => node.name);
  const sender = entry.senders[0] || "";
  const messageForm = `<div class="dbc-form-grid" data-form="message">`
    + `<label class="field"><span class="field-label">名称</span><input data-field="name" value="${escapeHtml(entry.name)}" ${lock} maxlength="128"></label>`
    + `<label class="field"><span class="field-label">CAN ID <small>十六进制</small></span><input data-field="id" value="${entry.id.toString(16).toUpperCase()}" ${lock}></label>`
    + `<label class="field"><span class="field-label">帧类型</span><select data-field="extended" ${lock}>${dbcOptions([{ value: "false", label: "标准 11 位" }, { value: "true", label: "扩展 29 位" }], String(entry.extended))}</select></label>`
    + `<label class="field"><span class="field-label">DLC</span><input data-field="dlc" type="number" min="0" max="8" value="${entry.dlc}" ${lock}></label>`
    + `<label class="field"><span class="field-label">发送节点</span><select data-field="sender" ${lock}>${dbcOptions([{ value: "", label: "（无）" }, ...nodeNames.map(name => ({ value: name, label: name }))], sender)}</select></label>`
    + `<label class="field"><span class="field-label">周期 <small>ms，0 表示非周期</small></span><input data-field="cycle_time" type="number" min="0" max="65535" value="${entry.cycle_ms ?? ""}" ${lock}></label>`
    + `<label class="field wide"><span class="field-label">注释</span><textarea data-field="comment" rows="2" ${lock}>${escapeHtml(entry.comment)}</textarea></label></div>`;
  const issues = dbcDetail()?.issues || [];
  const messageIssues = dbcIssueFor(issues, entry.key).filter(issue => issue.level !== "note");
  const issueNote = messageIssues.length
    ? `<div class="note tight" style="margin-top:var(--sp-2)">${messageIssues.slice(0, 3).map(issue => `<span class="tag ${issue.level === "error" ? "bad" : "warn"}">${escapeHtml(issue.message)}</span>`).join(" ")}</div>` : "";
  const left = `<article class="panel dbc-message-panel"><header class="panel-head"><h3 title="${escapeHtml(entry.name)}">${escapeHtml(entry.name)}</h3><div class="head-actions">`
    + actions([["msg-new", "新建"], ["msg-dup", "复制"], ["msg-up", "上移"], ["msg-down", "下移"], ["msg-del", "删除", true]])
    + `</div></header><div class="panel-body dbc-message-layout"><div>${messageForm}${issueNote}</div><div class="dbc-layout-preview">`
    + `<div class="dbc-subhead"><span>位布局</span><span class="dbc-inline">${dbcMuxViewHtml(entry)}<div class="segmented small" id="dbcNewOrder" role="group" aria-label="新信号字节序">`
    + ["intel", "motorola"].map(order => `<button type="button" data-order="${order}" class="${dbcEdit.newOrder === order ? "active" : ""}" title="新建信号时使用的字节序">${DBC_ORDER_LABEL[order]}</button>`).join("")
    + `</div></span></div>${dbcBitGridHtml(entry)}`
    + `<p class="dbc-grid-legend">${readonly ? "点击位格查看信号。" : "拖选位格调整信号，空白处拖选新建。"} 红色表示重叠，虚线表示超出 DLC。</p></div></div></article>`;
  return `<div class="dbc-edit-grid">${left}${dbcSignalPanelHtml(entry, catalog)}</div>`;
}

/** 多路复用报文：按复用值查看位布局，其余分支的信号先隐藏。 */
function dbcMuxViewHtml(entry) {
  const values = [...new Set(entry.signals.filter(signal => signal.mux.role === "multiplexed" || signal.mux.role === "both")
    .map(signal => signal.mux.value))].sort((a, b) => a - b);
  if (!values.length) return "";
  const options = [{ value: "", label: "全部复用值" }, ...values.map(value => ({ value: String(value), label: `复用值 ${value}` }))];
  return `<select data-field="mux_view" aria-label="按复用值查看">${dbcOptions(options, dbcEdit.muxView)}</select>`;
}

function dbcBitGridHtml(entry) {
  const rows = Math.max(1, Math.ceil(entry.bytes / 8) || 1, entry.dlc || 1);
  const owners = new Map();
  const shown = entry.signals.map(signal => dbcEdit.muxView === ""
    || !(signal.mux.role === "multiplexed" || signal.mux.role === "both") || String(signal.mux.value) === dbcEdit.muxView);
  entry.signals.forEach((signal, index) => {
    if (!shown[index]) return;
    signal.bits.forEach(bit => { if (!owners.has(bit)) owners.set(bit, []); owners.get(bit).push(index); });
  });
  const selectedIndex = entry.signals.findIndex(signal => signal.name === dbcUi.selected.signal);
  let html = '<table class="dbc-bitgrid" aria-label="位布局"><thead><tr><th></th>'
    + [7, 6, 5, 4, 3, 2, 1, 0].map(bit => `<th>${bit}</th>`).join("") + "</tr></thead><tbody>";
  for (let byte = 0; byte < rows; byte += 1) {
    html += `<tr><th class="row">字节 ${byte}</th>`;
    for (let bit = 7; bit >= 0; bit -= 1) {
      const index = byte * 8 + bit;
      const list = owners.get(index) || [];
      const classes = [];
      let style = "", label = "";
      if (index >= entry.dlc * 8) classes.push("outside");
      if (list.length) {
        classes.push("owned");
        const first = list[0];
        style = `background:var(--sigbg-${(first % 8) + 1});`;
        if (list.some((a, i) => list.slice(i + 1).some(b => dbcSignalsCoexist(entry.signals[a], entry.signals[b])))) classes.push("clash");
        const signal = entry.signals[first];
        if (signal.bits[0] === index) { classes.push("msb"); label = `<span class="tag-name">${escapeHtml(signal.name)}</span>`; }
        if (list.includes(selectedIndex)) classes.push("selected");
      }
      const owner = list.map(item => entry.signals[item].name).join(" / ");
      html += `<td class="${classes.join(" ")}" style="${style}" data-bit="${index}" ${owner ? `tabindex="0" role="button" aria-label="${escapeHtml(owner)}，bit ${index}"` : ""} title="${escapeHtml(owner ? `${owner} · bit ${index}` : `bit ${index}（空闲）`)}"><span class="bit">${index}</span>${label}</td>`;
    }
    html += "</tr>";
  }
  return `${html}</tbody></table>`;
}

function dbcSignalPanelHtml(entry, catalog) {
  const readonly = dbcReadonly();
  const lock = readonly ? "disabled" : "";
  const issues = dbcDetail()?.issues || [];
  const rows = entry.signals.map((signal, index) => {
    const bad = dbcIssueFor(issues, entry.key, signal.name).some(issue => issue.level === "error");
    const mux = signal.mux.role !== "none" ? `<span class="dbc-badge-mux">${signal.mux.role === "multiplexor" ? "M" : `m${signal.mux.value}`}</span>` : "";
    return `<tr class="${signal.name === dbcUi.selected.signal ? "active" : ""}" data-signal="${escapeHtml(signal.name)}">`
      + `<td><button class="dbc-signal-select" type="button" aria-pressed="${signal.name === dbcUi.selected.signal}"><span class="dbc-swatch" style="background:var(--series-${(index % 8) + 1})"></span>${escapeHtml(signal.name)}${mux}</button>${bad ? ' <span class="dbc-count bad">!</span>' : ""}</td>`
      + `<td>${signal.start}|${signal.length}</td><td>${signal.byte_order === "intel" ? "Intel" : "Motorola"}</td>`
      + `<td>${dbcNumber(signal.factor)}</td><td>${dbcNumber(signal.offset)}</td><td>${escapeHtml(signal.unit)}</td></tr>`;
  }).join("");
  const signal = dbcSelectedSignal();
  const actions = [["sig-new", "新建"], ["sig-dup", "复制"], ["sig-up", "上移"], ["sig-down", "下移"], ["sig-del", "删除", true]]
    .map(([act, label, danger]) => `<button class="dbc-mini ${danger ? "danger" : ""}" type="button" data-act="${act}" ${lock || ((act !== "sig-new" && !signal) ? "disabled" : "")}>${label}</button>`).join("");
  return `<article class="panel"><header class="panel-head"><h3>信号 · ${entry.signals.length}</h3><div class="head-actions">${actions}</div></header>`
    + `<div class="panel-body"><div class="table-wrap dbc-signal-list"><table class="frame-table"><thead><tr><th>名称</th><th>位|长</th><th>字节序</th><th>因子</th><th>偏移</th><th>单位</th></tr></thead>`
    + `<tbody id="dbcSignalList">${rows || '<tr><td colspan="6">还没有信号，点“新建”或在位布局里拖选。</td></tr>'}</tbody></table></div>`
    + (signal ? dbcSignalFormHtml(entry, signal, catalog, lock) : '<p class="note">选中一个信号后在这里编辑。</p>') + "</div></article>";
}

function dbcSignalFormHtml(entry, signal, catalog, lock) {
  const nodes = catalog.nodes.map(node => node.name);
  const receivers = nodes.length ? nodes.map(name => `<label class="checkbox"><input type="checkbox" data-receiver="${escapeHtml(name)}" ${signal.receivers.includes(name) ? "checked" : ""} ${lock}><span>${escapeHtml(name)}</span></label>`).join("")
    : '<span class="note tight">先在“节点”里添加节点。</span>';
  const choiceRows = signal.choices.map(choice => dbcChoiceRowHtml(choice.value, choice.label, lock)).join("");
  const tables = catalog.value_tables.length
    ? `<select data-field="apply_table" ${lock}><option value="">套用全局值表…</option>${catalog.value_tables.map(table => `<option value="${escapeHtml(table.name)}">${escapeHtml(table.name)}</option>`).join("")}</select>` : "";
  return `<div class="dbc-subhead"><span>信号 ${escapeHtml(signal.name)}</span></div><div class="dbc-form-grid" data-form="signal">`
    + `<label class="field"><span class="field-label">名称</span><input data-field="name" value="${escapeHtml(signal.name)}" ${lock} maxlength="128"></label>`
    + `<label class="field"><span class="field-label">起始位</span><input data-field="start" type="number" min="0" max="511" value="${signal.start}" ${lock}></label>`
    + `<label class="field"><span class="field-label">长度 <small>位</small></span><input data-field="length" type="number" min="1" max="64" value="${signal.length}" ${lock}></label>`
    + `<label class="field"><span class="field-label">字节序</span><select data-field="byte_order" ${lock}>${dbcOptions([{ value: "intel", label: DBC_ORDER_LABEL.intel }, { value: "motorola", label: DBC_ORDER_LABEL.motorola }], signal.byte_order)}</select></label>`
    + `<label class="field"><span class="field-label">符号</span><select data-field="signed" ${lock}>${dbcOptions([{ value: "false", label: "无符号" }, { value: "true", label: "有符号" }], String(signal.signed))}</select></label>`
    + `<label class="field"><span class="field-label">数值类型</span><select data-field="value_type" ${lock}>${dbcOptions(Object.entries(DBC_VALUE_TYPE_LABEL).map(([value, label]) => ({ value, label })), signal.value_type)}</select></label>`
    + `<label class="field"><span class="field-label">因子</span><input data-field="factor" value="${dbcNumber(signal.factor)}" ${lock}></label>`
    + `<label class="field"><span class="field-label">偏移</span><input data-field="offset" value="${dbcNumber(signal.offset)}" ${lock}></label>`
    + `<label class="field"><span class="field-label">最小值</span><input data-field="minimum" value="${dbcNumber(signal.minimum)}" ${lock}></label>`
    + `<label class="field"><span class="field-label">最大值</span><input data-field="maximum" value="${dbcNumber(signal.maximum)}" ${lock}></label>`
    + `<label class="field"><span class="field-label">单位</span><input data-field="unit" value="${escapeHtml(signal.unit)}" ${lock}></label>`
    + `<label class="field"><span class="field-label">初始原始值</span><input data-field="initial_raw" value="${signal.initial_raw ?? ""}" ${lock}></label>`
    + `<label class="field"><span class="field-label">多路复用</span><select data-field="mux_role" ${lock}>${dbcOptions(Object.entries(DBC_MUX_LABEL).map(([value, label]) => ({ value, label })), signal.mux.role)}</select></label>`
    + `<label class="field"><span class="field-label">复用值 <small>mN</small></span><input data-field="mux_value" type="number" min="0" value="${signal.mux.value ?? 0}" ${lock}></label>`
    + dbcExtMuxHtml(entry, signal, lock)
    + `<div class="field wide"><span class="field-label">接收节点</span><div class="dbc-inline">${receivers}</div></div>`
    + `<label class="field wide"><span class="field-label">注释</span><textarea data-field="comment" rows="2" ${lock}>${escapeHtml(signal.comment)}</textarea></label></div>`
    + `<div class="dbc-subhead"><span>值表</span><span class="dbc-inline">${tables}<button class="dbc-mini" type="button" data-act="choice-add" ${lock}>添加</button></span></div>`
    + `<div data-form="choices"><table class="dbc-choices"><tbody>${choiceRows || ""}</tbody></table>${choiceRows ? "" : '<p class="note tight">没有值表。填写后，实时页会把原始值显示成文字。</p>'}</div>`
    + `<div class="dbc-subhead"><span>当前帧试算</span></div><div class="dbc-trial" id="dbcTrial">等待数据</div>`;
}

/** 扩展多路复用（SG_MUL_VAL_）：被复用的信号可指定父选择信号和取值范围。 */
function dbcExtMuxHtml(entry, signal, lock) {
  if (signal.mux.role !== "multiplexed" && signal.mux.role !== "both") return "";
  const parents = entry.signals.filter(item => item.name !== signal.name && item.mux.role !== "multiplexed");
  const ext = signal.mux.ext;
  const ranges = ext ? ext.ranges.map(([low, high]) => (low === high ? String(low) : `${low}-${high}`)).join(",") : "";
  return `<label class="field"><span class="field-label">扩展复用父信号 <small>可选</small></span><select data-field="ext_parent" ${lock}>`
    + dbcOptions([{ value: "", label: "（无，简单复用）" }, ...parents.map(item => ({ value: item.name, label: item.name }))], ext?.signal || "") + "</select></label>"
    + `<label class="field"><span class="field-label">扩展复用取值 <small>如 1-3,5</small></span><input data-field="ext_ranges" value="${escapeHtml(ranges)}" ${lock}></label>`;
}

function dbcChoiceRowHtml(value, label, lock) {
  return `<tr><td><input data-choice="value" value="${escapeHtml(String(value))}" ${lock} aria-label="取值"></td>`
    + `<td><input data-choice="label" value="${escapeHtml(label)}" ${lock} aria-label="含义"></td>`
    + `<td><button class="dbc-mini danger" type="button" data-act="choice-del" ${lock} aria-label="删除这一行">×</button></td></tr>`;
}

function renderDbcTrial() {
  const box = $("#dbcTrial");
  if (!box) return;
  const decoded = dbcUi.live[dbcUi.source]?.decoded?.[dbcUi.selected.key];
  const row = decoded && decoded.file === dbcUi.fileId ? decoded.signals.find(item => item.name === dbcUi.selected.signal) : null;
  let content = "总线上还没有这条报文，或这个 DBC 没有绑定到当前数据源。";
  if (row?.state === "ok") {
    content = `<span>原始 ${escapeHtml(String(row.raw_text))}</span><span>物理 <b>${escapeHtml(dbcNumber(row.value))}</b> ${escapeHtml(row.unit || "")}</span>`
      + (row.has_label ? `<span>含义 ${row.label != null ? escapeHtml(row.label) : "表外值"}</span>` : "")
      + `<span>帧 ${escapeHtml(decoded.data)}</span>`;
  } else if (row) {
    content = row.state === "inactive" ? "这一帧的多路复用选择值不是该信号所在分支" : row.state === "missing" ? "收到的数据比 DLC 短，这个信号没有完整数据" : "等待数据";
  }
  if (dbcUi.signatures.trial !== content) { dbcUi.signatures.trial = content; box.innerHTML = content; }
}

function dbcNodesHtml(catalog) {
  const lock = dbcReadonly() ? "disabled" : "";
  const rows = catalog.nodes.map(node => `<tr data-node="${escapeHtml(node.name)}"><td class="dbc-name">${escapeHtml(node.name)}</td>`
    + `<td>${node.sends}</td><td>${node.receives}</td>`
    + `<td><input data-node-comment value="${escapeHtml(node.comment)}" placeholder="说明" ${lock} style="width:100%"></td>`
    + `<td><div class="dbc-row-actions"><button data-act="node-rename" ${lock}>改名</button><button data-act="node-del" ${lock}>删除</button></div></td></tr>`).join("");
  return `<article class="panel"><header class="panel-head"><h3>节点 · ${catalog.nodes.length}</h3><div class="head-actions"><button class="dbc-mini" type="button" data-act="node-new" ${lock}>新建节点</button></div></header>`
    + `<div class="table-wrap" style="border:0"><table class="frame-table"><thead><tr><th>节点</th><th>发送报文</th><th>接收报文</th><th>说明</th><th></th></tr></thead><tbody>${rows || '<tr><td colspan="5">还没有节点。</td></tr>'}</tbody></table></div></article>`;
}

function dbcTablesHtml(catalog) {
  const lock = dbcReadonly() ? "disabled" : "";
  const tables = catalog.value_tables.map(table => `<div class="dbc-subhead"><span>${escapeHtml(table.name)}</span><button class="dbc-mini danger" type="button" data-act="table-del" data-table="${escapeHtml(table.name)}" ${lock}>删除</button></div>`
    + `<div data-form="table" data-table="${escapeHtml(table.name)}"><table class="dbc-choices"><tbody>${table.choices.map(choice => dbcChoiceRowHtml(choice.value, choice.label, lock)).join("")}</tbody></table>`
    + `<button class="dbc-mini" type="button" data-act="table-row" ${lock}>添加一行</button></div>`).join("");
  const attrs = catalog.attr_defs.map(attr => `<tr><td>${escapeHtml({ "": "全局", BO_: "报文", SG_: "信号", BU_: "节点" }[attr.scope] || attr.scope)}</td><td class="dbc-name">${escapeHtml(attr.name)}</td>`
    + `<td>${escapeHtml(attr.type)} ${escapeHtml(attr.args)}</td><td>${attr.default == null ? "—" : escapeHtml(String(attr.default))}</td>`
    + `<td><div class="dbc-row-actions"><button data-act="attr-del" data-scope="${attr.scope}" data-name="${escapeHtml(attr.name)}" ${lock}>删除</button></div></td></tr>`).join("");
  return `<div class="dbc-edit-grid"><article class="panel"><header class="panel-head"><h3>全局值表</h3><div class="head-actions"><button class="dbc-mini" type="button" data-act="table-new" ${lock}>新建值表</button></div></header>`
    + `<div class="panel-body">${tables || '<p class="note tight">没有全局值表。信号的值表可以直接在信号里填写。</p>'}</div></article>`
    + `<article class="panel"><header class="panel-head"><h3>属性定义</h3><div class="head-actions"><button class="dbc-mini" type="button" data-act="attr-new" ${lock}>新建属性</button></div></header>`
    + `<div class="table-wrap" style="border:0"><table class="frame-table"><thead><tr><th>范围</th><th>名称</th><th>类型</th><th>默认</th><th></th></tr></thead><tbody>${attrs || '<tr><td colspan="5">没有属性定义。</td></tr>'}</tbody></table></div>`
    + `<div class="panel-body"><div class="dbc-form-grid" data-form="meta"><label class="field"><span class="field-label">版本</span><input data-field="version" value="${escapeHtml(catalog.version)}" ${lock}></label>`
    + `<label class="field wide"><span class="field-label">数据库注释</span><textarea data-field="db_comment" rows="2" ${lock}>${escapeHtml(catalog.comment)}</textarea></label></div></div></article></div>`;
}

function dbcCheckHtml() {
  const issues = dbcDetail()?.issues || [];
  const order = { error: 0, warn: 1, note: 2 };
  const label = { error: "错误", warn: "警告", note: "提示" };
  const cls = { error: "bad", warn: "warn", note: "neutral" };
  const rows = [...issues].sort((a, b) => order[a.level] - order[b.level]).map((issue, index) =>
    `<div class="dbc-issue"><span class="tag ${cls[issue.level]}">${label[issue.level]}</span><span>${escapeHtml(issue.message)}</span>`
    + `${issue.message_key ? `<button class="dbc-mini" type="button" data-act="goto" data-key="${escapeHtml(issue.message_key)}" data-signal="${escapeHtml(issue.signal || "")}">定位</button>` : "<span></span>"}</div>`).join("");
  return `<article class="panel"><header class="panel-head"><h3>校验 · ${issues.length}</h3></header>`
    + `<div class="dbc-issues">${rows || '<div class="dbc-audit-ok">没有发现问题。</div>'}</div></article>`;
}

/* ---------- 提交 ---------- */

function dbcParseNumber(textValue) {
  const value = Number(String(textValue).trim());
  return String(textValue).trim() !== "" && Number.isFinite(value) ? value : null;
}

function dbcMarkInvalid(node, message) {
  node.classList.add("invalid");
  toast(message, true);
}

async function dbcEditorChange(event) {
  const target = event.target;
  if (dbcReadonly()) return;
  target.classList.remove("invalid");
  const entry = dbcMessageEntry();
  const form = target.closest("[data-form]");
  if (target.dataset.field === "apply_table" && entry) return dbcApplyTable(entry, target);
  if (target.dataset.field === "mux_view") { dbcEdit.muxView = target.value; dbcUi.signatures.editor = ""; return void renderDbc(); }
  if (target.matches("[data-node-comment]")) {
    const node = target.closest("tr").dataset.node;
    return void await dbcCommit([{ op: "set_comment", scope: "node", node, comment: target.value }]);
  }
  if (!form && !target.dataset.field) return;
  const kind = form?.dataset.form;
  if (kind === "message" && entry) return dbcCommitMessageField(entry, target);
  if (kind === "signal" && entry) return dbcCommitSignalField(entry, target, form);
  if (kind === "choices" && entry) return dbcCommitChoices(entry, form);
  if (kind === "table") return dbcCommitTable(form);
  if (kind === "meta") {
    const field = target.dataset.field;
    if (field === "version") return void await dbcCommit([{ op: "set_version", version: target.value }]);
    if (field === "db_comment") return void await dbcCommit([{ op: "set_comment", scope: "database", comment: target.value }]);
  }
}

async function dbcCommitMessageField(entry, target) {
  const field = target.dataset.field;
  let fields = {};
  let nextKey = null;
  if (field === "name") fields = { name: target.value.trim() };
  else if (field === "comment") fields = { comment: target.value };
  else if (field === "dlc") {
    const value = dbcParseNumber(target.value);
    if (value === null || value < 0 || value > 8) return dbcMarkInvalid(target, "DLC 必须是 0 到 8 的整数");
    fields = { dlc: value };
  } else if (field === "cycle_time") {
    const value = target.value.trim() === "" ? null : dbcParseNumber(target.value);
    if (target.value.trim() !== "" && (value === null || value < 0 || value > 65535)) return dbcMarkInvalid(target, "周期必须是 0 到 65535 ms");
    fields = { cycle_time: value };
  } else if (field === "sender") {
    fields = { senders: target.value ? [target.value, ...entry.senders.slice(1).filter(name => name !== target.value)] : [] };
  } else if (field === "id" || field === "extended") {
    const root = target.closest("[data-form]");
    const idText = root.querySelector('[data-field="id"]').value.trim().replace(/^0x/i, "");
    const extended = root.querySelector('[data-field="extended"]').value === "true";
    const id = /^[0-9a-f]+$/i.test(idText) ? parseInt(idText, 16) : NaN;
    if (!Number.isInteger(id) || id < 0 || id > (extended ? 0x1FFFFFFF : 0x7FF)) {
      return dbcMarkInvalid(target, extended ? "扩展 ID 必须在 0..1FFFFFFF" : "标准 ID 必须在 0..7FF");
    }
    fields = { id, extended };
    nextKey = dbcKey(id, extended);
  }
  await dbcCommit([{ op: "set_message", message: entry.key, fields }], { key: nextKey });
}

async function dbcCommitSignalField(entry, target, form) {
  const signal = dbcSelectedSignal();
  if (!signal) return;
  const field = target.dataset.field;
  const fields = {};
  const numberFields = ["start", "length", "mux_value"];
  const decimalFields = ["factor", "offset", "minimum", "maximum"];
  if (numberFields.includes(field)) {
    const value = dbcParseNumber(target.value);
    if (value === null || !Number.isInteger(value) || value < 0) return dbcMarkInvalid(target, "请输入非负整数");
    fields[field] = value;
  } else if (decimalFields.includes(field)) {
    const value = dbcParseNumber(target.value);
    if (value === null) return dbcMarkInvalid(target, "请输入数字");
    fields[field] = value;
  } else if (field === "signed") fields.signed = target.value === "true";
  else if (field === "initial_raw") {
    const value = target.value.trim() === "" ? null : dbcParseNumber(target.value);
    if (target.value.trim() !== "" && value === null) return dbcMarkInvalid(target, "请输入数字");
    fields.initial_raw = value;
  } else if (field === "ext_parent" || field === "ext_ranges") {
    const parent = form.querySelector('[data-field="ext_parent"]').value;
    const rangeText = form.querySelector('[data-field="ext_ranges"]').value.replace(/\s+/g, "");
    if (!parent) fields.ext_mux = null;
    else {
      if (!/^\d+(-\d+)?(,\d+(-\d+)?)*$/.test(rangeText)) return dbcMarkInvalid(form.querySelector('[data-field="ext_ranges"]'), "取值范围写成 1-3,5 这样的形式");
      fields.ext_mux = { signal: parent, ranges: rangeText.split(",").map(part => {
        const [low, high] = part.split("-").map(Number);
        return [low, high ?? low];
      }) };
    }
  } else if (field === "mux_role") {
    fields.mux_role = target.value;
    fields.mux_value = dbcParseNumber(form.querySelector('[data-field="mux_value"]').value) ?? 0;
  } else if (target.dataset.receiver !== undefined) {
    fields.receivers = [...form.querySelectorAll("[data-receiver]")].filter(box => box.checked).map(box => box.dataset.receiver);
  } else if (["name", "unit", "comment", "byte_order", "value_type"].includes(field)) {
    fields[field] = field === "name" ? target.value.trim() : target.value;
  } else return;
  if (field === "name") dbcUi.selected.signal = fields.name;
  await dbcCommit([{ op: "set_signal", message: entry.key, signal: signal.name, fields }]);
}

function dbcCollectChoices(root) {
  const choices = [];
  for (const row of root.querySelectorAll("tbody tr")) {
    const valueNode = row.querySelector('[data-choice="value"]');
    const label = row.querySelector('[data-choice="label"]').value.trim();
    const raw = valueNode.value.trim();
    if (raw === "" && label === "") continue;
    const value = /^0x/i.test(raw) ? parseInt(raw, 16) : Number(raw);
    if (raw === "" || !Number.isInteger(value)) { dbcMarkInvalid(valueNode, "取值必须是整数"); return null; }
    if (!label) { dbcMarkInvalid(row.querySelector('[data-choice="label"]'), "含义不能为空"); return null; }
    choices.push({ value, label });
  }
  return choices;
}

async function dbcApplyTable(entry, select) {
  const signal = dbcSelectedSignal();
  const table = dbcCatalog().value_tables.find(item => item.name === select.value);
  if (!signal || !table) return;
  await dbcCommit([{ op: "set_signal", message: entry.key, signal: signal.name, fields: { choices: table.choices } }]);
}

async function dbcCommitChoices(entry, form) {
  const signal = dbcSelectedSignal();
  const choices = dbcCollectChoices(form);
  if (!signal || choices === null) return;
  await dbcCommit([{ op: "set_signal", message: entry.key, signal: signal.name, fields: { choices } }]);
}

async function dbcCommitTable(form) {
  const choices = dbcCollectChoices(form);
  if (choices === null) return;
  await dbcCommit([{ op: "set_value_table", name: form.dataset.table, choices }]);
}

/* ---------- 动作 ---------- */

function dbcNextFreeId(catalog) {
  const used = new Set(catalog.messages.filter(m => !m.extended).map(m => m.id));
  let id = Math.max(0x100, ...[...used].map(value => value + 1).filter(value => value <= 0x7FF), 0x100);
  while (used.has(id) && id < 0x7FF) id += 1;
  return id;
}

function dbcFreeRun(entry, length = 8) {
  const used = new Set(entry.signals.flatMap(signal => signal.bits));
  const limit = Math.max(entry.dlc * 8, 8);
  for (let start = 0; start + length <= limit; start += 8) {
    let free = true;
    for (let bit = start; bit < start + length; bit += 1) if (used.has(bit)) { free = false; break; }
    if (free) return { start, length };
  }
  for (let bit = 0; bit < limit; bit += 1) if (!used.has(bit)) return { start: bit, length: 1 };
  return { start: 0, length: 1 };
}

async function dbcEditorClick(event) {
  const actor = event.target.closest("[data-act]");
  const row = event.target.closest("#dbcSignalList tr[data-signal]");
  if (!actor && row) { dbcSelectMessage(dbcUi.selected.key, row.dataset.signal); return; }
  if (!actor) return;
  const act = actor.dataset.act;
  const entry = dbcMessageEntry();
  const catalog = dbcCatalog();
  const readonly = dbcReadonly();
  if (act === "goto") {
    dbcEdit.sub = "message";
    dbcSelectMessage(actor.dataset.key, actor.dataset.signal || null);
    return;
  }
  if (readonly) return;
  if (act === "msg-new") return dbcNewMessage();
  if (act === "node-new") return dbcNewNode();
  if (act === "table-new") return dbcNewTable();
  if (act === "attr-new") return dbcNewAttribute();
  if (act === "node-rename" || act === "node-del") return dbcNodeAction(act, event.target.closest("tr").dataset.node);
  if (act === "table-del") return void await dbcCommit([{ op: "set_value_table", name: actor.dataset.table, choices: [] }]);
  if (act === "table-row") {
    const table = actor.closest("[data-form]").querySelector("tbody");
    table.insertAdjacentHTML("beforeend", dbcChoiceRowHtml("", "", ""));
    table.lastElementChild.querySelector("input").focus();
    return;
  }
  if (act === "attr-del") return void await dbcCommit([{ op: "delete_attr_def", scope: actor.dataset.scope, name: actor.dataset.name }]);
  if (!entry) return;
  if (act === "choice-add") {
    const body = $('#dbcEditor [data-form="choices"] tbody');
    body?.insertAdjacentHTML("beforeend", dbcChoiceRowHtml("", "", ""));
    body?.lastElementChild.querySelector("input").focus();
    return;
  }
  if (act === "choice-del") {
    const form = actor.closest('[data-form]');
    actor.closest("tr").remove();
    if (form.dataset.form === "choices") dbcCommitChoices(entry, form);
    else dbcCommitTable(form);
    return;
  }
  if (act === "msg-dup") return dbcDuplicateMessage(entry, catalog);
  if (act === "msg-del") return dbcDeleteMessage(entry);
  if (act === "msg-up" || act === "msg-down") {
    const index = catalog.messages.findIndex(message => message.key === entry.key);
    return void await dbcCommit([{ op: "move_message", message: entry.key, index: index + (act === "msg-up" ? -1 : 1) }]);
  }
  if (act === "sig-new") return dbcNewSignal(entry);
  const signal = dbcSelectedSignal();
  if (!signal) return;
  if (act === "sig-dup") {
    const values = await dbcPrompt({ title: "复制信号", fields: [{ name: "name", label: "新信号名称", value: `${signal.name}_2`, required: true }], ok: "复制" });
    if (!values) return;
    if (await dbcCommit([{ op: "duplicate_signal", message: entry.key, signal: signal.name, name: values.name }])) {
      dbcUi.selected.signal = values.name;
      toast("已复制；新信号与原信号位置相同，请先拖到空位再保存");
      renderDbc();
    }
  } else if (act === "sig-del") {
    const answer = await dbcPrompt({ title: "删除信号", message: `删除信号 ${signal.name}？它的注释、值表等相关语句会一并删除，可用“撤销”恢复。`, fields: [], ok: "删除", danger: true });
    if (answer) { dbcUi.selected.signal = null; await dbcCommit([{ op: "delete_signal", message: entry.key, signal: signal.name }]); }
  } else if (act === "sig-up" || act === "sig-down") {
    const index = entry.signals.findIndex(item => item.name === signal.name);
    await dbcCommit([{ op: "move_signal", message: entry.key, signal: signal.name, index: index + (act === "sig-up" ? -1 : 1) }]);
  }
}

function dbcMessageFields(catalog, preset = {}) {
  return [
    { name: "name", label: "报文名称", value: preset.name || "", required: true },
    { name: "id", label: "CAN ID", hint: "十六进制", value: (preset.id ?? dbcNextFreeId(catalog)).toString(16).toUpperCase(), required: true },
    { name: "extended", label: "帧类型", type: "select", value: String(!!preset.extended),
      options: [{ value: "false", label: "标准 11 位" }, { value: "true", label: "扩展 29 位" }] },
    { name: "dlc", label: "DLC", type: "number", value: preset.dlc ?? 8, min: 0, max: 8, required: true },
    { name: "sender", label: "发送节点", type: "select", value: preset.sender || "",
      options: [{ value: "", label: "（无）" }, ...catalog.nodes.map(node => ({ value: node.name, label: node.name }))] },
    { name: "cycle", label: "周期", hint: "ms，可留空", type: "number", value: preset.cycle ?? "", min: 0, max: 65535 },
  ];
}

function dbcParseMessageValues(values) {
  const idText = values.id.replace(/^0x/i, "");
  const extended = values.extended === "true";
  const id = /^[0-9a-f]+$/i.test(idText) ? parseInt(idText, 16) : NaN;
  if (!Number.isInteger(id) || id < 0 || id > (extended ? 0x1FFFFFFF : 0x7FF)) { toast(extended ? "扩展 ID 必须在 0..1FFFFFFF" : "标准 ID 必须在 0..7FF", true); return null; }
  return { id, extended };
}

async function dbcNewMessage(preset = {}) {
  const catalog = dbcCatalog();
  if (!catalog) return;
  const values = await dbcPrompt({ title: "新建报文", fields: dbcMessageFields(catalog, preset), ok: "新建" });
  if (!values) return;
  const parsed = dbcParseMessageValues(values);
  if (!parsed) return;
  const cycle = values.cycle === "" || Number.isNaN(values.cycle) ? null : Number(values.cycle);
  const ok = await dbcCommit([{ op: "add_message", name: values.name, id: parsed.id, extended: parsed.extended, dlc: values.dlc,
    sender: values.sender || "", cycle_time: cycle }], { key: dbcKey(parsed.id, parsed.extended) });
  if (ok) { dbcEdit.sub = "message"; dbcUi.selected.signal = null; renderDbc(); }
}

async function dbcDuplicateMessage(entry, catalog) {
  const values = await dbcPrompt({ title: "复制报文", fields: dbcMessageFields(catalog, { name: `${entry.name}_2`, id: dbcNextFreeId(catalog), extended: entry.extended }).slice(0, 3), ok: "复制" });
  if (!values) return;
  const parsed = dbcParseMessageValues(values);
  if (!parsed) return;
  await dbcCommit([{ op: "duplicate_message", message: entry.key, name: values.name, id: parsed.id, extended: parsed.extended }],
    { key: dbcKey(parsed.id, parsed.extended) });
}

async function dbcDeleteMessage(entry) {
  const answer = await dbcPrompt({ title: "删除报文", message: `删除报文 ${entry.name} 及其全部信号？可用“撤销”恢复。`, fields: [], ok: "删除", danger: true });
  if (!answer) return;
  dbcUi.selected = { key: null, signal: null };
  await dbcCommit([{ op: "delete_message", message: entry.key }]);
}

async function dbcNewSignal(entry) {
  const existing = new Set(entry.signals.map(signal => signal.name));
  let number = entry.signals.length + 1;
  while (existing.has(`Signal_${number}`)) number += 1;
  const name = `Signal_${number}`;
  const run = dbcFreeRun(entry, 8);
  const ok = await dbcCommit([{ op: "add_signal", message: entry.key, fields: { name, start: run.start, length: run.length,
    byte_order: dbcEdit.newOrder === "motorola" ? "motorola" : "intel", factor: 1, offset: 0, minimum: 0, maximum: 0 } }]);
  if (ok) { dbcUi.selected.signal = name; renderDbc(); }
}

async function dbcCreateSignalFromRange(entry, range) {
  const existing = new Set(entry.signals.map(signal => signal.name));
  let number = entry.signals.length + 1;
  while (existing.has(`Signal_${number}`)) number += 1;
  const name = `Signal_${number}`;
  const ok = await dbcCommit([{ op: "add_signal", message: entry.key, fields: { name, start: range.start, length: range.length,
    byte_order: dbcEdit.newOrder, factor: 1, offset: 0, minimum: 0, maximum: 0 } }]);
  if (ok) { dbcUi.selected.signal = name; renderDbc(); }
}

async function dbcNewNode() {
  const values = await dbcPrompt({ title: "新建节点", fields: [{ name: "name", label: "节点名称", required: true }, { name: "comment", label: "说明" }], ok: "新建" });
  if (values) await dbcCommit([{ op: "add_node", name: values.name, comment: values.comment }]);
}

async function dbcNodeAction(act, name) {
  if (act === "node-rename") {
    const values = await dbcPrompt({ title: "节点改名", message: "报文发送者、信号接收者、注释和属性里的引用会一起更新。", fields: [{ name: "name", label: "新名称", value: name, required: true }], ok: "改名" });
    if (values && values.name !== name) await dbcCommit([{ op: "rename_node", node: name, name: values.name }]);
  } else {
    const answer = await dbcPrompt({ title: "删除节点", message: `删除节点 ${name}？仍作为报文发送者时会被拒绝。`, fields: [], ok: "删除", danger: true });
    if (answer) await dbcCommit([{ op: "delete_node", node: name }]);
  }
}

async function dbcNewTable() {
  const values = await dbcPrompt({ title: "新建全局值表", fields: [{ name: "name", label: "值表名称", required: true }], ok: "新建" });
  if (values) await dbcCommit([{ op: "set_value_table", name: values.name, choices: [{ value: 0, label: "0" }] }]);
}

async function dbcNewAttribute() {
  const values = await dbcPrompt({ title: "新建属性定义", fields: [
    { name: "scope", label: "作用范围", type: "select", value: "BO_", options: [{ value: "", label: "全局" }, { value: "BO_", label: "报文" }, { value: "SG_", label: "信号" }, { value: "BU_", label: "节点" }] },
    { name: "name", label: "属性名称", required: true },
    { name: "type", label: "类型", type: "select", value: "INT", options: ["INT", "HEX", "FLOAT", "STRING", "ENUM"].map(value => ({ value, label: value })) },
    { name: "args", label: "取值范围或枚举", hint: "例：0 65535 或 \"A\",\"B\"" },
    { name: "default", label: "默认值", hint: "字符串要带引号" }], ok: "新建" });
  if (values) await dbcCommit([{ op: "set_attr_def", scope: values.scope, name: values.name, type: values.type, args: values.args, default: values.default || null }]);
}

/** “未识别帧”一键建模：用总线上看到的 ID、DLC 和实测周期预填新建报文。 */
async function dbcModelFromFrame(frame) {
  if (!dbcCurrentFile()) {
    const result = await state.api.dbc_new("未命名.dbc");
    if (!result?.ok) return toast(result?.error || "无法新建 DBC", true);
    await state.api.dbc_set_bus(result.file.id, dbcLiveFor()?.bus === "canb" ? "canb" : "can1");
    dbcUi.fileId = result.file.id;
    await dbcSync();
  }
  if (dbcReadonly()) return toast("当前 DBC 是只读的，请先另存为可编辑副本或新建一个 DBC", true);
  dbcUi.tab = "edit";
  dbcEdit.sub = "message";
  renderDbc();
  await dbcNewMessage({ name: `Frame_${frame.id.toString(16).toUpperCase()}`, id: frame.id, extended: frame.extended,
    dlc: Math.min(frame.dlc, 8), cycle: frame.cycle_ms ? Math.round(frame.cycle_ms) : "" });
}

/* ---------- 撤销 / 保存 / 评审 ---------- */

async function dbcUndoRedo(which) {
  const file = dbcCurrentFile();
  if (!file) return;
  const result = await (which === "undo" ? state.api.dbc_undo(file.id) : state.api.dbc_redo(file.id));
  if (!result?.ok) return toast(result?.error || "操作失败", true);
  await dbcRefreshState();
  dbcUi.signatures = {};
  renderDbc();
}

async function dbcAfterSave(result) {
  if (!result?.ok) {
    if (result?.blockers?.length) { dbcEdit.sub = "check"; renderDbc(); }
    toast(result?.error || "保存失败", true);
    return false;
  }
  for (const warning of result.warnings || []) toast(warning, true);
  toast(`已保存 ${result.path}${result.backup ? "（旧内容在 .bak）" : ""}`);
  await dbcRefreshState();
  dbcUi.signatures = {};
  renderDbc();
  return true;
}

async function dbcSave() {
  const file = dbcCurrentFile();
  if (!file) return;
  if (!file.path) return dbcSaveAs();
  await dbcAfterSave(await state.api.dbc_save(file.id, {}));
}

async function dbcSaveAs(options = {}) {
  const file = dbcCurrentFile();
  if (!file) return;
  const result = await state.api.dbc_choose_save(file.id, options);
  if (result?.cancelled) return;
  await dbcAfterSave(result);
}

function dbcDiffHtml(lines) {
  return lines.map(line => {
    const cls = line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("@@") ? "hunk" : "";
    return `<span class="${cls}">${escapeHtml(line)}</span>`;
  }).join("\n");
}

/** 评审与对比共用：语义变化列表加文本差异。 */
function dbcChangesHtml(review, summaryExtra = "") {
  const labels = { add: "新增", delete: "删除", change: "修改" };
  const scopes = { node: "节点", message: "报文", signal: "信号", table: "值表", attribute: "属性" };
  const changes = review.changes.map(change => `<div class="dbc-change"><span class="tag ${change.op}">${labels[change.op]}</span>`
    + `<div><b>${scopes[change.scope]} ${escapeHtml(change.message ? `${change.message}.` : "")}${escapeHtml(change.target)}</b>${change.detail ? ` <span class="note tight">${escapeHtml(change.detail)}</span>` : ""}`
    + (change.changes.length ? `<ul>${change.changes.map(item => `<li><b>${escapeHtml(item.field)}</b> ${escapeHtml(item.old)} → ${escapeHtml(item.new)}</li>`).join("")}</ul>` : "") + "</div></div>").join("");
  return `<div class="dbc-review-summary"><span class="tag ok">新增 ${review.counts.add}</span><span class="tag bad">删除 ${review.counts.delete}</span>`
    + `<span class="tag warn">修改 ${review.counts.change}</span>${summaryExtra}</div>`
    + `<div class="dbc-review-list">${changes || '<p class="note">没有语义变化（可能只是格式）。</p>'}</div>`
    + `<div class="dbc-subhead"><span>文本差异${review.text.truncated ? `（仅前 ${review.text.lines.length} 行 / 共 ${review.text.total} 行）` : ""}</span></div>`
    + `<pre class="dbc-diff">${dbcDiffHtml(review.text.lines) || "没有文本差异"}</pre>`;
}

function dbcShowChanges(title, html, { saving }) {
  $("#dbcReviewBody").innerHTML = html;
  text("#dbcReviewTitle", title);
  $("#dbcReviewSave").classList.toggle("hidden", !saving);
  $$("#dbcReviewDialog footer .dbc-review-option").forEach(node => node.classList.toggle("hidden", !saving));
  $("#dbcReviewDialog").showModal();
}

async function dbcReview() {
  const file = dbcCurrentFile();
  if (!file) return;
  const review = await state.api.dbc_save_review(file.id);
  if (!review?.ok) return toast(review?.error || "无法生成评审", true);
  const blockers = review.blockers.length
    ? `<div class="db-notice" role="alert">还有 ${review.blockers.length} 个错误，修正后才能保存：${escapeHtml(review.blockers.slice(0, 3).map(issue => issue.message).join("；"))}</div>` : "";
  $("#dbcReviewCandbpp").checked = false;
  $("#dbcReviewEncoding").value = "";
  dbcShowChanges(`保存前评审 · ${file.name}`, blockers + dbcChangesHtml(review, `<span class="tag neutral">编码 ${escapeHtml(file.encoding_label)}</span>`), { saving: true });
  $("#dbcReviewSave").disabled = review.blockers.length > 0 || file.readonly;
}

async function dbcCompare() {
  const current = dbcCurrentFile();
  const others = dbcUi.files.filter(file => file.id !== current?.id);
  if (!current || !others.length) return toast("至少打开两个 DBC 才能对比", true);
  const values = await dbcPrompt({ title: "对比 DBC", message: `以 ${current.name} 为基准，列出另一个文件相对它的变化。`,
    fields: [{ name: "other", label: "对比对象", type: "select", value: others[0].id, options: others.map(file => ({ value: file.id, label: file.name })) }],
    ok: "对比" });
  if (!values) return;
  const result = await state.api.dbc_compare(current.id, values.other);
  if (!result?.ok) return toast(result?.error || "对比失败", true);
  dbcShowChanges(`对比 · ${result.first} → ${result.second}`, dbcChangesHtml(result), { saving: false });
}

async function dbcReviewSave() {
  const file = dbcCurrentFile();
  if (!file) return;
  const options = { candbpp: $("#dbcReviewCandbpp").checked };
  const encoding = $("#dbcReviewEncoding").value;
  if (encoding) options.encoding = encoding;
  $("#dbcReviewDialog").close();
  if (!file.path) return dbcSaveAs(options);
  await dbcAfterSave(await state.api.dbc_save(file.id, options));
}

/* ---------- 位图拖选 ---------- */

function dbcDragCells() {
  const drag = dbcEdit.drag;
  if (!drag) return [];
  const signal = dbcSelectedSignal();
  const order = signal ? signal.byte_order : dbcEdit.newOrder;
  const range = dbcRangeFromCells(drag.a, drag.b, order);
  return dbcSignalBits(range.start, range.length, order);
}

function dbcPaintDrag() {
  const cells = new Set(dbcDragCells());
  $$("#dbcEditor .dbc-bitgrid td").forEach(cell => cell.classList.toggle("drag", cells.has(Number(cell.dataset.bit))));
}

function dbcFinishDrag() {
  const drag = dbcEdit.drag;
  dbcEdit.drag = null;
  $$("#dbcEditor .dbc-bitgrid td.drag").forEach(cell => cell.classList.remove("drag"));
  if (!drag) return;
  const entry = dbcMessageEntry();
  if (!entry) return;
  if (drag.a === drag.b) {
    const owner = entry.signals.find(signal => signal.bits.includes(drag.a));
    if (owner) dbcSelectMessage(entry.key, owner.name);
    return;
  }
  if (dbcReadonly()) return;
  const signal = dbcSelectedSignal();
  const range = dbcRangeFromCells(drag.a, drag.b, signal ? signal.byte_order : dbcEdit.newOrder);
  if (range.length > 64) return toast("信号最长 64 位", true);
  if (signal) dbcCommit([{ op: "set_signal", message: entry.key, signal: signal.name, fields: { start: range.start, length: range.length } }]);
  else dbcCreateSignalFromRange(entry, range);
}

/* ---------- 事件 ---------- */

function bindDbcEditorControls() {
  const root = $("#dbcEditor");
  root?.addEventListener("keydown", event => {
    const cell = event.target.closest(".dbc-bitgrid td[data-bit]");
    if (!cell || !["Enter", " "].includes(event.key)) return;
    event.preventDefault();
    const owner = dbcMessageEntry()?.signals.find(signal => signal.bits.includes(Number(cell.dataset.bit)));
    if (owner) dbcSelectMessage(dbcUi.selected.key, owner.name);
  });
  root?.addEventListener("change", dbcEditorChange);
  root?.addEventListener("click", dbcEditorClick);
  root?.addEventListener("mousedown", event => {
    const cell = event.target.closest(".dbc-bitgrid td"); if (!cell || event.button !== 0) return;
    event.preventDefault();
    dbcEdit.drag = { a: Number(cell.dataset.bit), b: Number(cell.dataset.bit) };
    dbcPaintDrag();
  });
  root?.addEventListener("mouseover", event => {
    const cell = event.target.closest(".dbc-bitgrid td"); if (!cell || !dbcEdit.drag) return;
    dbcEdit.drag.b = Number(cell.dataset.bit);
    dbcPaintDrag();
  });
  document.addEventListener("mouseup", () => { if (dbcEdit.drag) dbcFinishDrag(); });
  root?.addEventListener("click", event => {
    const button = event.target.closest("#dbcNewOrder button"); if (!button) return;
    dbcEdit.newOrder = button.dataset.order;
    dbcUi.signatures.editor = "";
    renderDbc();
  });
  $("#dbcEditTabs")?.addEventListener("click", event => {
    const button = event.target.closest("button"); if (!button) return;
    dbcEdit.sub = button.dataset.sub;
    dbcUi.signatures.editor = "";
    renderDbc();
  });
  $("#dbcUndo")?.addEventListener("click", () => dbcUndoRedo("undo"));
  $("#dbcRedo")?.addEventListener("click", () => dbcUndoRedo("redo"));
  $("#dbcSave")?.addEventListener("click", dbcSave);
  $("#dbcSaveAs")?.addEventListener("click", () => dbcSaveAs());
  $("#dbcReview")?.addEventListener("click", dbcReview);
  $("#dbcCompare")?.addEventListener("click", dbcCompare);
  $("#dbcReviewSave")?.addEventListener("click", dbcReviewSave);
  window.addEventListener("keydown", event => {
    if (state.page !== "dbc" || !(event.ctrlKey || event.metaKey) || event.altKey) return;
    const key = event.key.toLowerCase();
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "");
    if (key === "s") { event.preventDefault(); dbcSave(); }
    else if (!typing && dbcUi.tab === "edit" && key === "z") { event.preventDefault(); dbcUndoRedo(event.shiftKey ? "redo" : "undo"); }
    else if (!typing && dbcUi.tab === "edit" && key === "y") { event.preventDefault(); dbcUndoRedo("redo"); }
  });
}
