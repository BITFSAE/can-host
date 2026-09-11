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
    python scripts/cnb_publish.py publish --repo totok22/can-host --tag v0.9.0 \
        --metadata-json release/github-release.json \
        --asset release/BITFSAE_CAN_Host_v0.9.0.zip \
        --asset release/BITFSAE_CAN_Host_v0.9.0.zip.sha256

    python scripts/cnb_publish.py verify --repo totok22/can-host
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

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
) -> dict[str, Any]:
    """不携带任何令牌，验证频道文件与发布附件在国内可匿名下载。"""
    url = manifest_raw_url(repo, branch, channel_path, base)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": ACCEPT_JSON})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        channel = json.loads(response.read().decode("utf-8"))
    results: list[dict[str, Any]] = []
    for release in channel.get("releases") or []:
        for asset in release.get("assets") or []:
            asset_url = _text(asset.get("url"))
            entry = {"tag": release.get("tag_name"), "name": asset.get("name"), "ok": False, "error": ""}
            try:
                head = urllib.request.Request(asset_url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(head, timeout=timeout) as response:
                    size = int(response.headers.get("Content-Length") or 0)
                    response.read(1)
                entry["ok"] = True
                entry["size"] = size
            except Exception as exc:  # pragma: no cover - 网络异常直接回报
                entry["error"] = str(exc)
            results.append(entry)
    return {"channel_url": url, "releases": len(channel.get("releases") or []), "assets": results}


def _read_token(env_name: str) -> str:
    token = (os.environ.get(env_name) or "").strip()
    if not token:
        raise CnbError(f"环境变量 {env_name} 中没有 CNB 访问令牌")
    return token


def _load_metadata(path: Path | None, tag: str, prerelease: bool) -> dict[str, Any]:
    if path is None:
        return {"tag_name": tag, "name": tag, "body": "", "published_at": "", "prerelease": prerelease, "draft": False}
    payload = json.loads(path.read_text(encoding="utf-8"))
    metadata = normalize_metadata(payload)
    metadata["prerelease"] = bool(metadata["prerelease"] or prerelease)
    return metadata


def cmd_publish(args: argparse.Namespace) -> int:
    token = _read_token(args.token_env)
    repo = args.repo
    metadata = _load_metadata(Path(args.metadata_json) if args.metadata_json else None, args.tag, args.prerelease)
    tag = metadata["tag_name"]
    assets = [Path(item) for item in args.asset]
    for asset in assets:
        if not asset.is_file():
            raise CnbError(f"待上传附件不存在：{asset}")

    client = CnbClient(repo, token, timeout=args.timeout, base=args.api_base)
    release = client.get_release_by_tag(tag)
    form = {
        "tag_name": tag,
        "name": metadata["name"],
        "body": metadata["body"],
        "prerelease": metadata["prerelease"],
        "make_latest": "false" if metadata["prerelease"] else "true",
    }
    if args.dry_run:
        print(f"[dry-run] 将镜像发布 {repo} {tag}（{len(assets)} 个附件）")
        for asset in assets:
            print(f"[dry-run]   {asset.name} ({asset.stat().st_size} 字节)")
        if not args.skip_channel:
            print(f"[dry-run]   刷新频道 {args.channel_branch}:{args.channel_path}")
        return 0

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

    if not args.skip_verify:
        report = verify_anonymous(repo, args.channel_branch, args.channel_path, args.web_base, args.timeout)
        bad = [item for item in report["assets"] if not item["ok"]]
        print(f"匿名校验：{report['channel_url']}，发布 {report['releases']} 个，附件 {len(report['assets'])} 个，失败 {len(bad)} 个")
        for item in bad:
            print(f"  下载失败 {item['tag']} {item['name']}：{item['error']}")
        if bad:
            raise CnbError("匿名下载校验失败，请检查 CNB 发布附件状态")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    report = verify_anonymous(args.repo, args.channel_branch, args.channel_path, args.web_base, args.timeout)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    failed = [item for item in report["assets"] if not item["ok"]]
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CNB 发布镜像与更新频道维护工具")
    sub = parser.add_subparsers(dest="command", required=True)

    publish = sub.add_parser("publish", help="镜像一次 GitHub 发布到 CNB 并刷新更新频道")
    publish.add_argument("--repo", required=True, help="CNB 仓库，格式 组织/仓库")
    publish.add_argument("--tag", required=True, help="发布标签，例如 v0.9.0")
    publish.add_argument("--asset", action="append", default=[], help="要上传的附件路径，可重复")
    publish.add_argument("--metadata-json", default="", help="GitHub 发布元数据 JSON（gh release view --json）")
    publish.add_argument("--prerelease", action="store_true", help="标记为预发布")
    publish.add_argument("--channel-branch", default=DEFAULT_CHANNEL_BRANCH)
    publish.add_argument("--channel-path", default=DEFAULT_CHANNEL_PATH)
    publish.add_argument("--channel-limit", type=int, default=DEFAULT_CHANNEL_LIMIT)
    publish.add_argument("--source", default=GITHUB_REPO, help="频道里记录的来源仓库")
    publish.add_argument("--token-env", default="CNB_TOKEN", help="保存访问令牌的环境变量名")
    publish.add_argument("--api-base", default=API_BASE)
    publish.add_argument("--web-base", default=WEB_BASE)
    publish.add_argument("--timeout", type=float, default=120.0)
    publish.add_argument("--skip-channel", action="store_true", help="只上传附件，不刷新频道")
    publish.add_argument("--skip-verify", action="store_true", help="跳过匿名下载校验")
    publish.add_argument("--dry-run", action="store_true")
    publish.set_defaults(func=cmd_publish)

    verify = sub.add_parser("verify", help="匿名校验更新频道与附件下载")
    verify.add_argument("--repo", required=True)
    verify.add_argument("--channel-branch", default=DEFAULT_CHANNEL_BRANCH)
    verify.add_argument("--channel-path", default=DEFAULT_CHANNEL_PATH)
    verify.add_argument("--web-base", default=WEB_BASE)
    verify.add_argument("--timeout", type=float, default=60.0)
    verify.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
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
