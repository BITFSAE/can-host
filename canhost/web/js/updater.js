/* 软件内更新：检查 Release、下载校验、退出安装。
 * 只使用零构建原生脚本；后端负责文件校验与替换，前端不接触安装目录。 */

var updaterPollTimer = null;
var updaterAutoStarted = false;
var updaterClosing = false;
var updaterDownloadStarted = false;
var updateResultShown = false;
var updaterExternalLinksBound = false;

function initUpdater() {
  if (!state.api || !state.bootstrap) return;
  bindUpdaterControls();
  bindExternalLinks();
  if (updaterPollTimer) return;
  updaterPollTimer = setInterval(pollUpdaterStatus, 1000);
  if (state.bootstrap.updater_check_enabled !== false && state.bootstrap.updater_repo && !updaterAutoStarted) {
    updaterAutoStarted = true;
    refreshUpdater(true);
  }
}

function bindUpdaterControls() {
  const versionFact = $("#appVersionFact");
  if (versionFact && !versionFact.dataset.bound) {
    versionFact.dataset.bound = "1";
    versionFact.addEventListener("click", () => openUpdaterDialog(true));
  }
  const check = $("#updaterCheck");
  if (check && !check.dataset.bound) {
    check.dataset.bound = "1";
    check.addEventListener("click", () => refreshUpdater(false));
  }
  const prerelease = $("#updaterPrerelease");
  if (prerelease && !prerelease.dataset.bound) {
    prerelease.dataset.bound = "1";
    prerelease.addEventListener("change", () => refreshUpdaterPending());
  }
  const install = $("#updaterInstall");
  if (install && !install.dataset.bound) {
    install.dataset.bound = "1";
    install.addEventListener("click", startUpdateIntent);
  }
  const releasePage = $("#updaterReleasePage");
  if (releasePage && !releasePage.dataset.bound) {
    releasePage.dataset.bound = "1";
    releasePage.addEventListener("click", () => openReleasePage(currentReleaseUrl()));
  }
  const history = $("#updaterHistory");
  if (history && !history.dataset.bound) {
    history.dataset.bound = "1";
    history.addEventListener("click", () => openReleaseHistoryDialog());
  }
  bindUpdateResultControls();
  bindReleaseHistoryControls();
  const saveToken = $("#updaterSaveToken");
  if (saveToken && !saveToken.dataset.bound) {
    saveToken.dataset.bound = "1";
    saveToken.addEventListener("click", async () => {
      const token = $("#updaterTokenInput")?.value?.trim() || "";
      const result = await state.api.save_update_token(token);
      if (!result.ok) {
        text("#updaterError", result.error || "无法保存令牌");
      } else {
        text("#updaterError", "令牌已保存");
        $("#updaterTokenInput").value = "";
        await refreshUpdater(false);
      }
    });
  }
  const clearToken = $("#updaterClearToken");
  if (clearToken && !clearToken.dataset.bound) {
    clearToken.dataset.bound = "1";
    clearToken.addEventListener("click", async () => {
      const result = await state.api.clear_update_token();
      if (!result.ok) {
        text("#updaterError", result.error || "无法清除令牌");
      } else {
        text("#updaterError", "令牌已清除");
        await refreshUpdater(false);
      }
    });
  }
}

function bindUpdateResultControls() {
  const releasePage = $("#updateResultReleasePage");
  if (releasePage && !releasePage.dataset.bound) {
    releasePage.dataset.bound = "1";
    releasePage.addEventListener("click", () => openReleasePage(currentReleaseUrl()));
  }
  const history = $("#updateResultHistory");
  if (history && !history.dataset.bound) {
    history.dataset.bound = "1";
    history.addEventListener("click", () => openReleaseHistoryDialog());
  }
}

function bindReleaseHistoryControls() {
  const fetchButton = $("#releaseHistoryFetch");
  if (fetchButton && !fetchButton.dataset.bound) {
    fetchButton.dataset.bound = "1";
    fetchButton.addEventListener("click", () => loadReleaseHistory(true));
  }
}

