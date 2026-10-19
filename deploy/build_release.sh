#!/usr/bin/env bash
# 只打包可再部署的源码与空模板；永不打包用户数据、密钥、缓存或本机虚拟环境。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
PROJECT="$(dirname "$ROOT")"
OUT="${1:-$(dirname "$PROJECT")/outputs/invest-harness-release.tar.gz}"
mkdir -p "$(dirname "$OUT")"
OUT_DIR="$(cd "$(dirname "$OUT")" && pwd -P)"
OUT="$OUT_DIR/$(basename "$OUT")"
case "$OUT" in
  "$PROJECT"|"$PROJECT"/*) printf '请把发布包输出到项目目录之外，避免重复打包\n' >&2; exit 2 ;;
esac
TMP="$(mktemp "$OUT_DIR/.invest-harness-release.XXXXXX")"
trap 'rm -f "$TMP"' EXIT
COPYFILE_DISABLE=1 tar -czf "$TMP" \
  --exclude='.DS_Store' --exclude='__pycache__' --exclude='*.pyc' \
  -C "$ROOT" \
  .env.example .gitignore README.md requirements.txt requirements-optional.txt \
  run.py start.sh harness skills web deploy
# 归档成员只能是源码白名单中的普通文件/目录；符号链接可能指向包外私有文件，直接拒绝。
python3 -c '
import sys, tarfile
allowed = {".env.example", ".gitignore", "README.md", "requirements.txt", "requirements-optional.txt", "run.py", "start.sh", "harness", "skills", "web", "deploy"}
blocked = {".env", "data", "logs", ".venv", "panel", ".workbuddy", ".git", "tests", "__pycache__", ".DS_Store", ".portfolio.pending.json"}
source_suffixes = (".py", ".md", ".json", ".j2", ".sh", ".txt", ".html", ".js", ".css", ".command")
with tarfile.open(sys.argv[1], "r:gz") as archive:
    for entry in archive:
        name = entry.name.removeprefix("./").rstrip("/")
        parts = name.split("/")
        if (parts[0] not in allowed or any(p in blocked or p.startswith("._") or (p.startswith(".env") and name != ".env.example") for p in parts)
                or entry.isfile() and name not in {".env.example", ".gitignore"} and not name.endswith(source_suffixes)
                or not (entry.isfile() or entry.isdir())):
            raise SystemExit(f"发布包检查失败：发现非源码文件或链接：{entry.name}")
' "$TMP"
mv "$TMP" "$OUT"
printf '已生成仅含白名单源码与模板、无用户数据目录和符号链接的发布包：%s\n' "$OUT"
