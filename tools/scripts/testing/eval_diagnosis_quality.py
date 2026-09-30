"""Diagnosis quality evaluation against the Golden Corpus (offline, no AI).

把 Wiki Golden Query quality gate 的思想扩展到 AI 诊断：

- ``--split development``：日常开发/调 Prompt 只看开发集；
- ``--split holdout``：release candidate 才跑，防"对着考卷调参"；
- 默认全量，退出码非 0 表示有语料未通过（接 CI 的 gate 语义）。

输入是 DiagnosisReadModel 形状的结果载荷（JSON 文件或 stdin JSONL）：
每行 ``{"case_id": ..., "result": {...}}``。没有结果文件的 ``--validate``
模式只做语料契约校验，供 CI 在无 AI 后端的环境里跑。

用法::

    python tools/scripts/testing/eval_diagnosis_quality.py --validate
    python tools/scripts/testing/eval_diagnosis_quality.py --split development \
        --results /tmp/diagnosis_results.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import find_repo_root


REPO_ROOT = find_repo_root()
sys.path.insert(0, str(REPO_ROOT / "tests" / "quality"))

from diagnosis_corpus import grade_result, load_corpus  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--split",
        choices=("development", "holdout"),
        default=None,
        help="只评测指定池（默认全量）",
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=None,
        help="JSONL 结果文件（每行 {case_id, result}）；缺省时只校验语料",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="只做语料契约校验，不评分",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="机器可读输出（供 CI 汇总）",
    )
    args = parser.parse_args()

    cases = load_corpus(args.split)
    errors: list[str] = []
    seen_ids: set[str] = set()
    for case in cases:
        case_id = str(case.get("id") or "")
        if not case_id:
            errors.append("case missing id")
        elif case_id in seen_ids:
            errors.append(f"duplicate case id: {case_id}")
        seen_ids.add(case_id)
        if case.get("split") not in ("development", "holdout"):
            errors.append(f"{case_id}: bad split {case.get('split')!r}")

    if args.validate:
        if args.json:
            print(json.dumps({"cases": len(cases), "errors": errors}))
        else:
            print(f"corpus cases: {len(cases)}")
            for error in errors:
                print(f"ERROR: {error}")
        return 1 if errors else 0

    if not args.results or not args.results.exists():
        print("ERROR: --results JSONL file required for grading", file=sys.stderr)
        return 2

    by_id = {str(case["id"]): case for case in cases}
    reports = []
    missing_results: list[str] = []
    for line in args.results.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        case_id = str(row.get("case_id") or "")
        case = by_id.get(case_id)
        if case is None:
            errors.append(f"result references unknown case: {case_id}")
            continue
        report = grade_result(row.get("result") or {}, case)
        reports.append(report)
    missing_results = sorted(set(by_id) - {r.case_id for r in reports})

    failed = [r for r in reports if not r.passed]
    passed = len(reports) - len(failed)
    if args.json:
        print(json.dumps({
            "cases": len(cases),
            "graded": len(reports),
            "passed": passed,
            "failed": len(failed),
            "missing_results": missing_results,
            "errors": errors,
            "reports": [r.to_dict() for r in failed],
        }, ensure_ascii=False))
    else:
        print(f"graded {len(reports)}/{len(cases)} cases: {passed} passed, {len(failed)} failed")
        for report in failed:
            print(f"FAIL {report.case_id}: {'; '.join(report.failures)}")
        for case_id in missing_results:
            print(f"MISSING result for case {case_id}")
        for error in errors:
            print(f"ERROR: {error}")
    # 语料错误 / 失败用例 / 缺结果都视为不通过（gate 语义）。
    return 1 if (errors or failed or missing_results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
