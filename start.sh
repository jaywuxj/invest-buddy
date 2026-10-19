#!/bin/bash
# harness 启动脚本：只使用项目内独立 venv（完全不依赖 WorkBuddy 运行时）
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "缺少 .venv：请先执行 python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi
exec .venv/bin/python run.py "$@"
