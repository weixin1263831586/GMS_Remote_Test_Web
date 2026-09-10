#!/usr/bin/env python3

# 项目导入前加载运行环境变量。
from bootstrap.env_loader import load_runtime_env


load_runtime_env()

import logging  # noqa: E402

import uvicorn  # noqa: E402

from bootstrap.application import create_app  # noqa: E402
from foundation.config import settings  # noqa: E402


app = create_app()


if __name__ == '__main__':
    logging.getLogger(__name__).info('Starting GMS Auto Test FastAPI Server')
    uvicorn.run(
        'app:app',
        host=settings.server_host,
        port=settings.server_port,
        proxy_headers=settings.proxy_headers_enabled,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        timeout_keep_alive=120,
        access_log=settings.environment != 'production',
        limit_concurrency=500,
        # 11.txt P2-2: no limit_max_requests. The old 10000-value made uvicorn
        # cycle the worker every few hours, dropping every WebSocket and
        # in-flight chunked upload. Memory-leak safety nets live in the
        # lifespan handlers + limit_concurrency instead.
    )
