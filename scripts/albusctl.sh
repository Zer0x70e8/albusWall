#!/usr/bin/env bash
# albusctl.sh — 在没有 pip 安装的情况下，直接从源码目录调用跑着的 AlbusWall。
#
# 用途：仓库是 src 布局，`python -m albuswall.client.cli` 需要 albuswall 可导入。
# 本脚本自己算出仓库根目录，把 src 塞进 PYTHONPATH，再把参数原样转交。
# 装了包（pip install -e .）之后就不需要它了，直接用 `albusctl`。
#
# 用法: scripts/albusctl.sh ping
#       scripts/albusctl.sh sources
#       scripts/albusctl.sh settings
#       scripts/albusctl.sh --json settings
#       scripts/albusctl.sh watch import.assets_imported --duration 10
set -euo pipefail

_self="${BASH_SOURCE[0]}"
_root="$(cd "$(dirname "$(readlink -f "$_self")")/.." && pwd)"

# 优先用仓库自带的 venv（PySide6/psutil 在这里），否则退回系统 python3
_py=""
for candidate in \
    "$_root/.venv/bin/python" \
    "$_root/.venv_win/Scripts/python.exe" \
    "$(command -v python3 || true)" \
    "$(command -v python || true)"
do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
        _py="$candidate"
        break
    fi
done

if [[ -z "$_py" ]]; then
    echo "albusctl: 找不到可用的 Python 解释器" >&2
    exit 2
fi

export PYTHONPATH="$_root/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$_py" -m albuswall.client.cli "$@"
