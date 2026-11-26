"""Guard the test-suite inventory against accidentally undiscovered tests."""

import shlex
import subprocess
from collections import Counter
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
# Run these in separate pytest processes: the generated package mirrors the
# source tests, and CodeSearch is an independent MCP plugin.
SEPARATE_TEST_ROOTS = (
    Path('agent/gms-remote-test/tests'),
    Path('plugins/gms-remote-test/tests'),
    Path('plugins/codesearch/tests'),
)


def tracked_test_paths() -> list[Path]:
    tracked = subprocess.run(
        ['git', 'ls-files', '-z'],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode('utf-8').split('\0')
    return [Path(name) for name in tracked if name.endswith('.py')
            and (Path(name).name.startswith('test_') or Path(name).name.endswith('_test.py'))]


def test_tracked_test_modules_have_a_collection_root(pytestconfig):
    default_roots = tuple(Path(path) for path in pytestconfig.getini('testpaths'))
    uncovered = [str(path) for path in tracked_test_paths()
                 if not any(path.is_relative_to(root) for root in (*default_roots, *SEPARATE_TEST_ROOTS))]
    assert not uncovered, f'Test modules without a collection root: {uncovered}'


def test_feature_unit_ci_jobs_cover_each_feature_test_root_once():
    workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8'))
    expected = {f'features/{path.parts[1]}' for path in tracked_test_paths()
                if len(path.parts) >= 3 and path.parts[0] == 'features'}
    counts: Counter[str] = Counter()
    for job_id, job in workflow['jobs'].items():
        if not job_id.startswith('unit-feature-'):
            continue
        for step in job.get('steps', []):
            run = step.get('run', '').replace('\\\n', ' ')
            for line in run.splitlines():
                if not line.lstrip().startswith('pytest '):
                    continue
                words = shlex.split(line, comments=True)
                counts.update(word for word in words[1:]
                              if word.startswith('features/') and word.count('/') == 1)
    missing = sorted(expected - counts.keys())
    duplicates = sorted(root for root, count in counts.items() if count != 1)
    assert not missing, f'Feature tests missing from unit CI jobs: {missing}'
    assert not duplicates, f'Feature test roots repeated in unit CI jobs: {duplicates}'
