/* 台架模拟从控页面模块：CAN1 从控帧注入工具。 */

function bindBenchControls() {
  $("#connectBenchButton").addEventListener("click", connectBench);
  $("#disconnectBenchButton").addEventListener("click", disconnectBench);
  $("#sendBenchCommand").addEventListener("click", sendBenchCommand);
  $("#benchCommand").addEventListener("keydown", event => {
    if (event.key === "Enter") sendBenchCommand();
  });
  $$("#page-bench [data-bench-command]").forEach(button => {
    button.addEventListener("click", () => runBenchCommand(button.dataset.benchCommand));
  });
}

async function connectBench() {
  if (!state.api) return toast("应用后端未就绪", true);
  const button = $("#connectBenchButton");
  button.disabled = true; button.textContent = "连接中…";
  const result = await state.api.connect_bench({ channel: $("#benchChannelSelect").value });
  button.disabled = false; button.textContent = "连接";
  if (!result.ok) return toast(result.error || "台架连接失败", true);
  toast("台架已连接到 F405 CAN1");
  await poll();
}

async function disconnectBench() {
  if (!state.api) return;
  await state.api.disconnect_bench();
  toast("台架已断开");
  await poll();
}

function renderBench() {
  const snapshot = state.toolSnapshots.bench || {};
  const bench = snapshot.bench;
  const connection = snapshot.connection || {};
  const active = connection.connected === true && connection.mode === "bench" && bench?.active === true;
  const controls = $$("#page-bench [data-bench-command], #sendBenchCommand, #benchCommand");
  controls.forEach(node => { node.disabled = !active; });
  $("#connectBenchButton").classList.toggle("hidden", active);
  $("#disconnectBenchButton").classList.toggle("hidden", !active);
  if (!active) {
    text("#benchStatus", "未连接");
    text("#benchFrameSummary", "未连接");
    $("#benchStatus").className = "tag neutral";
    $("#benchSlaves").innerHTML = '<div class="empty-state">连接 CAN1 后显示六个从控模型。</div>';
    return;
  }
  const online = (bench.slaves || []).filter(slave => slave.online).length;
  text("#benchStatus", "运行中 · " + online + "/6 在线");
  $("#benchStatus").className = "tag " + (online === 6 ? "ok" : "warn");
  text("#benchFrameSummary", `36 帧电压 + 6 帧温度 · 断线 ${bench.open_wire_cells || 0} 串 / ${bench.open_wire_temps || 0} 路`);
  $("#benchSlaves").innerHTML = (bench.slaves || []).map(slave =>
    `<div class="bench-slave ${slave.online ? "online" : "offline"}">`
    + `<span>从控 ${slave.id}</span><b>${slave.online ? "在线" : "离线"}</b>`
    + `<small>${slave.base_cell_mv} mV · ${slave.base_temp_c} °C</small></div>`
  ).join("");
}

async function runBenchCommand(command) {
  if (!state.api) return;
  const result = await state.api.bench_command(command);
  if (!result.ok) return toast(result.error || "台架命令失败", true);
  text("#benchResult", result.message || "命令已执行");
  await poll();
}

async function sendBenchCommand() {
  const input = $("#benchCommand");
  const command = input.value.trim();
  if (!command) return;
  await runBenchCommand(command);
  input.value = "";
}
