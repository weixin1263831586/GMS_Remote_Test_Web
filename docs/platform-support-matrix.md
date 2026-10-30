# 平台支持矩阵（Python / Ubuntu）

2026-10 外部代码复审要求明确"生产支持的 Python、Ubuntu 版本矩阵，
并对每个受支持环境进行依赖审计和 OCR 运行测试"。本文是唯一权威清单；
变更矩阵必须同步 CI（`.github/workflows/ci.yml`）与本文件。

## Python

| 版本 | 角色 | CI 运行位置 | 依赖审计 | 说明 |
|------|------|-------------|----------|------|
| 3.10 | 运行基线（生产）+ **全部 CI 单测/集成 job** | `unit-*`、`browser-e2e`、`worker-agent`、设备与固件等 job 的 `python-version: "3.10"` | `dependency-audit` 3.10 腿 | Pillow 固定 12.3.0（13.0.0 要求 ≥3.11），已知告警按 `.github/pip-audit-py310-waivers.txt` 逐 ID 豁免；清单外新告警失败门禁 |
| 3.11 | 依赖审计参考腿 | 仅 `dependency-audit` matrix 的 3.11 腿 | `dependency-audit` 3.11 腿（零豁免） | 解析出 Pillow 13.0.0 安全修复版；升级 3.10 → 3.11 后删除 12.3.0 分支与豁免清单 |

退出条件：生产解释器升级到 3.11 后，`requirements.txt` 删除
`python_version < "3.11"` 分支，删除豁免清单与 3.10 审计腿，并给出
风险退出时间表（37 条公告为"显式接受"，不是"已修复"）。

### Python 3.10 豁免退出计划

以下为 2026-10-27 制定的建议目标，实际生产切换由部署负责人执行；
日期不代表已经完成验收或可以跳过门禁。继续保留当前 3.10 审计腿，
直到生产迁移完成。

| 建议目标日期 | 工作 | 验收与退出条件 |
|--------------|------|----------------|
| 2026-11-03 | 在隔离的 Ubuntu 22.04 / Python 3.11 环境安装生产与开发依赖 | 两份依赖清单零豁免审计；运行 Assistant、Redmine（含真实 OCR）、集群、设备、固件与 Agent 包现有回归测试，补齐当前只有依赖审计的 3.11 运行验证 |
| 2026-11-10 | 部署负责人在预生产环境验证 Controller / Worker 和 Agent | 使用 `gms-agent doctor --json` 与 `gms-rt-system-selfcheck --json` 验收 Agent 部署；验证任务派发、报告回传、附件下载取消及 OCR，保存结果并准备旧环境回退方案 |
| 2026-11-17 | 在上述门禁通过后迁移生产解释器并清退豁免 | 生产确认均已切换后，将 Python 最低版本与 CI 运行基线同步提高到 3.11，删除 Pillow 12.3.0 条件分支、37 条豁免及 3.10 审计腿，更新本矩阵 |

任一阶段未通过时保留现有基线与审计门禁，记录阻塞原因和新的目标日期，
不得通过扩充豁免或提前删除 3.10 审计腿将迁移标为完成。

## Ubuntu（Controller / Worker 宿主）

| 版本 | 角色 | 内置 OCR 引擎 | 验证方式 |
|------|------|---------------|----------|
| 22.04 | CI 验证基线 + 当前生产宿主 | `tools/tesseract` 可加载（libtiff.so.5 / libgif.so.7 系统库齐备） | `unit-assistant-reports-rest` job（固定 `ubuntu-22.04`）内的真实 OCR 集成测试 |
| 24.04 | **未支持** | 内置引擎按 libtiff.so.5 链接，24.04 只有 libtiff6，加载失败 | 不部署；升级前必须在 24.04 上重建 `tools/tesseract`（`ldd` 全量核查无 not found）并让 OCR 集成测试转绿后再切 `runs-on` |

不允许为兼容新宿主在 CI 里逐库追加 `apt-get install`：那是每失败一次
补一个库的反模式（外部复审已指出的反模式）。正确路径是在目标系统重建内
置引擎或换用系统 tesseract。

## 门禁映射

- 依赖审计：`dependency-audit` matrix 双腿（3.11 零豁免 / 3.10 按清单
  豁免）——换解释器掩盖旧环境告警是不允许的。
- OCR：真实 Tesseract 集成测试只在 22.04 基线上运行；`bundled_tesseract_cmd`
  在引擎缺失/不可加载时优雅降级（派生状态 missing），部署到不支持宿主
  不会崩溃，但"OCR 可用"仅承诺 22.04。
- 本机复验（22.04 宿主，3.10 解释器）——与 CI 3.10 腿等价，**必须带豁免
  参数**：
  ```bash
  WAIVERS=$(grep -v '^#' .github/pip-audit-py310-waivers.txt | sed '/^$/d' | sed 's/^/--ignore-vuln /' | tr '\n' ' ')
  python3 -m pip_audit -r requirements.txt --strict $WAIVERS
  python3 -m pip_audit -r requirements-dev.txt --strict $WAIVERS
  pytest features/redmine/tests/test_evidence_images.py -q
  ```
