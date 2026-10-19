"""调度层：APScheduler cron（定时兜底通道），完全自给，不依赖任何外部 automation。

- 预警：quotehub 事件驱动为主，每小时安全网
- 面板数据 panel-build：工作日 08:40 / 16:20 / 21:15；周五 16:40 全量（含一页纸）
- 专家分析 experts-refresh：周六 09:00
- 美股盘前简报：工作日 21:30
"""
from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .app_settings import auto_enabled
from .events import bus
from .jobs import run_job
from .store import log_run

_scheduler: AsyncIOScheduler | None = None
_inflight: set[str] = set()  # 同名任务单飞行，防止重入

SCHEDULES = [
    ("alert-check", CronTrigger(day_of_week="mon-fri", minute=5), "工作日每小时安全网"),
    # 面板数据：港股盘前 / 港股收盘后 / 美股盘前 三次刷新（取代原 WorkBuddy 周更与哨兵产出）
    ("panel-build", CronTrigger(day_of_week="mon-fri", hour=8, minute=40), "工作日 08:40 港股盘前"),
    ("panel-build", CronTrigger(day_of_week="mon-fri", hour=16, minute=20), "工作日 16:20 港股收盘后"),
    ("panel-build", CronTrigger(day_of_week="mon-fri", hour=21, minute=15), "工作日 21:15 美股盘前"),
    ("panel-build-full", CronTrigger(day_of_week="fri", hour=16, minute=40), "周五 16:40 含一页纸"),
    ("valuation-refresh", CronTrigger(hour=6, minute=30), "每天 06:30 估值预计算（美股收盘后、港股开盘前）"),
    ("experts-refresh", CronTrigger(day_of_week="sat", hour=9, minute=0), "周六 09:00 大师分析周更"),
    ("review-weekly", CronTrigger(day_of_week="sat", hour=9, minute=30), "周六 09:30 周度复盘"),
    ("preopen-brief", CronTrigger(day_of_week="mon-fri", hour=21, minute=30), "工作日 21:30"),
]


async def guarded_run(name: str, trigger: str, **kwargs) -> None:
    if trigger == "cron" and not auto_enabled(name):
        log_run(name, "skip", "设置中心已关闭此项自动任务")
        return
    if name in _inflight:
        log_run(name, "skip", f"上一次仍在运行（trigger={trigger}）")
        return
    _inflight.add(name)
    bus.publish("job_start", {"job": name, "trigger": trigger})
    try:
        await run_job(name, **kwargs)
    except Exception as e:
        log_run(name, "error", f"trigger={trigger}: {e}")
        bus.publish("job_error", {"job": name, "error": str(e)[:300]})
    finally:
        _inflight.discard(name)


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler:
        return _scheduler
    _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    for i, (name, trigger, desc) in enumerate(SCHEDULES):
        _scheduler.add_job(
            guarded_run,
            trigger,
            args=[name, "cron"],
            id=f"{name}#{i}",  # 同一任务可挂多个时点
            name=f"{name}（{desc}）",
            max_instances=1,
            coalesce=True,
            # 电脑睡眠/合盖时进程挂起，醒来后 3 小时内的错过任务仍补跑一次（原 10 分钟会被直接跳过）
            misfire_grace_time=3 * 3600,
        )
    # 自愈兜底（2026-10-19）：周末合盖 → 06:30 估值、08:40 面板全部错过。每 15 分钟按「数据新鲜度」检查，
    # 逾期项自动触发对应任务；同一任务 60 分钟内最多补跑一次，尊重设置中心的自动开关。
    _scheduler.add_job(catch_up, "interval", minutes=15, id="catch-up", name="逾期自动补跑（每 15 分钟检查数据新鲜度）",
                       max_instances=1, coalesce=True)
    _scheduler.start()
    # 启动后 2 分钟先检查一次（覆盖「服务刚重启 / 电脑刚唤醒」）
    import datetime as _dt
    _scheduler.add_job(catch_up, trigger="date", run_date=_dt.datetime.now() + _dt.timedelta(minutes=2),
                       id="catch-up-boot", name="启动后补跑检查")
    return _scheduler


_CATCHUP_LAST: dict[str, float] = {}
CATCHUP_COOLDOWN_SEC = 3600


async def catch_up() -> list[str]:
    """读数据新鲜度，逾期（late/missing）项按 job 字段自动补跑；返回本次触发的任务名。"""
    import asyncio
    import time as _t
    from . import freshness, portfolio
    try:
        codes = [p.get("code") for p in (portfolio.load_holdings().get("positions") or []) if p.get("code")]
        rep = freshness.evaluate(_t.time(), codes)
    except Exception as e:
        log_run("catch-up", "error", f"新鲜度检查失败：{e}")
        return []
    now = _t.time()
    jobs: list[str] = []
    for x in rep.get("items") or []:
        j = x.get("job")
        if x.get("status") not in ("late", "missing") or not j or j in jobs:
            continue
        if j in _inflight or now - _CATCHUP_LAST.get(j, 0) < CATCHUP_COOLDOWN_SEC or not auto_enabled(j):
            continue
        jobs.append(j)
    for j in jobs:
        _CATCHUP_LAST[j] = now
        asyncio.create_task(guarded_run(j, "catchup"))
    if jobs:
        log_run("catch-up", "info", "逾期自动补跑：" + "、".join(jobs))
    return jobs


def schedule_list() -> list[dict]:
    if not _scheduler:
        return []
    return [
        {"id": j.id, "name": j.name, "next_run": j.next_run_time.isoformat() if j.next_run_time else None}
        for j in _scheduler.get_jobs()
    ]
