"""组合配置读取：harness/data/config/（用户自维护的持仓与预警规则），不引用任何外部产物。"""
from __future__ import annotations

from typing import Any

from .holdings_service import holdings_snapshot
from .settings import CONFIG_DIR
from .store import read_json


def load_holdings() -> dict[str, Any]:
    return holdings_snapshot()


def all_stocks() -> list[dict]:
    h = load_holdings()
    return list(h.get("positions") or []) + list(h.get("watch") or [])


def positions() -> list[dict]:
    return list(load_holdings().get("positions") or [])


def gtimg_codes() -> list[str]:
    return [s["gtimg"] for s in all_stocks() if s.get("gtimg")]


def gtimg_to_code() -> dict[str, str]:
    return {s["gtimg"]: s["code"] for s in all_stocks() if s.get("gtimg") and s.get("code")}


def stock_meta(code: str) -> dict | None:
    for s in all_stocks():
        if s.get("code") == code:
            return s
    return None


_REF_LABEL = {"target_min": "机构目标价下限", "target_avg": "机构目标均价", "target_max": "机构目标价上限"}


def resolved_alert_config() -> dict:
    """预警规则解析：value_ref（如 research.target_min）在读取时从 harness 自产研报实时取值。

    取不到值（如美股未配目标价数据源）→ 该规则本轮 active=False 并标注原因，不会误报。
    """
    from .settings import PANEL_OUT_DIR

    cfg = read_json(CONFIG_DIR / "alerts.json", {"rules": []}) or {"rules": []}
    research = ((read_json(PANEL_OUT_DIR / "research.json", {}) or {}).get("items")) or {}
    out = []
    for r in cfg.get("rules") or []:
        r = dict(r)
        ref = r.get("value_ref")
        if ref:
            field = ref.split(".", 1)[-1]
            item = research.get(r.get("code")) or {}
            val = item.get(field)
            label = _REF_LABEL.get(field, field)
            if val is None:
                r["value"], r["active"] = None, False
                r["note"] = f"{label}暂无数据（等待研报数据源），规则暂停"
            else:
                r["value"] = val
                r["note"] = f"{label} {val}（{item.get('target_count', '--')} 家，harness 研报实时值）" + (f"；{r['memo']}" if r.get("memo") else "")
        out.append(r)
    return {**cfg, "rules": out}


def load_alert_rules() -> list[dict]:
    return [r for r in resolved_alert_config()["rules"] if r.get("active", True) and r.get("value") is not None]


def load_personas() -> list[dict]:
    return (read_json(CONFIG_DIR / "personas.json", {}) or {}).get("personas") or []
