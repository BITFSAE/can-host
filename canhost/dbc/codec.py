"""Reading and writing DBC files with their original encoding.

Chinese Windows tools save GBK; others use UTF-8 with or without a BOM.  The
encoding found on read is the default on write, so an edit never silently
changes how an existing file is stored.  A character the chosen encoding
cannot represent is an error, never a replacement.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

ENCODINGS = ("utf-8", "utf-8-sig", "gbk", "cp1252", "latin-1")
LABELS = {"utf-8": "UTF-8", "utf-8-sig": "UTF-8 带 BOM", "gbk": "GBK", "cp1252": "Windows-1252",
          "latin-1": "Latin-1"}


class DbcCodecError(ValueError):
    pass


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """Return ``(text, encoding)``; the first encoding that reads the bytes wins."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8"), "utf-8-sig"
    for encoding in ("utf-8", "gbk", "cp1252"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), "latin-1"


def encode_text(text: str, encoding: str) -> bytes:
    if encoding not in ENCODINGS:
        raise DbcCodecError(f"不支持的编码：{encoding}")
    try:
        data = text.encode("utf-8" if encoding == "utf-8-sig" else encoding)
    except UnicodeEncodeError as exc:
        bad = text[exc.start:exc.end]
        line = text.count("\n", 0, exc.start) + 1
        raise DbcCodecError(f"第 {line} 行的字符“{bad}”无法用 {LABELS[encoding]} 保存，请改用 UTF-8") from exc
    return b"\xef\xbb\xbf" + data if encoding == "utf-8-sig" else data


def read_file(path: str | Path) -> tuple[str, str]:
    return decode_bytes(Path(path).read_bytes())


def write_file(path: str | Path, text: str, encoding: str, keep_backup: bool = True) -> None:
    """Write atomically: temp file in the same directory, then replace.

    The previous contents are kept next to the file as ``<name>.bak`` so a save
    that turns out to be wrong can be undone outside the program."""
    target = Path(path)
    data = encode_text(text, encoding)
    target.parent.mkdir(parents=True, exist_ok=True)
    if keep_backup and target.exists():
        backup = target.with_name(target.name + ".bak")
        backup.write_bytes(target.read_bytes())
    handle, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
