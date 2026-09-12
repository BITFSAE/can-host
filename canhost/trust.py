"""Trusted CA bundle for HTTPS requests in the frozen macOS app.

Windows 的 python.org/PyInstaller 运行环境使用系统证书库，TLS 校验本来就能
通过；macOS 冻结包没有系统 CA 列表，缺证书时会报
``SSL: CERTIFICATE_VERIFY_FAILED``。这里保留解释器默认信任库，并始终追加随包
安装的 certifi，避免存在但不完整或过期的系统 CA 文件使随包证书失效。
"""
from __future__ import annotations

import ssl
from pathlib import Path

import certifi

__all__ = ["ca_bundle_path", "certifi_roots_loaded", "https_ssl_context"]


def ca_bundle_path() -> str:
    """Path to the CA bundle HTTPS clients should trust ("" when missing)."""
    bundle = Path(certifi.where())
    return str(bundle) if bundle.is_file() else ""


def https_ssl_context() -> ssl.SSLContext:
    """Default HTTPS context with the bundled certifi roots added."""
    context = ssl.create_default_context()
    cafile = ca_bundle_path()
    if cafile:
        context.load_verify_locations(cafile=cafile)
    return context


def certifi_roots_loaded(context: ssl.SSLContext) -> bool:
    """Return whether *context* contains every root from the bundled CA file."""
    cafile = ca_bundle_path()
    if not cafile:
        return False
    bundle_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    bundle_context.load_verify_locations(cafile=cafile)
    bundle_roots = set(bundle_context.get_ca_certs(binary_form=True))
    loaded_roots = set(context.get_ca_certs(binary_form=True))
    return bool(bundle_roots) and bundle_roots.issubset(loaded_roots)
