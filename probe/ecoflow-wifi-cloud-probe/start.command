#!/bin/zsh
set -eu
umask 077
PROBE_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$PROBE_DIR/../.." && pwd)"
PROBE_ENV="$PROJECT_DIR/work/ecoflow-probe-runtime"
PROBE_CA="$PROJECT_DIR/config/mac-system-roots.pem"
cd "$PROBE_DIR"
echo 'EcoFlow RIVER 3 UPS 唯讀採集測試'
if ! command -v python3 >/dev/null 2>&1; then
  echo '找不到 Python 3。請先安裝 Python 3.10 或以上版本。'
  read 'REPLY?按 Enter 關閉……'
  exit 1
fi
if [[ ! -x "$PROBE_ENV/bin/python" ]]; then
  python3 -m venv "$PROBE_ENV"
fi
mkdir -p "$(dirname "$PROBE_CA")"
security find-certificate -a -p /System/Library/Keychains/SystemRootCertificates.keychain > "$PROBE_CA"
# Use macOS system root certificates; never disable certificate verification.
export SSL_CERT_FILE="$PROBE_CA"
export PIP_CERT="$PROBE_CA"
export PYTHONDONTWRITEBYTECODE=1
if ! "$PROBE_ENV/bin/python" -c 'import paho.mqtt.client; import probe; import ef_river3_pb2' >/dev/null 2>&1; then
  "$PROBE_ENV/bin/python" -m pip install --disable-pip-version-check --index-url https://pypi.org/simple -r "$PROBE_DIR/requirements.txt"
fi
"$PROBE_ENV/bin/python" "$PROBE_DIR/probe.py" wizard || PROBE_EXIT=$?
echo
echo "已結束（退出碼 ${PROBE_EXIT:-0}）。結果保存在工具資料夾的 results/。"
read 'REPLY?按 Enter 關閉……'
