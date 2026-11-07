#!/usr/bin/env bash
# Build bothouse-iot firmware: clean xiaozhi-esp32 v2.5.2 + firmware/patches + firmware/components.
#
#   OTA_URL=http://<device-service-host>:8791/xiaozhi/ota/ firmware/scripts/build.sh [board] [workdir]
#
# board   xiaozhi board name (default spotpear/sp-esp32-s3-1.28-box; the role dial is only wired there)
# workdir where to clone/patch xiaozhi-esp32 (default firmware/.build/xiaozhi-esp32)
# Needs ESP-IDF v6.1 sourced (`. $IDF_PATH/export.sh`) or IDF_PATH set. Output: <workdir>/../out/.
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
BOARD=${1:-spotpear/sp-esp32-s3-1.28-box}
WORK=${2:-$HERE/.build/xiaozhi-esp32}
UPSTREAM=${XIAOZHI_REPO:-https://github.com/78/xiaozhi-esp32.git}
TAG=v2.5.2
: "${OTA_URL:?set OTA_URL, e.g. http://192.0.2.10:8791/xiaozhi/ota/ (your device service)}"

if [ -z "${IDF_PATH:-}" ] || ! command -v idf.py >/dev/null; then
  echo "ESP-IDF not in this shell: run . \$IDF_PATH/export.sh first" >&2; exit 1
fi

if [ ! -d "$WORK/.git" ]; then
  git clone --depth 1 -b "$TAG" "$UPSTREAM" "$WORK"
fi
cd "$WORK"
if ! git diff --quiet "$TAG" -- main 2>/dev/null; then
  echo "== $WORK already patched, reusing"
else
  git checkout -q "$TAG"
  git apply --whitespace=nowarn "$HERE"/patches/*.patch
  echo "== applied $(ls "$HERE"/patches/*.patch | wc -l | tr -d ' ') patch(es)"
fi
rm -rf rh_components && cp -R "$HERE/components" rh_components

# bake the OTA URL into the board's sdkconfig append (scripts/build.py reads config.json)
CFG="main/boards/$BOARD/config.json"
python3 - "$CFG" "$OTA_URL" <<'PY'
import json, sys
p, url = sys.argv[1], sys.argv[2]
d = json.load(open(p))
for b in d.get("builds", []):
    app = [x for x in b.setdefault("sdkconfig_append", []) if not x.startswith("CONFIG_OTA_URL=")]
    app.append(f'CONFIG_OTA_URL="{url}"')
    b["sdkconfig_append"] = app
json.dump(d, open(p, "w"), indent=4)
PY

export IDF_CCACHE_ENABLE=${IDF_CCACHE_ENABLE:-1}
python scripts/build.py "$BOARD" --wake-word disabled
grep -E '^CONFIG_OTA_URL=|^CONFIG_RH_' sdkconfig

OUT=$(dirname "$WORK")/out
mkdir -p "$OUT"
cp build/xiaozhi.bin build/ota_data_initial.bin build/merged-binary.bin "$OUT"/
cp build/bootloader/bootloader.bin build/partition_table/partition-table.bin "$OUT"/ 2>/dev/null || true
cp sdkconfig "$OUT"/sdkconfig.used
(cd "$OUT" && { command -v sha256sum >/dev/null && sha256sum *.bin || shasum -a 256 *.bin; } | tee SHA256SUMS)
echo BUILD_DONE
