#!/bin/bash
# 双击运行：在系统终端中把 harness 注册为 macOS 开机自启服务（launchd）
cd "$(dirname "$0")"
./install_launchd.sh install
echo
./install_launchd.sh status
echo
read -n 1 -s -r -p "按任意键关闭窗口…"
