"""Repository-wide test environment controls.

Isolated router tests intentionally call endpoints without constructing platform
sessions. Production and normal application runs remain secure-by-default; only
pytest explicitly disables the global middleware gate. Dedicated security tests
override this variable and verify the production behavior.
"""

import os


# 在应用导入前隔离部署环境配置。
os.environ["GMS_SKIP_RUNTIME_ENV"] = "1"
os.environ.setdefault("GMS_AUTH_REQUIRED", "false")

# 生产环境由 bootstrap.dependencies 在启动时注册 cluster port；测试里
# 不一定经过 create_app()。这里统一注册，使单独运行某个测试文件与
# 全量运行行为一致（port 未注册时 get_cluster_service 会抛 RuntimeError
# 并被 API 层转成 503，导致依赖 patch cluster_api.cluster_service 的
# 用例在独立运行时失败）。port 包装按调用时解析名字，仍尊重测试 patch。
from features.cluster import register_cluster_port


register_cluster_port()
