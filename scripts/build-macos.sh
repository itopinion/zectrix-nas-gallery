#!/bin/sh
set -eu

TASK_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TASK_PYTHON=${PYTHON:-python3}
cd "$TASK_ROOT"

if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
  printf '%s\n' 'This release is built on an Apple Silicon Mac.' >&2
  exit 1
fi

"$TASK_PYTHON" -c 'import PIL, PyInstaller'
mkdir -p build/bin
xcrun swiftc -O -target arm64-apple-macosx15.0 "$TASK_ROOT/native/PortraitGate.swift" -o "$TASK_ROOT/build/portrait-gate"
"$TASK_PYTHON" scripts/collect-licenses.py
"$TASK_PYTHON" -m PyInstaller \
  --noconfirm --clean --onefile --name zectrix-nas-gallery \
  --target-architecture arm64 \
  --distpath build/bin --workpath build/pyinstaller --specpath build \
  --paths "$TASK_ROOT/app" --add-data "$TASK_ROOT/web:web" \
  --add-data "$TASK_ROOT/build/licenses:licenses" \
  --add-binary "$TASK_ROOT/build/portrait-gate:binaries" \
  "$TASK_ROOT/app/main.py"
"$TASK_PYTHON" scripts/package.py --binary build/bin/zectrix-nas-gallery "$@"
