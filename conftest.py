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
