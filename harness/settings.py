"""全局配置：环境变量优先，默认值兜底。不硬编码密钥。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent          # harness/ 项目根


def load_dotenv() -> None:
    """先读取项目根 .env，再生成模块级配置；进程环境变量优先。"""
    f = BASE_DIR / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


load_dotenv()

DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
SKILLS_DIR = BASE_DIR / "skills"

# 完全自给：不再引用 panel/data（WorkBuddy 产物）。
# CONFIG_DIR = 用户配置（持仓/预警规则/人设展示），由用户维护；
# PANEL_OUT_DIR = 面板契约数据，全部由 harness 生产者自产（jobs/panel_build.py）
CONFIG_DIR = DATA_DIR / "config"
PANEL_OUT_DIR = DATA_DIR / "panel"

HOST = os.environ.get("HARNESS_HOST", "127.0.0.1")
PORT = int(os.environ.get("HARNESS_PORT", "7788"))

# 行情轮询（gtimg 备份通道）间隔秒；富途可用时仅作交叉校验
QUOTE_POLL_INTERVAL = float(os.environ.get("QUOTE_POLL_INTERVAL", "10"))
# 仅在交易时段轮询：工作日 09:00-16:30（港股）与 21:00-05:00（美股，夏令时近似）
QUOTE_POLL_MARKET_ONLY = os.environ.get("QUOTE_POLL_MARKET_ONLY", "1") == "1"

QUOTE_CACHE_TTL_SEC = float(os.environ.get("QUOTE_CACHE_TTL_SEC", "3"))

# 预警命中后的 webhook（企业微信/Telegram bot，可选）
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")

# ---------- LLM 提供方与模型路由 ----------
# 路由 alias -> (provider, model)。可用环境变量覆盖，如 HARNESS_ROUTE_PRO=doubao:doubao-seed-2-0-pro
# 密钥仍在使用时现读，避免修改进程环境变量后的旧值滞留。
LLM_PROVIDERS = {
    "deepseek": {
        "base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "key_env": "DEEPSEEK_API_KEY",
    },
    "glm": {
        "base_url": os.environ.get("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
        "key_env": "GLM_API_KEY",
    },
    "doubao": {
        "base_url": os.environ.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
        "key_env": "ARK_API_KEY",
    },
}


def get_api_key(provider: str) -> str:
    cfg = LLM_PROVIDERS.get(provider) or {}
    return os.environ.get(cfg.get("key_env", ""), "")

def _route(alias: str, default: str) -> tuple[str, str]:
    raw = os.environ.get(f"HARNESS_ROUTE_{alias.upper()}", default)
    provider, _, model = raw.partition(":")
    return provider, model

MODEL_ROUTES = {
    "flash": _route("flash", "deepseek:deepseek-chat"),
    "pro": _route("pro", "deepseek:deepseek-reasoner"),
    "checker": _route("checker", "glm:glm-4-flash"),
}

# 价格表：元 / 百万 tokens（input, output）。签约后按官网校准；可用 env 覆盖单个，如 PRICE_DEEPSEEK_CHAT=1,2
PRICE_TABLE = {
    "deepseek-chat": (1.0, 2.0),
    "deepseek-reasoner": (4.0, 16.0),
    "glm-4-flash": (0.0, 0.0),
    "glm-4-air": (0.5, 0.5),
    "doubao-seed-2-0-mini": (0.3, 0.6),
    "doubao-seed-2-0-pro": (3.0, 9.0),
}

LLM_TIMEOUT_SEC = float(os.environ.get("LLM_TIMEOUT_SEC", "120"))
LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "2"))

# 数字回读对账容差（1%）与豁免上限（小于该值的数视为分数/序号/年份类，不参与对账）
RECONCILE_TOLERANCE = float(os.environ.get("RECONCILE_TOLERANCE", "0.01"))
RECONCILE_MIN_MAGNITUDE = float(os.environ.get("RECONCILE_MIN_MAGNITUDE", "100"))

# Finnhub（美股新闻，可选）
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")

# 富途 OpenD（可选；需本地运行 OpenD 网关）
FUTU_HOST = os.environ.get("FUTU_HOST", "127.0.0.1")
FUTU_PORT = int(os.environ.get("FUTU_PORT", "11111"))

for directory in (DATA_DIR, LOGS_DIR, DATA_DIR / "diligence", CONFIG_DIR, PANEL_OUT_DIR):
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass


def env_key(name: str) -> str:
    """密钥在使用时读取，不缓存到模块级常量。"""
    return os.environ.get(name, "").strip()
