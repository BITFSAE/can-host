"""Tests for the frozen-app CA bundle helpers."""

from __future__ import annotations

import pathlib
import ssl
import unittest

from canhost.trust import ca_bundle_path, https_ssl_context, prefer_certifi


class TrustTest(unittest.TestCase):
    def test_certifi_bundle_exists_and_context_loads_it(self) -> None:
        self.assertTrue(ca_bundle_path(), "certifi 未安装或证书包缺失")
        context = https_ssl_context()
        self.assertIs(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.get_ca_certs(), "HTTPS 上下文未加载任何 CA 证书")

    def test_prefer_certifi_matches_default_cafile_state(self) -> None:
        cafile = getattr(ssl.get_default_verify_paths(), "cafile", "") or ""
        expected = not (cafile and pathlib.Path(cafile).is_file())
        self.assertEqual(prefer_certifi(), expected)


if __name__ == "__main__":
    unittest.main()
