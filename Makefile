# Repository verification and package release entry points.
PY ?= python3
export V
PYTEST_ARGS ?=

.PHONY: help repository-test agent-test agent-check agent-release agent-sync-check codesearch-check

help:
	@echo "repository-test    All default test roots excluding browser and device soak suites"
	@echo "agent-test         Agent source + generated tests + version contract"
	@echo "agent-check        agent-test + tracked-file secret scan"
	@echo "agent-release V=X.Y.Z  version bump, sync and full self-check"
	@echo "agent-sync-check   sync and reject generated worktree drift"
	@echo "codesearch-check   standalone CodeSearch plugin tests"

repository-test:
	$(PY) -m pytest --ignore=tests/e2e --ignore=tests/runtime_ui --ignore=tests/soak -q $(PYTEST_ARGS)

# Standalone suites use separate processes and fixtures.
agent-test:
	$(PY) -m pytest --confcutdir=agent/gms-remote-test agent/gms-remote-test/tests -q
	$(PY) -m pytest --confcutdir=plugins/gms-remote-test plugins/gms-remote-test/tests -q
	$(PY) tools/scripts/agent/release.py --check

agent-check: agent-test
	$(PY) scripts/security/check_source_secrets.py .

# Release sync is expected to change generated files; do not require a clean
# worktree here. The separate sync check is for a staged/committed baseline.
agent-release:
	@test -n "$$V" || { echo "Usage: make agent-release V=X.Y.Z"; exit 2; }
	$(PY) tools/scripts/agent/release.py --version "$$V"
	$(MAKE) agent-check

# Compare against the index so intentionally staged release changes are allowed.
# Require all generated files to be tracked; git diff alone misses new files.
agent-sync-check:
	$(PY) tools/scripts/agent/sync_package.py .
	git diff --exit-code -- plugins/gms-remote-test/
	@untracked=$$(git ls-files --others --exclude-standard -- plugins/gms-remote-test/) && test -z "$$untracked"
	@echo "Generated plugin matches the staged baseline"

codesearch-check:
	$(PY) -m pytest --confcutdir=plugins/codesearch plugins/codesearch/tests -q
