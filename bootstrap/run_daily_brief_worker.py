"""Composition-root entry for the standalone Redmine daily-brief worker.

Mirrors ``app.py``: runtime environment (Redmine credentials etc. from
configs/runtime.json) must be merged into ``os.environ`` before any feature
module import, so systemd-started worker processes see the same credentials
as the web service. Kept at the composition root (bootstrap → features) —
feature modules must not import bootstrap back.
"""

# 项目(feature)导入前加载运行环境变量——见 app.py 同款模式。
from bootstrap.env_loader import load_runtime_env


load_runtime_env()

from features.redmine.daily_brief_worker import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
