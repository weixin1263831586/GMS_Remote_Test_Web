#!/usr/bin/env python3

# 项目导入前加载运行环境变量。
from bootstrap.env_loader import load_runtime_env

load_runtime_env()

import logging

import uvicorn

from bootstrap.application import create_app
from foundation.config import settings


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
        limit_max_requests=10000,
    )
