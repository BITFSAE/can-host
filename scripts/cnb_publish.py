"""把上位机发布产物镜像到 CNB（cnb.cool），并维护国内可匿名读取的更新频道。

用途
----
- GitHub Actions 在创建 GitHub Release 之后调用 ``publish``：把同样的附件上传到
  CNB 发布（Release），然后刷新更新频道文件 ``latest.json``。
- 本地可用同一条命令补做镜像（幂等：同名附件覆盖、发布已存在则只更新说明）。

为什么需要更新频道
------------------
CNB 的 ``api.cnb.cool`` 一律需要访问令牌，而主域名 ``cnb.cool`` 上的
``/<repo>/-/releases/download/<tag>/<file>`` 与 ``/<repo>/-/git/raw/<ref>/<path>``
允许匿名访问。因此上位机只能在主域名上匿名取到"有哪些发布"，
频道文件就是这份匿名可读的发布清单；附件下载地址也一律写成主域名形式，
保证国内直连即可检查更新并下载，不需要代理、也不需要任何令牌。

约定
----
- 频道文件放在独立分支（默认 ``cnb-update``），不放进 main，
  避免与 GitHub main 的代码镜像相互覆盖。
- 只使用标准库；令牌只从环境变量读取（默认 ``CNB_TOKEN``），不出现在命令行里。
- 附件保留天数用 ``ttl=0``（永久），否则 CNB 默认期限到期后老版本会下不到。

命令行
------
    # CI 里附件已在本地（release.yml 刚打完包）：直接上传
    python scripts/cnb_publish.py publish --repo totok22/can-host --tag v0.9.0 \
        --metadata-json release/github-release.json \
        --asset release/BITFSAE_CAN_Host_v0.9.0.zip \
        --asset release/BITFSAE_CAN_Host_v0.9.0.zip.sha256

    # 从 GitHub 拉取并镜像（已是最新镜像时只打印一行就退出）
    python scripts/cnb_publish.py sync --repo totok22/can-host --tag v0.9.0
    python scripts/cnb_publish.py sync --repo totok22/can-host          # 最新正式发布

    # 不携带令牌，验证频道与附件在国内可匿名下载
    python scripts/cnb_publish.py verify --repo totok22/can-host
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

API_BASE = "https://api.cnb.cool"
WEB_BASE = "https://cnb.cool"
GITHUB_REPO = "BITFSAE/can-host"

ACCEPT_API = "application/vnd.cnb.api+json"
ACCEPT_JSON = "application/json"
USER_AGENT = "BITFSAE-CAN-Host-CNB-Mirror/1.0"

DEFAULT_CHANNEL_BRANCH = "cnb-update"
DEFAULT_CHANNEL_PATH = "latest.json"
DEFAULT_CHANNEL_LIMIT = 10
CHANNEL_SCHEMA = 1

# 附件永久保留；CNB 的 ttl 单位是天，0 表示永久（上限 180 天）。
ASSET_TTL_DAYS = 0

# 发布附件命名约定（与 build_windows.ps1 / release.yml 一致）。
# GitHub API 不可用（CNB 节点共享出口 IP 会被限流）时按它拼下载地址。
ASSET_NAME_PATTERNS = (
    "BITFSAE_CAN_Host_{tag}.zip",
    "BITFSAE_CAN_Host_{tag}.zip.sha256",
    "BITFSAE_CAN_Host_{tag}_setup.exe",
)


class CnbError(RuntimeError):
    """CNB 接口或 git 推送失败。"""


def asset_download_url(repo: str, tag: str, filename: str, base: str = WEB_BASE) -> str:
    """上位机匿名下载单个发布附件的地址（主域名，跟随 302 到临时地址）。"""
    return "{}/{}/-/releases/download/{}/{}".format(
        base.rstrip("/"),
        repo.strip("/"),
        urllib.parse.quote(tag, safe=""),
        urllib.parse.quote(filename, safe=""),
    )


def manifest_raw_url(
    repo: str,
    branch: str = DEFAULT_CHANNEL_BRANCH,
    path: str = DEFAULT_CHANNEL_PATH,
    base: str = WEB_BASE,
) -> str:
    """更新频道文件的匿名原始地址。"""
    return "{}/{}/-/git/raw/{}/{}".format(
        base.rstrip("/"),
        repo.strip("/"),
        urllib.parse.quote(branch, safe=""),
        urllib.parse.quote(path, safe=""),
    )


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def normalize_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """把 GitHub API / ``gh release view --json`` 的两种字段命名统一成内部结构。"""
    if not isinstance(payload, dict):
        raise ValueError("发布元数据不是 JSON 对象")
    tag = _text(payload.get("tag_name") or payload.get("tagName")).strip()
    if not tag:
        raise ValueError("发布元数据缺少 tag_name")
    prerelease = payload.get("prerelease")
    if prerelease is None:
        prerelease = payload.get("isPrerelease")
    return {
        "tag_name": tag,
        "name": _text(payload.get("name")).strip() or tag,
        "body": _text(payload.get("body")),
        "published_at": _text(payload.get("published_at") or payload.get("publishedAt")),
        "prerelease": bool(prerelease),
        "draft": bool(payload.get("draft") or payload.get("isDraft")),
    }


def build_channel(
    repo: str,
    releases: Iterable[dict[str, Any]],
    limit: int = DEFAULT_CHANNEL_LIMIT,
    base: str = WEB_BASE,
    source: str = GITHUB_REPO,
    now: datetime | None = None,
) -> dict[str, Any]:
    """由 CNB 发布列表生成更新频道内容；附件地址改写成主域名匿名下载地址。"""
    entries: list[dict[str, Any]] = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        tag = _text(release.get("tag_name")).strip()
        if not tag:
            continue
        assets: list[dict[str, Any]] = []
        for asset in release.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            name = _text(asset.get("name")).strip()
            if not name:
                continue
            url = asset_download_url(repo, tag, name, base)
            assets.append(
                {
                    "id": _text(asset.get("id")),
                    "name": name,
                    "size": int(asset.get("size") or 0),
                    "url": url,
                    "browser_download_url": url,
                    "content_type": _text(asset.get("content_type")),
                }
            )
        entries.append(
            {
                "tag_name": tag,
                "name": _text(release.get("name")).strip() or tag,
                "body": _text(release.get("body")),
                "published_at": _text(release.get("published_at") or release.get("created_at")),
                "prerelease": bool(release.get("prerelease")),
                "draft": False,
                "html_url": f"{base.rstrip('/')}/{repo.strip('/')}/-/releases/tag/{urllib.parse.quote(tag, safe='')}",
                "assets": assets,
            }
        )
        if len(entries) >= max(1, int(limit)):
            break
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "schema": CHANNEL_SCHEMA,
        "repo": repo,
        "source": source,
        "updated_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "releases": entries,
    }


class CnbClient:
    """CNB OpenAPI 的最小客户端（发布与附件上传）。"""

    def __init__(self, repo: str, token: str, timeout: float = 120.0, base: str = API_BASE) -> None:
        self.repo = repo.strip("/")
        self.token = token
        self.timeout = timeout
        self.base = base.rstrip("/")

    def _url(self, path: str) -> str:
        return f"{self.base}/{self.repo}/-/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        url: str,
        data: bytes | None = None,
        content_type: str | None = None,
        accept: str = ACCEPT_API,
        auth: bool = True,
    ) -> Any:
        headers = {"User-Agent": USER_AGENT, "Accept": accept}
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                return response, body
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace").strip()
            except Exception:  # pragma: no cover - 读取错误体失败不影响报错信息
                detail = ""
            raise CnbError(f"CNB 请求失败 {method} {url} -> HTTP {exc.code} {detail[:400]}") from exc
        except urllib.error.URLError as exc:
            raise CnbError(f"CNB 网络不可达 {method} {url}: {exc.reason}") from exc

    @staticmethod
    def _decode(body: bytes, context: str, required: bool = True) -> Any:
        """解析响应体；CNB 的更新接口可能返回空体，因此只有必需时才报错。"""
        text = body.decode("utf-8", "replace").strip()
        if not text:
            if required:
                raise CnbError(f"CNB {context} 返回空响应")
            return {}
        try:
            return json.loads(text)
        except ValueError as exc:
            raise CnbError(f"CNB {context} 返回的不是 JSON：{text[:200]!r}") from exc

    def get_release_by_tag(self, tag: str) -> dict[str, Any] | None:
        url = self._url(f"releases/tags/{urllib.parse.quote(tag, safe='')}")
        try:
            _, body = self._request("GET", url)
        except CnbError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        payload = self._decode(body, f"发布查询 {tag}")
        return payload if isinstance(payload, dict) else None

    def create_release(self, form: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(form, ensure_ascii=False).encode("utf-8")
        _, body = self._request("POST", self._url("releases"), data, ACCEPT_JSON)
        payload = self._decode(body, "创建发布")
        if not isinstance(payload, dict) or not payload.get("id"):
            raise CnbError(f"CNB 创建发布返回格式不正确：{body[:300]!r}")
        return payload

    def patch_release(self, release_id: str, form: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(form, ensure_ascii=False).encode("utf-8")
        _, body = self._request("PATCH", self._url(f"releases/{release_id}"), data, ACCEPT_JSON)
        payload = self._decode(body, "更新发布", required=False)
        return payload if isinstance(payload, dict) else {}

    def list_releases(self, page_size: int = DEFAULT_CHANNEL_LIMIT) -> list[dict[str, Any]]:
        url = self._url(f"releases?page=1&page_size={max(1, int(page_size))}")
        _, body = self._request("GET", url)
        payload = self._decode(body, "发布列表")
        if not isinstance(payload, list):
            raise CnbError(f"CNB 发布列表返回格式不正确：{body[:300]!r}")
        return [item for item in payload if isinstance(item, dict)]

    def upload_asset(self, release_id: str, path: Path) -> None:
        """预签名地址上传 + 确认；同名附件覆盖，永久保留。"""
        size = path.stat().st_size
        form = json.dumps(
            {"asset_name": path.name, "size": size, "overwrite": True, "ttl": ASSET_TTL_DAYS},
            ensure_ascii=False,
        ).encode("utf-8")
        _, body = self._request(
            "POST", self._url(f"releases/{release_id}/asset-upload-url"), form, ACCEPT_JSON
        )
        ticket = self._decode(body, "附件上传地址")
        upload_url = _text(ticket.get("upload_url"))
        verify_url = _text(ticket.get("verify_url"))
        if not upload_url or not verify_url:
            raise CnbError(f"CNB 未返回附件上传地址：{body[:300]!r}")
        payload = path.read_bytes()
        self._request("PUT", upload_url, payload, "application/octet-stream", accept=ACCEPT_JSON, auth=False)
        # 确认接口必须带上 Accept: application/json，否则 CNB 返回 406。
        self._request("POST", verify_url, b"{}", ACCEPT_JSON, accept=ACCEPT_JSON)


def _run_git(args: list[str], cwd: Path, env: dict[str, str], secret: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        message = f"{result.stdout}\n{result.stderr}".replace(secret, "***")
        raise CnbError(f"git {' '.join(args)} 失败：{message.strip()[:600]}")
    return result.stdout


def push_channel(
    repo: str,
    token: str,
    branch: str,
    path: str,
    content: dict[str, Any],
    message: str,
) -> str:
    """把频道文件提交到独立分支；分支不存在时创建。返回提交摘要。"""
    if shutil.which("git") is None:
        raise CnbError("系统中找不到 git，无法写入更新频道分支")
    remote = f"https://cnb:{urllib.parse.quote(token, safe='')}@cnb.cool/{repo.strip('/')}"
    payload = json.dumps(content, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    env = dict(os.environ)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "cnb-mirror",
            "GIT_AUTHOR_EMAIL": "cnb-mirror@users.noreply.github.com",
            "GIT_COMMITTER_NAME": "cnb-mirror",
            "GIT_COMMITTER_EMAIL": "cnb-mirror@users.noreply.github.com",
        }
    )
    target = Path(path)
    with tempfile.TemporaryDirectory(prefix="cnb-channel-") as directory:
        work = Path(directory)
        _run_git(["init", "-q", "-b", branch], work, env, token)
        _run_git(["remote", "add", "origin", remote], work, env, token)
        fetched = True
        try:
            _run_git(["fetch", "-q", "--depth", "1", "origin", branch], work, env, token)
        except CnbError:
            fetched = False
        if fetched:
            _run_git(["checkout", "-q", "FETCH_HEAD"], work, env, token)
        target.parent.mkdir(parents=True, exist_ok=True)
        (work / target).write_text(payload, encoding="utf-8", newline="\n")
        _run_git(["add", "--", str(target)], work, env, token)
        status = _run_git(["status", "--porcelain"], work, env, token).strip()
        if not status:
            return "频道文件无变化，跳过提交"
        _run_git(["commit", "-q", "-m", message], work, env, token)
        _run_git(["push", "-q", "origin", f"HEAD:refs/heads/{branch}"], work, env, token)
        return _run_git(["rev-parse", "--short", "HEAD"], work, env, token).strip()


def verify_anonymous(
    repo: str,
    branch: str,
    channel_path: str,
    base: str = WEB_BASE,
    timeout: float = 60.0,
    attempts: int = 3,
) -> dict[str, Any]:
    """不携带任何令牌，验证频道文件与发布附件在国内可匿名下载。

    下载校验带重试：国内到 CDN 偶发 TLS/连接抖动，不该让自动流水线误报失败。
    """
    url = manifest_raw_url(repo, branch, channel_path, base)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": ACCEPT_JSON})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        channel = json.loads(response.read().decode("utf-8"))
    results: list[dict[str, Any]] = []
    for release in channel.get("releases") or []:
        for asset in release.get("assets") or []:
            asset_url = _text(asset.get("url"))
            entry = {"tag": release.get("tag_name"), "name": asset.get("name"), "ok": False, "error": ""}
            for attempt in range(max(1, attempts)):
                try:
                    head = urllib.request.Request(asset_url, headers={"User-Agent": USER_AGENT})
                    with urllib.request.urlopen(head, timeout=timeout) as response:
                        size = int(response.headers.get("Content-Length") or 0)
                        response.read(1)
                    entry["ok"] = True
                    entry["size"] = size
                    entry["error"] = ""
                    break
                except Exception as exc:  # pragma: no cover - 网络异常直接回报
                    entry["error"] = str(exc)
                    if attempt + 1 < max(1, attempts):
                        time.sleep(1.0 + attempt)
            results.append(entry)
    return {"channel_url": url, "releases": len(channel.get("releases") or []), "assets": results}


def _read_token(env_name: str) -> str:
    token = (os.environ.get(env_name) or "").strip()
    if not token:
        raise CnbError(f"环境变量 {env_name} 中没有 CNB 访问令牌")
    return token


def github_json(url: str, token: str | None = None, timeout: float = 60.0) -> Any:
    """读取 GitHub API（公开仓库无需令牌；有令牌时用于提高限额）。"""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace").strip()
        except Exception:  # pragma: no cover
            detail = ""
        raise CnbError(f"GitHub 请求失败 {url} -> HTTP {exc.code} {detail[:200]}") from exc
    except urllib.error.URLError as exc:
        raise CnbError(f"GitHub 网络不可达 {url}: {exc.reason}") from exc


def github_release(
    repo: str,
    tag: str | None = None,
    include_prerelease: bool = False,
    token: str | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """取指定标签的 GitHub 发布；不给标签时取最新（默认跳过预发布）。"""
    base = f"https://api.github.com/repos/{repo.strip('/')}"
    if tag:
        payload = github_json(f"{base}/releases/tags/{urllib.parse.quote(tag, safe='')}", token, timeout)
        if not isinstance(payload, dict):
            raise CnbError(f"GitHub 发布 {tag} 返回格式不正确")
        return payload
    releases = github_json(f"{base}/releases?per_page=20", token, timeout)
    if not isinstance(releases, list):
        raise CnbError("GitHub 发布列表返回格式不正确")
    for candidate in releases:
        if not isinstance(candidate, dict) or candidate.get("draft"):
            continue
        if candidate.get("prerelease") and not include_prerelease:
            continue
        return candidate
    raise CnbError(f"GitHub 仓库 {repo} 上没有可用的正式发布")


def github_assets(release: dict[str, Any]) -> list[dict[str, Any]]:
    """GitHub 发布附件（名称、大小和匿名下载地址）。"""
    result: list[dict[str, Any]] = []
    for asset in release.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        name = _text(asset.get("name")).strip()
        url = _text(asset.get("browser_download_url")).strip()
        if not name or not url:
            continue
        result.append({"name": name, "size": int(asset.get("size") or 0), "url": url})
    return result


def mirror_is_current(cnb_release: dict[str, Any] | None, wanted_assets: Sequence[dict[str, Any]]) -> bool:
    """CNB 发布是否已包含待镜像的全部附件（名称与大小都一致）。"""
    if not cnb_release:
        return False
    have = {
        _text(asset.get("name")): int(asset.get("size") or 0)
        for asset in cnb_release.get("assets") or []
        if isinstance(asset, dict)
    }
    want = [(_text(asset.get("name")), int(asset.get("size") or 0)) for asset in wanted_assets]
    if not want:
        return False
    return all(have.get(name) == size for name, size in want)


def download_file(url: str, target: Path, token: str | None = None, timeout: float = 600.0) -> None:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/octet-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    with urllib.request.urlopen(request, timeout=timeout) as response, partial.open("wb") as handle:
        while True:
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    partial.replace(target)


def _tag_sort_key(tag: str) -> tuple:
    """给 vX.Y.Z 标签排序；带后缀的预发布排在对应正式版之后。"""
    text = tag[1:] if tag.startswith(("v", "V")) else tag
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-(.+))?", text)
    if not match:
        return (0, 0, 0, 1, text)
    major, minor, patch = (int(match.group(index)) for index in (1, 2, 3))
    suffix = match.group(4)
    return (major, minor, patch, 0 if suffix is None else 1, suffix or "")


def latest_release_tag(repo: str, timeout: float = 60.0) -> str:
    """用 git ls-remote 取最新正式标签（不经过 GitHub API，避免共享出口 IP 限流）。"""
    if shutil.which("git") is None:
        raise CnbError("系统中找不到 git，无法按标签探测最新发布")
    result = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", f"https://github.com/{repo.strip('/')}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    if result.returncode != 0:
        raise CnbError(f"git ls-remote 失败：{(result.stderr or result.stdout).strip()[:300]}")
    tags = [
        line.split("\t", 1)[1].removeprefix("refs/tags/")
        for line in result.stdout.splitlines()
        if "\trefs/tags/" in line
    ]
    formal = [tag for tag in tags if re.fullmatch(r"v?\d+\.\d+\.\d+", tag)]
    if not formal:
        raise CnbError(f"GitHub 仓库 {repo} 上没有形如 vX.Y.Z 的标签")
    return max(formal, key=_tag_sort_key)


def probe_remote_size(url: str, timeout: float = 60.0) -> int:
    """HEAD 探测发布附件大小；附件不存在返回 0。"""
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.headers.get("Content-Length") or 0)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return 0
        raise CnbError(f"探测发布附件失败 {url} -> HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise CnbError(f"探测发布附件网络不可达 {url}: {exc.reason}") from exc


def fallback_release(
    repo: str,
    tag: str,
    asset_patterns: Sequence[str] = ASSET_NAME_PATTERNS,
    timeout: float = 60.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """GitHub API 不可用时按仓库约定的附件名探测发布。

    限流不该让自动补镜像失败：标签用 `git ls-remote` 取，附件名按
    `BITFSAE_CAN_Host_<标签>.*` 约定拼出，大小用 HEAD 探测。
    代价是拿不到 Release 说明文字，更新窗口会显示“此次 Release 未填写说明”。
    """
    if not tag:
        tag = latest_release_tag(repo, timeout)
    assets: list[dict[str, Any]] = []
    for pattern in asset_patterns:
        name = pattern.format(tag=tag)
        url = "https://github.com/{}/releases/download/{}/{}".format(
            repo.strip("/"),
            urllib.parse.quote(tag, safe=""),
            urllib.parse.quote(name, safe=""),
        )
        size = probe_remote_size(url, timeout)
        if size <= 0:
            raise CnbError(f"GitHub 上找不到发布附件 {name}，请确认标签 {tag} 是否为正式发布")
        assets.append({"name": name, "size": size, "url": url})
    metadata = {
        "tag_name": tag,
        "name": tag,
        "body": "",
        "published_at": "",
        "prerelease": "-" in tag,
        "draft": False,
    }
    return metadata, assets


def _load_metadata(path: Path | None, tag: str, prerelease: bool) -> dict[str, Any]:
    if path is None:
        return {"tag_name": tag, "name": tag, "body": "", "published_at": "", "prerelease": prerelease, "draft": False}
    payload = json.loads(path.read_text(encoding="utf-8"))
    metadata = normalize_metadata(payload)
    metadata["prerelease"] = bool(metadata["prerelease"] or prerelease)
    return metadata


def _refresh_channel(
    client: "CnbClient", repo: str, token: str, args: argparse.Namespace, tag: str
) -> None:
    releases = client.list_releases(args.channel_limit)
    channel = build_channel(repo, releases, args.channel_limit, args.web_base, args.source)
    digest = push_channel(
        repo,
        token,
        args.channel_branch,
        args.channel_path,
        channel,
        f"chore(cnb): 刷新更新频道（{tag}）",
    )
    print(f"更新频道 {args.channel_branch}:{args.channel_path} -> {digest}")


def _verify_channel(repo: str, args: argparse.Namespace) -> None:
    report = verify_anonymous(repo, args.channel_branch, args.channel_path, args.web_base, args.timeout)
    bad = [item for item in report["assets"] if not item["ok"]]
    print(f"匿名校验：{report['channel_url']}，发布 {report['releases']} 个，附件 {len(report['assets'])} 个，失败 {len(bad)} 个")
    for item in bad:
        print(f"  下载失败 {item['tag']} {item['name']}：{item['error']}")
    if bad:
        raise CnbError("匿名下载校验失败，请检查 CNB 发布附件状态")


def _publish_release(
    client: "CnbClient",
    repo: str,
    token: str,
    metadata: dict[str, Any],
    assets: list[Path],
    args: argparse.Namespace,
) -> int:
    """把已经准备好的附件发布到 CNB，并刷新与校验更新频道。"""
    tag = _text(metadata["tag_name"])
    form = {
        "tag_name": tag,
        "name": metadata["name"],
        "body": metadata["body"],
        "prerelease": bool(metadata["prerelease"]),
        "make_latest": "false" if metadata["prerelease"] else "true",
    }
    release = client.get_release_by_tag(tag)
    if release is None:
        release = client.create_release(form)
        print(f"创建 CNB 发布 {tag}（id={release.get('id')}）")
    else:
        release_id = _text(release.get("id"))
        client.patch_release(release_id, {key: form[key] for key in ("name", "body", "prerelease", "make_latest")})
        print(f"更新已有 CNB 发布 {tag}（id={release_id}）")

    release_id = _text(release.get("id"))
    for asset in assets:
        client.upload_asset(release_id, asset)
        print(f"上传附件 {asset.name}（{asset.stat().st_size} 字节）")

    refreshed = client.get_release_by_tag(tag) or {}
    uploaded = {_text(item.get("name")) for item in refreshed.get("assets") or []}
    missing = [asset.name for asset in assets if asset.name not in uploaded]
    if missing:
        raise CnbError(f"CNB 发布 {tag} 缺少附件：{', '.join(missing)}")

    if not args.skip_channel:
        _refresh_channel(client, repo, token, args, tag)
    if not args.skip_verify:
        _verify_channel(repo, args)
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    token = _read_token(args.token_env)
    repo = args.repo
    metadata = _load_metadata(Path(args.metadata_json) if args.metadata_json else None, args.tag, args.prerelease)
    assets = [Path(item) for item in args.asset]
    for asset in assets:
        if not asset.is_file():
            raise CnbError(f"待上传附件不存在：{asset}")

    if args.dry_run:
        print(f"[dry-run] 将镜像发布 {repo} {metadata['tag_name']}（{len(assets)} 个附件）")
        for asset in assets:
            print(f"[dry-run]   {asset.name} ({asset.stat().st_size} 字节)")
        if not args.skip_channel:
            print(f"[dry-run]   刷新频道 {args.channel_branch}:{args.channel_path}")
        return 0

    client = CnbClient(repo, token, timeout=args.timeout, base=args.api_base)
    return _publish_release(client, repo, token, metadata, assets, args)


def cmd_sync(args: argparse.Namespace) -> int:
    """从 GitHub 拉取发布产物并镜像到 CNB；已是最新镜像时直接跳过。"""
    token = _read_token(args.token_env)
    repo = args.repo
    github_token = (os.environ.get(args.github_token_env) or "").strip() or None
    try:
        payload = github_release(args.github_repo, args.tag or None, args.include_prerelease,
                                 github_token, args.timeout)
        metadata = normalize_metadata(payload)
        remote_assets = github_assets(payload)
        if not remote_assets:
            raise CnbError(f"GitHub 发布 {metadata['tag_name']} 没有附件")
    except CnbError as exc:
        # CNB 构建节点共享出口 IP，未认证的 GitHub API 经常被限流；
        # 这种情况下按仓库约定探测，保证自动补镜像仍然能跑完。
        print(f"GitHub API 不可用：{exc}")
        print("改用标签与附件名约定探测…")
        metadata, remote_assets = fallback_release(
            args.github_repo, args.tag, args.asset_name or ASSET_NAME_PATTERNS, args.timeout
        )
        print(f"按约定探测到发布 {metadata['tag_name']}（{len(remote_assets)} 个附件）")
    tag = metadata["tag_name"]

    client = CnbClient(repo, token, timeout=args.timeout, base=args.api_base)
    current = client.get_release_by_tag(tag)
    if mirror_is_current(current, remote_assets) and not args.force:
        print(f"CNB 已是最新镜像：{tag}（{len(remote_assets)} 个附件），跳过下载与上传")
        if not args.skip_channel:
            _refresh_channel(client, repo, token, args, tag)
        if not args.skip_verify:
            _verify_channel(repo, args)
        return 0

    work = Path(args.download_dir) if args.download_dir else Path(tempfile.mkdtemp(prefix="cnb-sync-"))
    work.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for asset in remote_assets:
        target = work / asset["name"]
        if target.is_file() and target.stat().st_size == asset["size"]:
            print(f"复用已下载附件 {target.name}")
        else:
            print(f"下载 {asset['name']}（{asset['size']} 字节）")
            download_file(asset["url"], target, github_token, args.timeout)
        if target.stat().st_size != asset["size"]:
            raise CnbError(f"{target.name} 下载不完整：期望 {asset['size']} 字节，实际 {target.stat().st_size} 字节")
        paths.append(target)

    if args.dry_run:
        print(f"[dry-run] 将镜像发布 {repo} {tag}（{len(paths)} 个附件）")
        return 0
    return _publish_release(client, repo, token, metadata, paths, args)



def cmd_verify(args: argparse.Namespace) -> int:
    report = verify_anonymous(args.repo, args.channel_branch, args.channel_path, args.web_base, args.timeout)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    failed = [item for item in report["assets"] if not item["ok"]]
    return 1 if failed else 0


def _add_publish_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True, help="CNB 仓库，格式 组织/仓库")
    parser.add_argument("--prerelease", action="store_true", help="标记为预发布")
    parser.add_argument("--channel-branch", default=DEFAULT_CHANNEL_BRANCH)
    parser.add_argument("--channel-path", default=DEFAULT_CHANNEL_PATH)
    parser.add_argument("--channel-limit", type=int, default=DEFAULT_CHANNEL_LIMIT)
    parser.add_argument("--source", default=GITHUB_REPO, help="频道里记录的来源仓库")
    parser.add_argument("--token-env", default="CNB_TOKEN", help="保存 CNB 访问令牌的环境变量名")
    parser.add_argument("--api-base", default=API_BASE)
    parser.add_argument("--web-base", default=WEB_BASE)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--skip-channel", action="store_true", help="只上传附件，不刷新频道")
    parser.add_argument("--skip-verify", action="store_true", help="跳过匿名下载校验")
    parser.add_argument("--dry-run", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CNB 发布镜像与更新频道维护工具")
    sub = parser.add_subparsers(dest="command", required=True)

    publish = sub.add_parser("publish", help="把本地已有的发布附件上传到 CNB 并刷新更新频道")
    _add_publish_options(publish)
    publish.add_argument("--tag", required=True, help="发布标签，例如 v0.9.0")
    publish.add_argument("--asset", action="append", default=[], help="要上传的附件路径，可重复")
    publish.add_argument("--metadata-json", default="", help="GitHub 发布元数据 JSON（gh release view --json）")
    publish.set_defaults(func=cmd_publish)

    sync = sub.add_parser("sync", help="从 GitHub 拉取发布产物并镜像到 CNB（已是最新则跳过）")
    _add_publish_options(sync)
    sync.add_argument("--github-repo", default=GITHUB_REPO, help="来源 GitHub 仓库")
    sync.add_argument("--tag", default="", help="指定发布标签；留空取最新正式发布")
    sync.add_argument("--include-prerelease", action="store_true", help="取最新时允许预发布")
    sync.add_argument("--download-dir", default="", help="附件下载目录；留空用临时目录")
    sync.add_argument("--asset-name", action="append", default=[], help="附件名约定（{tag} 会被替换），可重复")
    sync.add_argument("--github-token-env", default="GITHUB_TOKEN", help="可选的 GitHub 令牌环境变量名")
    sync.add_argument("--force", action="store_true", help="即使已是最新镜像也重新上传")
    sync.set_defaults(func=cmd_sync)

    verify = sub.add_parser("verify", help="匿名校验更新频道与附件下载")
    verify.add_argument("--repo", required=True)
    verify.add_argument("--channel-branch", default=DEFAULT_CHANNEL_BRANCH)
    verify.add_argument("--channel-path", default=DEFAULT_CHANNEL_PATH)
    verify.add_argument("--web-base", default=WEB_BASE)
    verify.add_argument("--timeout", type=float, default=60.0)
    verify.set_defaults(func=cmd_verify)
    return parser


def _configure_stdout() -> None:
    """Windows 控制台/CI 的默认编码可能装不下中文，改成 UTF-8 并替换无法编码的字符。"""
    stream = getattr(sys, "stdout", None)
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover - 环境不支持时保持原样
        pass


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except CnbError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 顶层统一兜底，CI 需要非零退出码
        print(f"错误：{exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
