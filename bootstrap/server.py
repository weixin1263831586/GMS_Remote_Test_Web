"""Uvicorn CLI with the Controller's HTTPS shutdown protocol as its default."""

from __future__ import annotations

from uvicorn.config import HTTP_PROTOCOLS
from uvicorn.main import main as uvicorn_main


# 同时覆盖 multiprocessing 子进程导入本入口模块的场景。
HTTP_PROTOCOLS["auto"] = "bootstrap.http_protocol:ShutdownHttpProtocol"


def main():
    # 保留 Uvicorn 完整 CLI，仅替换 auto 协议选择；显式 --http 仍可覆盖。
    uvicorn_main()


if __name__ == "__main__":
    main()