/* 更新完成弹窗：安装包升级和软件内更新走同一条路径，必须显示版本变化和本次改了什么。 */
function showUpdateResult(data) {
  const dialog = $("#updateResultDialog");
  if (!dialog || !data || updateResultShown) return;
  updateResultShown = true;
  state.updateResult = data;
  text("#updateResultPrevious", data.previous_version ? "v" + data.previous_version : "首次安装");
  text("#updateResultCurrent", data.current_version ? "v" + data.current_version : "等待数据");
  text("#updateResultDate", String(data.version_date || data.published_at || "").slice(0, 10) || "—");
  renderReleaseNotes($("#updateResultNotes"), data.notes || []);
  const fallback = $("#updateResultFallback");
  if (fallback) {
    const notes = (data.notes || []).filter(note => String(note || "").trim());
    fallback.innerHTML = notes.length
      ? "完整说明见 " + linkMarkup(currentReleaseUrl(), "发布页") + "。"
      : "此版本没有登记条目；" + linkMarkup(currentReleaseUrl(), "在发布页查看完整说明") + "。";
    fallback.classList.remove("hidden");
  }
  if (!dialog.open) dialog.showModal();
}

function currentReleaseUrl() {
  return state.updateResult?.release_url
    || state.updater?.latest?.html_url
    || state.bootstrap?.release_url
    || "https://github.com/BITFSAE/can-host/releases";
}

function linkMarkup(url, label) {
  const safe = /^https:\/\//i.test(String(url || "")) ? String(url) : "";
  return safe
    ? '<a href="' + escapeHtml(safe) + '" data-external="1">' + escapeHtml(label) + "</a>"
    : escapeHtml(label);
}

function renderReleaseNotes(list, notes, emptyText) {
  if (!list) return;
  const items = (Array.isArray(notes) ? notes : [notes]).filter(note => String(note || "").trim());
  list.innerHTML = items.length
    ? items.map(note => "<li>" + escapeHtml(String(note)) + "</li>").join("")
    : '<li class="release-notes-empty">' + escapeHtml(emptyText || "此版本没有登记条目。") + "</li>";
}

async function openReleasePage(url) {
  if (!state.api?.open_release_page) { toast("当前版本无法打开浏览器", true); return; }
  const result = await state.api.open_release_page(String(url || ""));
  if (!result?.ok) toast(result?.error || "无法打开浏览器", true);
}

/* 版本历史：优先随包说明，按需联网核对最近发布。 */
async function openReleaseHistoryDialog() {
  const dialog = $("#releaseHistoryDialog");
  if (!dialog) return;
  if (!dialog.open) dialog.showModal();
  await loadReleaseHistory(false);
}

async function loadReleaseHistory(online) {
  if (!state.api?.release_history) return;
  const list = $("#releaseHistoryList");
  const error = $("#releaseHistoryError");
  if (error) error.classList.add("hidden");
  if (list && !list.childElementCount) list.innerHTML = '<p class="note tight">正在读取版本说明…</p>';
  try {
    const result = await state.api.release_history(!!online);
    renderReleaseHistory(result);
  } catch (failure) {
    if (list) list.innerHTML = "";
    if (error) {
      error.textContent = failure?.message || "无法读取版本说明";
      error.classList.remove("hidden");
    }
  }
}

function renderReleaseHistory(data) {
  const list = $("#releaseHistoryList");
  const error = $("#releaseHistoryError");
  const hint = $("#releaseHistoryHint");
  if (!list) return;
  if (error) error.classList.add("hidden");
  const entries = Array.isArray(data?.entries) ? data.entries : [];
  if (hint) {
    hint.textContent = data?.online
      ? "已合并随包说明和最近一次检查到的 Release。"
      : "显示随包版本说明；联网获取可核对最新发布。";
  }
  if (!entries.length) {
    list.innerHTML = '<p class="note tight">当前版本没有可显示的版本说明。</p>';
    return;
  }
  const current = String(data?.current_version || "");
  list.innerHTML = entries.map(entry => {
    const version = String(entry.version || "");
    const notes = (entry.notes || []).map(note => "<li>" + escapeHtml(String(note)) + "</li>").join("");
    const source = entry.source === "release" ? '<em class="release-history-source">Release</em>' : "";
    const isCurrent = version && version === current ? '<em class="release-history-current">当前</em>' : "";
    return '<article class="release-history-entry' + (version === current ? " current" : "") + '">'
      + "<header><b>v" + escapeHtml(version) + "</b>" + isCurrent + source
      + "<span>" + escapeHtml(String(entry.date || "")) + "</span></header>"
      + (notes ? "<ul>" + notes + "</ul>" : '<p class="note tight">该版本没有登记条目。</p>')
      + "</article>";
  }).join("");
}

