# 平台支持矩阵（Python / Ubuntu）

2026-10 外部代码复审要求明确"生产支持的 Python、Ubuntu 版本矩阵，
并对每个受支持环境进行依赖审计和 OCR 运行测试"。本文是唯一权威清单；
变更矩阵必须同步 CI（`.github/workflows/ci.yml`）与本文件。

## Python

| 版本 | 角色 | 依赖审计 | 说明 |
|------|------|----------|------|
| 3.10 | 运行基线（生产） | `dependency-audit` 3.10 腿 | Pillow 固定 12.3.0（13.0.0 要求 ≥3.11），已知告警按 `.github/pip-audit-py310-waivers.txt` 逐 ID 豁免；清单外新告警失败门禁 |
| 3.11 | 审计参考 + CI unit | `dependency-audit` 3.11 腿（零豁免） | 解析出 Pillow 13.0.0 安全修复版；升级 3.10 → 3.11 后删除 12.3.0 分支与豁免清单 |

退出条件：生产解释器升级到 3.11 后，`requirements.txt` 删除
`python_version < "3.11"` 分支，删除豁免清单与 3.10 审计腿。

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
- 本机复验（22.04 宿主）：`python3 -m pip_audit -r requirements.txt --strict`
  （3.10 + 豁免清单）与
  `pytest features/redmine/tests/test_evidence_images.py -q`。
