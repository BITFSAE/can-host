"""Tests for the frozen-app CA bundle helpers."""

from __future__ import annotations

import ssl
import unittest
from unittest.mock import patch

from canhost.trust import ca_bundle_path, certifi_roots_loaded, https_ssl_context


class TrustTest(unittest.TestCase):
    def test_certifi_bundle_exists_and_context_loads_it(self) -> None:
        self.assertTrue(ca_bundle_path(), "certifi 未安装或证书包缺失")
        context = https_ssl_context()
        self.assertIs(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.get_ca_certs(), "HTTPS 上下文未加载任何 CA 证书")
        self.assertTrue(certifi_roots_loaded(context), "HTTPS 上下文未完整加载 certifi")

    def test_context_always_adds_certifi_to_default_roots(self) -> None:
        with patch("canhost.trust.ssl.create_default_context") as create_context:
            context = create_context.return_value
            self.assertIs(https_ssl_context(), context)
        context.load_verify_locations.assert_called_once_with(cafile=ca_bundle_path())

    def test_certifi_root_check_rejects_an_empty_context(self) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.assertFalse(certifi_roots_loaded(context))


if __name__ == "__main__":
    unittest.main()
