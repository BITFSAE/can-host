"""CNB 发布镜像工具（scripts/cnb_publish.py）的纯函数与上传流程测试。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import cnb_publish  # noqa: E402  （脚本目录按文件位置注入 sys.path）


def _release(tag: str, assets: list[dict], **extra) -> dict:
    payload = {"tag_name": tag, "name": tag, "body": "notes", "prerelease": False, "draft": False,
               "published_at": "2026-09-04T10:01:22Z", "assets": assets}
    payload.update(extra)
    return payload


class UrlBuilderTest(unittest.TestCase):
    def test_asset_download_url_uses_anonymous_main_domain(self) -> None:
        url = cnb_publish.asset_download_url("totok22/can-host", "v0.9.0", "BITFSAE_CAN_Host_v0.9.0.zip")
        self.assertEqual(
            url,
            "https://cnb.cool/totok22/can-host/-/releases/download/v0.9.0/BITFSAE_CAN_Host_v0.9.0.zip",
        )
        self.assertNotIn("api.cnb.cool", url)

    def test_asset_download_url_quotes_special_characters(self) -> None:
        url = cnb_publish.asset_download_url("o/r", "v1.0.0-rc1", "a b#1.zip")
        self.assertIn("/v1.0.0-rc1/", url)
        self.assertTrue(url.endswith("/a%20b%231.zip"))

    def test_manifest_raw_url_points_at_channel_branch(self) -> None:
        self.assertEqual(
            cnb_publish.manifest_raw_url("totok22/can-host"),
            "https://cnb.cool/totok22/can-host/-/git/raw/cnb-update/latest.json",
        )


class MetadataTest(unittest.TestCase):
    def test_normalizes_gh_camel_case_and_api_snake_case(self) -> None:
        camel = cnb_publish.normalize_metadata(
            {"tagName": "v0.9.0", "name": "v0.9.0", "body": "b", "publishedAt": "2026-09-04T10:01:22Z",
             "isPrerelease": True}
        )
        snake = cnb_publish.normalize_metadata(
            {"tag_name": "v0.9.0", "body": "b", "published_at": "2026-09-04T10:01:22Z", "prerelease": True}
        )
        self.assertEqual(camel["tag_name"], "v0.9.0")
        self.assertTrue(camel["prerelease"])
        self.assertEqual(snake["name"], "v0.9.0")
        self.assertTrue(snake["prerelease"])

    def test_missing_tag_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            cnb_publish.normalize_metadata({"name": "v0.9.0"})


class ChannelTest(unittest.TestCase):
    def test_channel_rewrites_assets_to_anonymous_urls(self) -> None:
        releases = [_release("v0.9.0", [
            {"id": "7", "name": "BITFSAE_CAN_Host_v0.9.0.zip", "size": 17188170,
             "url": "https://api.cnb.cool/totok22/can-host/-/releases/download/v0.9.0/x.zip"},
            {"id": "8", "name": "BITFSAE_CAN_Host_v0.9.0.zip.sha256", "size": 95, "url": ""},
        ])]
        channel = cnb_publish.build_channel(
            "totok22/can-host", releases, now=datetime(2026, 9, 12, tzinfo=timezone.utc)
        )
        self.assertEqual(channel["schema"], cnb_publish.CHANNEL_SCHEMA)
        self.assertEqual(channel["updated_at"], "2026-09-12T00:00:00Z")
        self.assertEqual(channel["source"], cnb_publish.GITHUB_REPO)
        entry = channel["releases"][0]
        self.assertEqual(entry["tag_name"], "v0.9.0")
        for asset in entry["assets"]:
            self.assertTrue(asset["url"].startswith("https://cnb.cool/totok22/can-host/-/releases/download/v0.9.0/"))
            self.assertNotIn("api.cnb.cool", asset["url"])
            self.assertEqual(asset["url"], asset["browser_download_url"])
        self.assertEqual(entry["assets"][0]["size"], 17188170)

    def test_channel_skips_drafts_and_honours_limit(self) -> None:
        releases = [
            _release("v0.9.0", []),
            _release("v0.8.9", [], draft=True),
            _release("v0.8.8", []),
            _release("v0.8.7", []),
        ]
        channel = cnb_publish.build_channel("totok22/can-host", releases, limit=2)
        self.assertEqual([item["tag_name"] for item in channel["releases"]], ["v0.9.0", "v0.8.8"])

    def test_channel_accepts_empty_release_list(self) -> None:
        channel = cnb_publish.build_channel("totok22/can-host", [])
        self.assertEqual(channel["releases"], [])


class UploadFlowTest(unittest.TestCase):
    def test_upload_asset_runs_presign_put_and_json_confirmation(self) -> None:
        calls: list[tuple] = []

        class FakeClient(cnb_publish.CnbClient):
            def _request(self, method, url, data=None, content_type=None,
                         accept=cnb_publish.ACCEPT_API, auth=True):
                calls.append((method, url, content_type, accept, auth))
                if url.endswith("asset-upload-url"):
                    return None, json.dumps({
                        "upload_url": "https://asset.cnb.cool/assets/t/ticket",
                        "verify_url": "https://api.cnb.cool/totok22/can-host/-/releases/1/"
                                      "asset-upload-confirmation/ticket/path?ttl=0",
                        "expires_in_sec": 43200,
                    }).encode("utf-8")
                return None, b"{}"

        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory) / "BITFSAE_CAN_Host_v0.9.0.zip.sha256"
            asset.write_text("deadbeef\n", encoding="utf-8")
            client = FakeClient("totok22/can-host", "token")
            client.upload_asset("2098433475644043264", asset)

        self.assertEqual([call[0] for call in calls], ["POST", "PUT", "POST"])
        presign, upload, confirm = calls
        self.assertTrue(presign[4])
        self.assertFalse(upload[4], "预签名地址不能带 CNB 令牌")
        self.assertEqual(upload[2], "application/octet-stream")
        self.assertEqual(confirm[2], "application/json")
        self.assertEqual(confirm[3], "application/json",
                         "确认接口必须接受 JSON，否则 CNB 返回 406")


class TokenTest(unittest.TestCase):
    def test_missing_token_env_is_reported(self) -> None:
        with self.assertRaises(cnb_publish.CnbError):
            cnb_publish._read_token("CNB_TOKEN_NOT_SET_FOR_TESTS")


if __name__ == "__main__":
    unittest.main()