function bindExternalLinks() {
  // core.js 的首次初始化与 DOMContentLoaded 都会走到 initUpdater()；重复
  // 注册会让一次点击调用两次后端，打开两个浏览器标签，因此只绑定一次。
  if (updaterExternalLinksBound) return;
  updaterExternalLinksBound = true;
  document.addEventListener("click", event => {
    const link = event.target.closest("a[data-external]");
    if (!link) return;
    event.preventDefault();
    openReleasePage(link.getAttribute("href"));
  });
}

async function openUpdaterDialog(startDownload = false) {
  const dialog = $("#updaterDialog");
  if (!dialog) return;
  if (!dialog.open) dialog.showModal();
  updaterRenderUpdaterStatus();
  await pollUpdaterStatus();
  // Source runs are allowed to check but not install. Opening the entry point
  // should still provide a useful result when startup auto-check is disabled.
  if (updaterStatusIsIdle()) await refreshUpdater(false);
  if (startDownload && state.updater?.state === "update_available" && state.updater?.install_supported) {
    await startUpdateIntent();
  }
}

async function refreshUpdater(automatic) {
  if (!state.api?.check_for_updates) return;
  const include = !!$("#updaterPrerelease")?.checked;
  const result = automatic
    ? await state.api.auto_check_for_updates()
    : await state.api.check_for_updates(include);
  if (!result.ok && result.state !== "checking") {
    text("#updaterError", result.error || "检查更新失败");
  }
  await pollUpdaterStatus();
}

function refreshUpdaterPending() {
  const stateName = getUpdaterExtra()?.state || "idle";
  if (["idle", "up_to_date", "update_available", "check_failed"].includes(stateName)) refreshUpdater(false);
  else updaterRenderUpdaterStatus();
}

function updaterStatusIsIdle() {
  return getUpdaterExtra()?.state === "idle";
}

function getUpdaterExtra() {
  return state.updater || {};
}

async function pollUpdaterStatus() {
  if (!state.api?.get_updater_status) return;
  const status = await state.api.get_updater_status();
  if (status) state.updater = status;
  updaterRenderUpdaterStatus();
  if (updaterDownloadStarted && status?.state === "ready") {
    updaterDownloadStarted = false;
    updaterRenderUpdaterStatus();
  } else if (updaterDownloadStarted && ["download_failed", "install_failed"].includes(status?.state)) {
    updaterDownloadStarted = false;
    updaterRenderUpdaterStatus();
  }
}

