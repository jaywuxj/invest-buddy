#!/bin/bash
# 把 harness 注册为 macOS 用户级 LaunchAgent：登录即启动、崩溃自动拉起、与任何 IDE/会话无关。
# 用法：deploy/install_launchd.sh [install|uninstall|restart|status|logs]
set -euo pipefail

LABEL="com.invest.harness"
HARNESS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$HARNESS_DIR/.venv/bin/python"
DOMAIN="gui/$(id -u)"
LOG_DIR="$HARNESS_DIR/logs"

service_port() {
  # 与 plist 的干净环境一致；不能让安装者终端导出的 HARNESS_PORT 覆盖服务实际端口。
  env -i PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" HOME="$HOME" \
    "$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); from harness.settings import PORT; print(PORT)' "$HARNESS_DIR"
}

xml_escape() {
  "$PY" -c 'import html, sys; print(html.escape(sys.argv[1], quote=True))' "$1"
}

write_plist() {
  mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
  local py_xml run_xml dir_xml stdout_xml stderr_xml
  py_xml="$(xml_escape "$PY")"
  run_xml="$(xml_escape "$HARNESS_DIR/run.py")"
  dir_xml="$(xml_escape "$HARNESS_DIR")"
  stdout_xml="$(xml_escape "$LOG_DIR/launchd.out.log")"
  stderr_xml="$(xml_escape "$LOG_DIR/launchd.err.log")"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$py_xml</string>
    <string>$run_xml</string>
  </array>
  <key>WorkingDirectory</key><string>$dir_xml</string>
  <!-- 干净环境：不继承任何 IDE 注入的 PYTHONPATH/shim，只给最小 PATH -->
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>LANG</key><string>en_US.UTF-8</string>
    <key>LC_ALL</key><string>en_US.UTF-8</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>PYTHONNOUSERSITE</key><string>1</string>
    <key>TZ</key><string>Asia/Shanghai</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <!-- 任何退出（崩溃/被 kill）都自动拉起；10 秒节流防止崩溃风暴 -->
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$stdout_xml</string>
  <key>StandardErrorPath</key><string>$stderr_xml</string>
</dict>
</plist>
EOF
  plutil -lint "$PLIST" >/dev/null
}

is_loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

case "${1:-install}" in
  install)
    [ -x "$PY" ] || { printf '缺少 %s，请先执行 deploy/install_local.sh\n' "$PY" >&2; exit 1; }
    PORT="$(service_port)"
    if lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && ! is_loaded; then
      printf '端口 %s 已被其他进程占用。请自行确认进程并停止后再安装；不会自动终止。\n' "$PORT" >&2
      lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2 || true
      exit 3
    fi
    if is_loaded; then
      echo "检测到已安装的服务，先卸载…"
      launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
      # bootout 是异步的：旧服务未拆完就 bootstrap 会报 5: Input/output error（踩过的坑）
      for i in $(seq 1 20); do is_loaded || break; sleep 0.5; done
      for i in $(seq 1 20); do lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 0.5; done
    fi
    write_plist
    launchctl enable "$DOMAIN/$LABEL" 2>/dev/null || true
    ok=0
    for attempt in 1 2 3; do
      if launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null; then ok=1; break; fi
      is_loaded && { ok=1; break; }
      sleep 2
    done
    if [ "$ok" != 1 ]; then
      echo "launchctl bootstrap 失败。若在 IDE/AI 助手内置终端运行，请改用系统「终端.app」执行本脚本。" >&2
      exit 5
    fi
    echo "已安装：${PLIST}，等待服务就绪…"
    for i in $(seq 1 20); do
      sleep 1
      if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
        printf 'harness 已由 launchd 托管运行：http://127.0.0.1:%s/\n' "$PORT"
        exit 0
      fi
    done
    echo "20 秒内服务未就绪，最近错误日志：" >&2
    tail -n 15 "$LOG_DIR/launchd.err.log" >&2 || true
    if grep -q "Operation not permitted" "$LOG_DIR/launchd.err.log" 2>/dev/null; then
      echo "macOS 隐私保护可能拦截了「文稿」目录：系统设置 › 隐私与安全性 › 完全磁盘访问权限，" >&2
      echo "  添加 $("$PY" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$PY" 2>/dev/null || printf '%s' "$PY")，然后执行：$0 restart" >&2
    fi
    exit 1
    ;;
  uninstall)
    is_loaded && launchctl bootout "$DOMAIN/$LABEL" || true
    rm -f "$PLIST"; echo "已卸载"
    ;;
  restart)
    launchctl kickstart -k "$DOMAIN/$LABEL"; echo "已重启"
    ;;
  status)
    launchctl print "$DOMAIN/$LABEL" | grep -E "state|pid|last exit|runs" || echo "未加载"
    [ -x "$PY" ] || { echo "health: 未安装 .venv"; exit 0; }
    PORT="$(service_port)"
    curl -fsS --max-time 3 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && printf 'health: OK (端口 %s)\n' "$PORT" || printf 'health: 无响应 (端口 %s)\n' "$PORT"
    ;;
  logs)
    tail -n 40 "$LOG_DIR/launchd.err.log"
    ;;
  *) echo "用法: $0 [install|uninstall|restart|status|logs]"; exit 1 ;;
esac
