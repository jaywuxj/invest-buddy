#!/usr/bin/env python3
"""harness 入口：调度器 + QuoteHub + 开放 API，单进程常驻（launchd 看护）。

用法:
  python run.py                 # 启动全部（默认 127.0.0.1:7788）
  python run.py --job alert-check   # 手动单次跑一个任务后退出（调试用）
  python run.py --analyze 01810.HK  # 手动深研一只后退出
"""
from __future__ import annotations

import argparse
import asyncio
import sys


def main() -> None:
    from harness import settings  # .env 在 settings 的模块级配置读取前加载

    parser = argparse.ArgumentParser()
    parser.add_argument("--job", help="单次运行指定任务后退出")
    parser.add_argument("--analyze", metavar="CODE", help="单次深研指定标的后退出")
    args = parser.parse_args()

    if args.job or args.analyze:
        from harness.jobs import run_job

        async def once():
            if args.job:
                return await run_job(args.job)
            return await run_job("deep-diligence", code=args.analyze)

        result = asyncio.run(once())
        print(result)
        return

    import uvicorn

    if settings.HOST not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("独立单用户版仅允许绑定本机回环地址；请勿把包含持仓和密钥操作的 API 暴露到公网")
    uvicorn.run("harness.api:app", host=settings.HOST, port=settings.PORT, log_level="info")


if __name__ == "__main__":
    sys.exit(main())
