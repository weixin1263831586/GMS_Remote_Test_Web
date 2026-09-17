#!/usr/bin/env python3

# 项目导入前加载运行环境变量。
from bootstrap.env_loader import load_runtime_env


load_runtime_env()

import logging  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402

import uvicorn  # noqa: E402

from bootstrap.application import create_app  # noqa: E402
from foundation.config import settings  # noqa: E402


# systemd 启动走 uvicorn CLI，不会执行 __main__ 分支；此前应用自身的
# logger（features.*）没有任何 handler，INFO 级诊断（例如终端握手的
# [TERMINAL] 拒绝原因）被静默丢弃，排障时 journalctl 里只剩访问日志。
# 挂到 root → stderr 后 journald 可直接收集；uvicorn 自己的 logger 带
# 独立 handler 且不向 root 传播，不会重复输出。
logging.basicConfig(
    level=getattr(
        logging,
        str(os.environ.get("GMS_LOG_LEVEL", "INFO")).upper(),
        logging.INFO,
    ),
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


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
        # no limit_max_requests. The old 10000-value made uvicorn
        # cycle the worker every few hours, dropping every WebSocket and
        # in-flight chunked upload. Memory-leak safety nets live in the
        # lifespan handlers + limit_concurrency instead.
    )
