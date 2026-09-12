#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PROJECT_ROOT/.venv-canhost-build-macos"
PYTHON_BIN="${CANHOST_PYTHON:-python3.11}"
LABEL="${1:-}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "macOS DMG 只能在 macOS 上构建。" >&2
  exit 1
fi
if [[ "$(uname -m)" != "arm64" ]]; then
  echo "当前脚本生成 Apple Silicon 原生包，请在 arm64 Mac 上运行。" >&2
  exit 1
fi
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "未找到 $PYTHON_BIN；请安装 arm64 Python 3.11，或用 CANHOST_PYTHON 指定。" >&2
  exit 1
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
PYTHON="$VENV_DIR/bin/python"

"$PYTHON" -m pip install --retries 10 --timeout 60 -r "$PROJECT_ROOT/requirements-build.txt"

cd "$PROJECT_ROOT"
"$PYTHON" -m unittest discover -s Tests -p "test_*.py" -v
"$PYTHON" -m PyInstaller --noconfirm --clean "$PROJECT_ROOT/can_host_macos.spec"

APP="$PROJECT_ROOT/dist/BITFSAE CAN Host.app"
EXECUTABLE="$APP/Contents/MacOS/BITFSAE_CAN_Host"
if [[ ! -x "$EXECUTABLE" ]]; then
  echo "打包失败：未生成 $EXECUTABLE" >&2
  exit 1
fi
if ! file "$EXECUTABLE" | grep -q "arm64"; then
  echo "打包失败：主程序不是 arm64。" >&2
  file "$EXECUTABLE" >&2
  exit 1
fi

"$EXECUTABLE" --packaging-smoke-test

# CI does not install the third-party MacCAN driver.  On a developer/release
# Mac that has it, additionally prove that the frozen executable itself can
# find and load libPCBUSB.  This does not open an adapter or transmit a frame.
if "$PYTHON" -c 'from can.interfaces.pcan.basic import PCANBasic; PCANBasic()' >/dev/null 2>&1; then
  "$EXECUTABLE" --pcan-driver-smoke-test
  echo "PCAN driver load check passed in the frozen app."
else
  echo "PCAN driver load check skipped: libPCBUSB is not installed on this build machine."
fi

# Info.plist must not promise an older macOS release than a bundled Mach-O
# dependency actually supports.  PyInstaller does not validate this itself.
"$PYTHON" - "$APP" <<'PY'
from pathlib import Path
import plistlib
import subprocess
import sys

app = Path(sys.argv[1])
with (app / "Contents" / "Info.plist").open("rb") as stream:
    declared_text = str(plistlib.load(stream)["LSMinimumSystemVersion"])

def version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))

declared = version_tuple(declared_text)
violations = []
checked = 0
for path in (app / "Contents").rglob("*"):
    if not path.is_file():
        continue
    result = subprocess.run(
        ["vtool", "-show-build", str(path)], capture_output=True, text=True,
    )
    if result.returncode != 0:
        continue
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == "minos":
            checked += 1
            minimum = fields[1]
            if version_tuple(minimum) > declared:
                violations.append(f"{path.relative_to(app)} requires macOS {minimum}")
if violations:
    raise SystemExit(
        f"Info.plist declares macOS {declared_text}, but bundled binaries require newer:\n"
        + "\n".join(violations)
    )
if checked == 0:
    raise SystemExit("No Mach-O deployment targets were found in the app bundle")
print(f"Minimum macOS version check passed: {declared_text} ({checked} targets)")
PY

codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

if [[ -z "$LABEL" ]]; then
  LABEL="$($PYTHON -c 'from canhost import __version__; print(__version__)')"
fi
LABEL="${LABEL#v}"
LABEL="${LABEL#V}"

RELEASE_DIR="$PROJECT_ROOT/release"
DMG_NAME="BITFSAE_CAN_Host_macOS_arm64_v${LABEL}.dmg"
DMG_PATH="$RELEASE_DIR/$DMG_NAME"
STAGING_DIR="$(mktemp -d "${TMPDIR:-/tmp}/canhost-dmg.XXXXXX")"
trap 'rm -rf "$STAGING_DIR"' EXIT

mkdir -p "$RELEASE_DIR"
cp -R "$APP" "$STAGING_DIR/BITFSAE CAN Host.app"
ln -s /Applications "$STAGING_DIR/Applications"
rm -f "$DMG_PATH" "$DMG_PATH.sha256"
hdiutil create -volname "BITFSAE CAN Host" -srcfolder "$STAGING_DIR" \
  -ov -format ULMO "$DMG_PATH"
(cd "$RELEASE_DIR" && shasum -a 256 "$DMG_NAME" > "$DMG_NAME.sha256")

echo "Build complete: $DMG_PATH"
