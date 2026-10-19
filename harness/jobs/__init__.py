"""任务注册表：定时 / 事件 / API 三通道调用的都是这里同一份实现。"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from . import alert_check, deep_diligence, news_sentinel, panel_build, preopen_brief, weekly


async def _experts_refresh() -> dict:
    """全部持仓与关注 × 个股级大师重新生成专家分析（组合层大师不参与个股）。"""
    from .. import panel_api, portfolio
    from ..store import log_run

    personas = [p["id"] for p in portfolio.load_personas() if (p.get("scope") or "stock") == "stock"]
    sem = asyncio.Semaphore(4)
    ok = fail = 0
    failures = []

    async def one(code: str, pid: str):
        nonlocal ok, fail
        async with sem:
            last_err = ""
            for attempt in range(2):  # 偶发 JSON 解析失败，重试一次即可恢复
                try:
                    r = await panel_api.generate_expert(code, pid, True)
                    if r.get("ok"):
                        ok += 1
                        return
                    last_err = str(r.get("error") or "not_ok")[:120]
                except Exception as exc:
                    last_err = f"{type(exc).__name__}:{str(exc)[:100]}"
            fail += 1
            failures.append(f"{code}/{pid}: {last_err}")

    # 2026-10-15：持仓 + 关注全覆盖（决策页「大家怎么看」对关注股同样计分，缺失会只剩机构评级）
    await asyncio.gather(*(one(s["code"], pid) for s in portfolio.all_stocks() for pid in personas))
    detail = f"成功 {ok}，失败 {fail}" + ("；" + " | ".join(failures[:8]) if failures else "")
    log_run("experts-refresh", "ok" if not fail else "partial", detail)
    return {"ok": ok, "fail": fail, "failures": failures}


async def _valuation_refresh() -> dict:
    """全组合估值（三把尺子）预计算：串行跑 westock，单只失败保留上一次结果。"""
    from .. import valuation
    from ..store import log_run
    r = await asyncio.to_thread(valuation.refresh_all, False)
    log_run("valuation-refresh", "ok" if not r.get("fail") else "partial",
            f"成功 {r.get('ok')}，数据不足 {r.get('insufficient', 0)}，失败 {r.get('fail')}" +
            ("；" + " | ".join(f"{k}: {v}" for k, v in list((r.get("errors") or {}).items())[:6]) if r.get("errors") else ""))
    return r


async def _review_weekly() -> dict:
    """周六 09:30 自动生成上周/本周组合复盘（规则计算 + 企微推送）。"""
    from .. import review
    return await review.build_review(None, {"notify": True})


# name -> (可调用, 描述)。带参数的 job（deep-diligence）经 /api/analyze/<code> 触发。
JOBS: dict[str, tuple[Callable[..., Awaitable[Any]], str]] = {
    "panel-build": (panel_build.run, "面板数据构建：财务/财报/研报（程序）+ 事件/信号/主题（DeepSeek）"),
    "panel-build-full": (panel_build.run_full, "面板全量构建：panel-build + 全组合一页纸"),
    "panel-onepager": (panel_build.run_onepager_only, "仅重建全组合公司一页纸"),
    "experts-refresh": (_experts_refresh, "全部持仓与关注 × 个股级大师分析重新生成"),
    "valuation-refresh": (_valuation_refresh, "全组合估值（三把尺子）预计算"),
    "alert-check": (alert_check.run, "预警安全网：全组合规则评估（事件驱动的定时兜底）"),
    "preopen-brief": (preopen_brief.run, "美股盘前简报"),
    "news-sentinel": (news_sentinel.run, "重大事件哨兵"),
    "review-weekly": (_review_weekly, "周六 09:30 生成组合周度复盘并推送"),
    "weekly-signals": (weekly.run_signals, "周更A：行情信号与新闻流（已被 panel-build 覆盖，保留手动）"),
    "weekly-onepager": (weekly.run_onepager, "周更B：基本面研报一页纸（deep 级，保留手动）"),
}


async def run_job(name: str, **kwargs) -> Any:
    if name == "deep-diligence":
        code = kwargs.get("code")
        if not code:
            raise KeyError("deep-diligence 需要 code 参数（用 /api/analyze/<code>）")
        return await deep_diligence.run(code)
    if name not in JOBS:
        raise KeyError(f"未知任务: {name}，可选: {list(JOBS)} / deep-diligence")
    fn, _ = JOBS[name]
    return await fn()


def job_list() -> list[dict]:
    out = [{"name": n, "description": d, "params": []} for n, (_, d) in JOBS.items()]
    out.append({"name": "deep-diligence", "description": "个股深度研究（随时触发）", "params": ["code"]})
    return out