function updaterRenderUpdaterStatus() {
  const status = state.updater || {};
  const latest = status.latest || null;
  const hasToken = !!status.has_token;
  const installSupported = !!status.install_supported;
  const currentVersion = state.bootstrap?.version;
  const stateName = status.state || "idle";

  const headline = $("#updaterHeadline");
  if (headline) headline.textContent = updaterHeadline(status);
  const badge = $("#updaterStateBadge");
  if (badge) {
    badge.textContent = updaterBadgeText(status);
    badge.className = "updater-state-badge";
    const badgeClass = updaterBadgeClass(stateName);
    if (badgeClass) badge.classList.add(badgeClass);
  }

  const messageNode = $("#updaterMessage");
  if (messageNode) {
    messageNode.textContent = updaterStatusText(status);
    messageNode.className = "updater-status-line";
    if (["update_available", "ready", "checking", "downloading"].includes(stateName)) {
      messageNode.classList.add("active");
    }
    if (["check_failed", "download_failed", "install_failed"].includes(stateName)) {
      messageNode.classList.add("bad");
    }
    if (stateName === "up_to_date" || stateName === "ready") messageNode.classList.add("ok");
  }

  const current = $("#updaterCurrent");
  if (current) current.textContent = currentVersion ? "v" + currentVersion : "等待数据";
  const latestNode = $("#updaterLatest");
  if (latestNode) latestNode.textContent = latest?.tag_name ? "v" + String(latest.tag_name).replace(/^v/i, "") : "等待数据";
  const latestCard = latestNode?.closest(".updater-version-card");
  if (latestCard) latestCard.classList.toggle("available", stateName === "update_available");
  const releaseKind = $("#updaterReleaseKind");
  if (releaseKind) releaseKind.textContent = latest
    ? (latest.prerelease ? "预发布版 · 需谨慎安装" : "正式版 · 可直接更新")
    : "等待检查";
  const dateNode = $("#updaterPublished");
  if (dateNode) dateNode.textContent = latest?.published_at ? String(latest.published_at).slice(0, 10) : "—";

  const notes = $("#updaterNotes");
  if (notes) {
    const changes = latest?.changes || [];
    const body = String(latest?.body || "").trim();
    renderReleaseNotes(
      notes,
      changes,
      latest
        ? (body ? "该 Release 未填写逐条说明，展开下方原文查看。" : "此次 Release 未填写说明。")
        : "检查完成后会在这里显示本版本的更新内容。",
    );
    // 逐条条目已经覆盖了正文里的“本次更新”小节，原文折叠保留给需要看全文的人。
    const rawPanel = $("#updaterNotesRawPanel");
    const raw = $("#updaterNotesRaw");
    const showRaw = !changes.length && !!body;
    if (rawPanel) rawPanel.classList.toggle("hidden", !showRaw);
    if (raw) raw.textContent = showRaw ? body.slice(0, 4000) : "";
  }

  const size = latest?.assets?.find(item => String(item.name).toLowerCase().endsWith(".zip"))?.size;
  const sizeNode = $("#updaterSize");
  if (sizeNode) sizeNode.textContent = size ? formatUpdaterSize(size) : "—";

  const progress = $("#updaterProgress");
  if (progress) {
    const pct = Math.round((Number(status.progress) || 0) * 100);
    progress.style.width = pct + "%";
    $("#updaterProgressPanel")?.classList.toggle("hidden", !["downloading", "ready"].includes(stateName));
  }
  const progressText = $("#updaterProgressText");
  if (progressText) progressText.textContent = ["downloading", "ready"].includes(stateName)
    ? Math.round((Number(status.progress) || 0) * 100) + "%" : "";
  const progressLabel = $("#updaterProgressLabel");
  if (progressLabel) progressLabel.textContent = stateName === "ready"
    ? "更新包已下载" : status.download_stage === "verifying" ? "正在校验更新包" : "下载进度";
  const progressBytes = $("#updaterProgressBytes");
  if (progressBytes) {
    const downloaded = Number(status.downloaded_bytes) || 0;
    const total = Number(status.total_bytes) || 0;
    progressBytes.textContent = total
      ? formatUpdaterSize(downloaded) + " / " + formatUpdaterSize(total)
      : downloaded ? formatUpdaterSize(downloaded) + " 已下载" : "准备下载…";
  }
  const progressSpeed = $("#updaterProgressSpeed");
  if (progressSpeed) {
    progressSpeed.textContent = stateName === "downloading" && Number(status.download_speed_bps) > 0
      ? formatUpdaterSize(status.download_speed_bps) + "/s"
      : stateName === "ready" ? "已校验" : "—";
  }
  const progressPanel = $("#updaterProgressPanel");
  if (progressPanel) progressPanel.classList.toggle("verifying", stateName === "downloading" && status.download_stage === "verifying");

  const errorNode = $("#updaterError");
  if (errorNode) {
    if (status.error) {
      errorNode.textContent = status.error;
      errorNode.classList.remove("hidden");
    } else {
      errorNode.textContent = "";
      errorNode.classList.add("hidden");
    }
  }

  const fileNode = $("#updaterFile");
  if (fileNode) {
    fileNode.textContent = status.stage_dir ? "下载与校验已完成。点击“重启更新”后，当前软件会自动退出并打开新版本。" : "";
    fileNode.classList.toggle("hidden", !status.stage_dir);
  }

  const tokenNode = $("#updaterTokenState");
  if (tokenNode) {
    tokenNode.textContent = hasToken ? "已保存只读令牌，仅私有仓库需要" : "公开仓库无需令牌，默认不保存";
    tokenNode.classList.toggle("ok", hasToken);
  }
  const clearNode = $("#updaterClearToken");
  if (clearNode) clearNode.disabled = !hasToken;

  const checkBtn = $("#updaterCheck");
  if (checkBtn) checkBtn.disabled = ["checking", "downloading", "installing"].includes(stateName);
  const installBtn = $("#updaterInstall");
  if (installBtn) {
    const canDownload = stateName === "update_available" && installSupported;
    const canInstall = stateName === "ready";
    installBtn.disabled = (!canDownload && !canInstall) || (canInstall && !installSupported) || updaterClosing || updaterDownloadStarted;
    if (stateName === "installing" || updaterClosing) installBtn.textContent = "正在退出…";
    else if (stateName === "downloading" || updaterDownloadStarted) installBtn.textContent = "下载中…";
    else if (stateName === "ready") installBtn.textContent = "重启更新";
    else if (stateName === "update_available" && latest?.tag_name) installBtn.textContent = "下载 v" + String(latest.tag_name).replace(/^v/i, "");
    else installBtn.textContent = "下载更新";
  }
  const prerelease = $("#updaterPrerelease");
  if (prerelease) prerelease.disabled = ["checking", "downloading", "installing"].includes(stateName);
  const autoNote = $("#updaterAutoNote");
  const frozenMac = state.bootstrap?.frozen === true && state.bootstrap?.runtime_platform === "darwin";
  if (autoNote) autoNote.textContent = installSupported
    ? "公开仓库默认方案：启动时自动检查一次正式版；优先 CNB 国内镜像，失败回退 GitHub；发现更新不会自动下载。"
    : frozenMac
      ? "macOS 发布版只检查版本（优先 CNB 国内镜像）；升级请下载对应 DMG 后覆盖安装。"
      : "当前为源码运行，只能检查发布（优先 CNB 国内镜像），不能替换安装目录。";
  const repoNode = $("#updaterRepo");
  if (repoNode) repoNode.textContent = state.bootstrap?.updater_repo || "BITFSAE/can-host";
  const sourceNode = $("#updaterSource");
  if (sourceNode) sourceNode.textContent = updaterSourceText(status);
  const pathNode = $("#updaterSettingsPath");
  if (pathNode) pathNode.textContent = state.bootstrap?.updater_settings_path || "—";
  const logNode = $("#updaterLogDir");
  if (logNode) logNode.textContent = state.bootstrap?.updater_log_dir || "—";

  const indicator = $("#appUpdateIndicator");
  const versionFact = $("#appVersionFact");
  const hasUpdate = ["update_available", "downloading", "ready"].includes(stateName);
  if (indicator) indicator.classList.toggle("hidden", !hasUpdate);
  if (indicator) {
    const icon = indicator.querySelector("svg");
    const indicatorText = $("#appUpdateIndicatorText");
    if (stateName === "downloading") {
      indicator.classList.add("downloading");
      indicator.classList.remove("ready");
      if (indicatorText) indicatorText.textContent = Math.round((Number(status.progress) || 0) * 100) + "%";
      if (icon) icon.innerHTML = '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l2.5 2"/>';
    } else if (stateName === "ready") {
      indicator.classList.add("ready");
      indicator.classList.remove("downloading");
      if (indicatorText) indicatorText.textContent = "";
      if (icon) icon.innerHTML = '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 20h14"/>';
    } else {
      indicator.classList.remove("downloading", "ready");
      if (indicatorText) indicatorText.textContent = "";
      if (icon) icon.innerHTML = '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 20h14"/>';
    }
  }
  if (versionFact) {
    versionFact.classList.toggle("has-update", stateName === "update_available");
    versionFact.title = hasUpdate
      ? stateName === "ready" ? "更新包已准备好，点击重启更新" : stateName === "downloading" ? "正在下载更新" : "发现新版本 " + (latest?.tag_name || "") + "，点击查看更新"
      : "软件内更新";
  }
}

