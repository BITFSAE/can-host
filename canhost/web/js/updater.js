/* 软件内更新：检查 GitHub Release、下载校验、保存私有仓库令牌、退出安装。
 * 只使用零构建原生脚本；后端负责完整令牌与文件替换，前端不接触安装目录。 */

var updaterPollTimer = null;
var updaterAutoStarted = false;
var updaterClosing = false;

function initUpdater() {
  if (!state.api || !state.bootstrap) return;
  bindUpdaterControls();
  if (updaterPollTimer) return;
  updaterPollTimer = setInterval(pollUpdaterStatus, 1000);
  if (state.bootstrap.updater_enabled && state.bootstrap.updater_repo && !updaterAutoStarted) {
    updaterAutoStarted = true;
    refreshUpdater(true);
  }
}

function bindUpdaterControls() {
  const versionFact = $("#appVersionFact");
  if (versionFact && !versionFact.dataset.bound) {
    versionFact.dataset.bound = "1";
    versionFact.addEventListener("click", openUpdaterDialog);
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
  const download = $("#updaterDownload");
  if (download && !download.dataset.bound) {
    download.dataset.bound = "1";
    download.addEventListener("click", async () => {
      const result = await state.api.download_update();
      if (!result.ok) {
        toast(result.error || "无法开始下载", true);
        updaterRenderUpdaterStatus();
      } else {
        await pollUpdaterStatus();
      }
    });
  }
  const install = $("#updaterInstall");
  if (install && !install.dataset.bound) {
    install.dataset.bound = "1";
    install.addEventListener("click", () => installDownloadedUpdate());
  }
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

async function openUpdaterDialog() {
  const dialog = $("#updaterDialog");
  if (!dialog) return;
  if (!dialog.open) dialog.showModal();
  updaterRenderUpdaterStatus();
  await pollUpdaterStatus();
  // Source runs are allowed to check but not install. Opening the entry point
  // should still provide a useful result when startup auto-check is disabled.
  if (updaterStatusIsIdle()) await refreshUpdater(false);
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
    notes.textContent = latest?.body
      ? String(latest.body).trim().slice(0, 600)
      : latest ? "此次 Release 未填写说明。" : "检查完成后会在这里显示版本说明。";
  }

  const size = latest?.assets?.find(item => String(item.name).toLowerCase().endsWith(".zip"))?.size;
  const sizeNode = $("#updaterSize");
  if (sizeNode) sizeNode.textContent = size ? formatUpdaterSize(size) : "—";

  const progress = $("#updaterProgress");
  if (progress) {
    const pct = Math.round((Number(status.progress) || 0) * 100);
    progress.style.width = pct + "%";
    progress.parentNode?.classList.toggle("hidden", stateName !== "downloading");
  }
  const progressText = $("#updaterProgressText");
  if (progressText) progressText.textContent = stateName === "downloading" ? "完成 " + Math.round((Number(status.progress) || 0) * 100) + "%" : "";

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
    fileNode.textContent = status.stage_dir ? "下载与校验已完成。点击“重启并安装”后，应用会自动替换并启动新版本。" : "";
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
  const downloadBtn = $("#updaterDownload");
  if (downloadBtn) downloadBtn.disabled = stateName !== "update_available" || !latest?.assets?.length;
  const installBtn = $("#updaterInstall");
  if (installBtn) {
    installBtn.disabled = stateName !== "ready" || !installSupported || updaterClosing;
    installBtn.textContent = stateName === "installing" ? "正在退出…" : "重启并安装";
  }
  const prerelease = $("#updaterPrerelease");
  if (prerelease) prerelease.disabled = ["checking", "downloading", "installing"].includes(stateName);
  const autoNote = $("#updaterAutoNote");
  if (autoNote) autoNote.textContent = installSupported
    ? "公开仓库默认方案：启动时自动检查一次正式版；发现更新不会自动下载。"
    : "当前为源码运行，只能检查 GitHub Release，不能替换安装目录。";
  const repoNode = $("#updaterRepo");
  if (repoNode) repoNode.textContent = state.bootstrap?.updater_repo || "BITFSAE/can-host";
  const pathNode = $("#updaterSettingsPath");
  if (pathNode) pathNode.textContent = state.bootstrap?.updater_settings_path || "—";

  const indicator = $("#appUpdateIndicator");
  const versionFact = $("#appVersionFact");
  const hasUpdate = stateName === "update_available";
  if (indicator) indicator.classList.toggle("hidden", !hasUpdate);
  if (versionFact) {
    versionFact.classList.toggle("has-update", hasUpdate);
    versionFact.title = hasUpdate
      ? "发现新版本 " + (latest?.tag_name || "") + "，点击查看更新"
      : "软件内更新";
  }
}

function updaterHeadline(status) {
  const stateName = status.state || "idle";
  if (stateName === "update_available") return "有新版本可用";
  if (stateName === "up_to_date") return "当前已是最新版本";
  if (["checking", "downloading"].includes(stateName)) return "正在获取更新信息";
  if (stateName === "ready") return "更新包已准备好";
  if (stateName === "installing") return "正在交接安装任务";
  if (["check_failed", "download_failed", "install_failed"].includes(stateName)) return "更新流程需要处理";
  return "检查官方 Release";
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
  if (stateName === "checking") return "正在检查 GitHub Release…";
  if (stateName === "update_available") return "发现新版本 " + (status.latest?.tag_name || "");
  if (stateName === "up_to_date") return "当前已是最新版本";
  if (stateName === "check_failed") return "检查更新失败";
  if (stateName === "downloading") return "正在下载 " + (status.latest?.tag_name || status.downloaded_zip || "") + "…";
  if (stateName === "download_failed") return "下载更新失败";
  if (stateName === "ready") return "已下载并校验，可以重启安装";
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
