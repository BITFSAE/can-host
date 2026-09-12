"""Trusted CA bundle for HTTPS requests in the frozen macOS app.

Windows 的 python.org/PyInstaller 运行环境使用系统证书库，TLS 校验本来就能
通过；macOS 冻结包没有系统 CA 列表，缺证书时会报
``SSL: CERTIFICATE_VERIFY_FAILED``。这里统一给出可靠的 CA 路径：优先随包
安装的 certifi，否则退回解释器默认配置。
"""
from __future__ import annotations

import ssl
from pathlib import Path

import certifi

__all__ = ["ca_bundle_path", "prefer_certifi", "https_ssl_context"]


def ca_bundle_path() -> str:
    """Path to the CA bundle HTTPS clients should trust ("" when missing)."""
    bundle = Path(certifi.where())
    return str(bundle) if bundle.is_file() else ""


def prefer_certifi() -> bool:
    """True when the interpreter default CA file is missing or unusable."""
    try:
        default = ssl.get_default_verify_paths()
    except Exception:
        return True
    cafile = getattr(default, "cafile", "") or ""
    return not (cafile and Path(cafile).is_file())


def https_ssl_context() -> ssl.SSLContext:
    """Default HTTPS context anchored on a CA bundle that is known present."""
    context = ssl.create_default_context()
    cafile = ca_bundle_path()
    if prefer_certifi() and cafile:
        context.load_verify_locations(cafile=cafile)
    return context