function updaterHeadline(status) {
  const stateName = status.state || "idle";
  if (stateName === "update_available") return "有新版本可用";
  if (stateName === "up_to_date") return "当前已是最新版本";
  if (stateName === "checking") return "正在获取更新信息";
  if (stateName === "downloading") return "正在下载更新";
  if (stateName === "ready") return "更新包已准备好";
  if (stateName === "installing") return "正在交接安装任务";
  if (["check_failed", "download_failed", "install_failed"].includes(stateName)) return "更新流程需要处理";
  return "检查官方发布";
}

function updaterSourceLabel(source) {
  if (source === "cnb") return "CNB 镜像";
  if (source === "github") return "GitHub";
  return "";
}

function updaterSourceText(status) {
  const repo = state.bootstrap?.updater_repo || "BITFSAE/can-host";
  const cnb = state.bootstrap?.updater_cnb_repo;
  const label = updaterSourceLabel(status?.source);
  if (label === "CNB 镜像") return cnb ? "CNB 镜像 · " + cnb : "CNB 镜像";
  if (label === "GitHub") return "GitHub · " + repo;
  return cnb ? "CNB 镜像优先（" + cnb + "），失败回退 GitHub" : repo;
}

function updaterBadgeText(status) {
  const stateName = status.state || "idle";
  if (stateName === "update_available") return "可更新";
  if (stateName === "up_to_date") return "已是最新";
  if (stateName === "checking") return "检查中";
  if (stateName === "downloading") return "下载中";
  if (stateName === "ready") return "待安装";
  if (stateName === "installing") return "安装中";
  if (["check_failed", "download_failed", "install_failed"].includes(stateName)) return "需处理";
  return "未检查";
}

