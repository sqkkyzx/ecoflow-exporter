#!/bin/zsh
set -eu
PROBE_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$PROBE_DIR/../.." && pwd)"
PROBE_PYTHON="$PROJECT_DIR/work/ecoflow-probe-runtime/bin/python"
cd "$PROBE_DIR"
if [[ ! -x "$PROBE_PYTHON" ]]; then
  echo '請先用 start.command 完成初次設定。'
else
  PYTHONDONTWRITEBYTECODE=1 "$PROBE_PYTHON" "$PROBE_DIR/probe.py" status --follow || true
fi
echo
read 'REPLY?按 Enter 關閉摘要視窗……'
