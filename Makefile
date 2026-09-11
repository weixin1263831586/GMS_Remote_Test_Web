# Agent 包发布与自检一键入口（根 AGENTS.md「Verification」的四步检查链）。
# 用法：
#   make agent-check             只读自检：双向 pytest + secrets 扫描 + 版本契约
#   make agent-release V=X.Y.Z   发布：改版本号（内部自动 sync 生成树）→ 复跑完整自检
#   make agent-sync-check        幂等验证：sync 后 plugins/ 不得产生 git 漂移
#   make help                    查看目标列表

PY ?= python3

.PHONY: help agent-check agent-release agent-sync-check

help:
	@echo "agent-check        双向 pytest + secrets 扫描 + release --check"
	@echo "agent-release V=X.Y.Z  版本发布（含 sync 与完整自检）"
	@echo "agent-sync-check   验证 sync_agent_package 幂等（plugins/ 无漂移）"

# 四步顺序与独立 pytest 进程为根 AGENTS.md 的硬性要求，勿合并。
agent-check:
	$(PY) -m pytest agent/gms-remote-test/tests -q
	$(PY) -m pytest plugins/gms-remote-test/tests -q
	$(PY) scripts/check_source_secrets.py .
	$(PY) tools/release_agent.py --check

# tools/release_agent.py --version 内部会调用 tools/sync_agent_package.py
# 同步 plugins/ 生成树，这里只负责在其后复跑完整自检确认新版本契约成立。
agent-release:
	@test -n "$(V)" || { echo "用法: make agent-release V=X.Y.Z"; exit 2; }
	$(PY) tools/release_agent.py --version $(V)
	$(MAKE) agent-check

# sync 必须幂等：源树未变时重复执行不得改动 plugins/。
# git diff --exit-code 对比工作区与暂存区；请在 plugins/ 相关改动入暂存区/
# 提交后运行，否则在途差异会被误报为 sync 漂移。
agent-sync-check:
	$(PY) tools/sync_agent_package.py
	@git diff --exit-code -- plugins/ && echo "plugins/ 无漂移：sync 幂等"
