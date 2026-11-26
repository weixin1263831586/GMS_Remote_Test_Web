"""Guard the test-suite inventory against accidentally undiscovered tests."""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
# Run these in separate pytest processes: the generated package mirrors the
# source tests, and CodeSearch is an independent MCP plugin.
SEPARATE_TEST_ROOTS = (
    Path('agent/gms-remote-test/tests'),
    Path('plugins/gms-remote-test/tests'),
    Path('plugins/codesearch/tests'),
)


def test_tracked_test_modules_have_a_collection_root(pytestconfig):
    default_roots = tuple(Path(path) for path in pytestconfig.getini('testpaths'))
    tracked = subprocess.run(
        ['git', 'ls-files', '-z'],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode('utf-8').split('\0')
    uncovered = []
    for name in tracked:
        path = Path(name)
        if path.suffix != '.py':
            continue
        if not (path.name.startswith('test_') or path.name.endswith('_test.py')):
            continue
        if not any(path.is_relative_to(root) for root in (*default_roots, *SEPARATE_TEST_ROOTS)):
            uncovered.append(name)
    assert not uncovered, f'Test modules without a collection root: {uncovered}'
