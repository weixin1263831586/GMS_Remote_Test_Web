"""vulture allowlist.

`python3 -m vulture features foundation worker_agent tools --min-confidence 90`
的已知误报集中在这里：vulture 不理解上下文管理器协议与 mock 打桩签名，
`contextmanager`/`__exit__`/`patch(...)` 的参数被误判为未使用变量。
FastAPI 路由 handler 经装饰器注册、由框架回调，天然是 vulture 盲区，
但 min-confidence 90 只报 100% 置信度的局部变量，路由函数不在其列。

用法（与 tests/architecture 一起在 repo-contract gate 中执行）：
    python3 -m vulture features foundation worker_agent tools \
        tools/vulture_allowlist.py --min-confidence 90
"""

# contextmanager 协议的 __exit__(exc_type, exc_val, exc_tb) 签名参数。
exc_type
exc_val
exc_tb

# mock.patch(...)(...)/monkeypatch 打桩函数的未用参数（按关键字传入）。
allow_redirects

# mock 扩展返回值未解包（测试断言只关心调用发生）。
snap
tz
family
