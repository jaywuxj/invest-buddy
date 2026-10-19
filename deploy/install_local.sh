#!/usr/bin/env bash
# 单用户本地部署，默认只绑定 127.0.0.1；绝不读取或复制作者本机数据。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
umask 077
for path in .env .venv data data/config data/panel logs data/config/holdings.json data/config/alerts.json data/config/personas.json data/config/peers.json; do
  if [ -L "$path" ]; then printf '拒绝写入符号链接：%s\n' "$path" >&2; exit 2; fi
done
PYTHON="${PYTHON:-python3}"
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 11), "需要 Python 3.11 或更新版本"'
if [ ! -x .venv/bin/python ]; then "$PYTHON" -m venv .venv; fi
.venv/bin/python -m pip install -r requirements.txt 'akshare>=1.14'
if [ "${INSTALL_FUTU:-0}" = 1 ]; then .venv/bin/python -m pip install 'futu-api>=8.0'; fi
mkdir -p data/config data/panel logs
if [ ! -e .env ]; then cp .env.example .env; fi
chmod 600 .env
if [ ! -e data/config/holdings.json ]; then printf '{"positions":[],"watch":[]}\n' > data/config/holdings.json; fi
if [ ! -e data/config/alerts.json ]; then printf '{"rules":[]}\n' > data/config/alerts.json; fi
if [ ! -e data/config/personas.json ]; then cp deploy/personas.example.json data/config/personas.json; fi
if [ ! -e data/config/peers.json ]; then cp deploy/peers.example.json data/config/peers.json; fi
chmod 700 data data/config data/panel logs
chmod 600 data/config/holdings.json data/config/alerts.json data/config/personas.json
PORT="$(.venv/bin/python -c 'from harness.settings import PORT; print(PORT)')"
if [ "$(uname -s)" = Darwin ]; then
  printf '安装完成。请先填写 %s/.env，然后在系统终端运行：%s/deploy/install_launchd.sh install\n' "$ROOT" "$ROOT"
else
  printf '安装完成。请先填写 %s/.env，再执行：%s/start.sh\n' "$ROOT" "$ROOT"
fi
printf '仅供本机访问：http://127.0.0.1:%s/；请勿把未加认证的服务暴露至公网。\n' "$PORT"