function updaterBadgeClass(stateName) {
  if (stateName === "update_available" || stateName === "checking" || stateName === "downloading") return "active";
  if (stateName === "up_to_date" || stateName === "ready") return "ok";
  if (["check_failed", "download_failed", "install_failed"].includes(stateName)) return "bad";
  return "";
}

function updaterStatusText(status) {
  const stateName = status.state || "idle";
  const label = updaterSourceLabel(status.source);
  const suffix = label ? "（" + label + "）" : "";
  if (stateName === "checking") return "正在检查更新…";
  if (stateName === "update_available") return "发现新版本 " + (status.latest?.tag_name || "") + suffix;
  if (stateName === "up_to_date") return "当前已是最新版本" + suffix;
  if (stateName === "check_failed") return "检查更新失败";
  if (stateName === "downloading" && status.download_stage === "checksum") return "正在准备更新包…";
  if (stateName === "downloading" && status.download_stage === "verifying") return "下载完成，正在校验更新包…";
  if (stateName === "downloading") return "正在下载 " + (status.latest?.tag_name || status.downloaded_zip || "") + "…";
  if (stateName === "download_failed") return "下载更新失败";
  if (stateName === "ready") return "已下载并校验，可以重启更新";
  if (stateName === "installing") return "应用正在退出并安装更新…";
  if (stateName === "install_failed") return "无法启动安装助手";
  return "尚未检查更新";
}

function formatUpdaterSize(value) {
  const bytes = Number(value) || 0;
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KiB";
  if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + " MiB";
  return (bytes / (1024 * 1024 * 1024)).toFixed(1) + " GiB";
}

async function startUpdateIntent() {
  if (!state.api || updaterClosing || updaterDownloadStarted) return;
  if (state.updater?.install_supported === false) return;
  const stateName = state.updater?.state;
  if (stateName === "ready") {
    await installDownloadedUpdate();
    return;
  }
  if (stateName !== "update_available") return;
  updaterDownloadStarted = true;
  updaterRenderUpdaterStatus();
  try {
    const result = await state.api.download_update();
    if (!result.ok) {
      updaterDownloadStarted = false;
      toast(result.error || "无法开始下载", true);
      updaterRenderUpdaterStatus();
      return;
    }
    await pollUpdaterStatus();
  } catch (error) {
    updaterDownloadStarted = false;
    toast(error?.message || "无法开始下载", true);
    updaterRenderUpdaterStatus();
  }
}

async function installDownloadedUpdate() {
  if (!state.api?.install_update) return;
  if (updaterClosing) return;
  updaterClosing = true;
  try {
    const result = await state.api.install_update();
    if (result.ok) {
      text("#updaterMessage", "应用即将退出并安装新版本…");
      text("#updaterError", "请稍候；安装完成后新版本会自动启动。");
      $("#updaterError")?.classList.remove("hidden");
      $("#updaterInstall").disabled = true;
    } else {
      updaterClosing = false;
      text("#updaterError", result.error || "无法启动安装");
      $("#updaterError")?.classList.remove("hidden");
      updaterRenderUpdaterStatus();
    }
  } catch (error) {
    updaterClosing = false;
    text("#updaterError", error?.message || "无法启动安装");
    $("#updaterError")?.classList.remove("hidden");
    updaterRenderUpdaterStatus();
  }
}

if (!window.initUpdater) window.initUpdater = initUpdater;
document.addEventListener("DOMContentLoaded", () => initUpdater());
