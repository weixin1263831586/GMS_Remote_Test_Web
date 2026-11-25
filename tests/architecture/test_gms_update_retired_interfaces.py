"""The GMS Update Center has one API namespace and no retired scanner packages."""
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'features/system'
OLD_PACKAGES = ('update_' + 'monitor', 'mainline_' + 'issues', 'gms_' + 'update', 'update_' + 'radar')
OLD_API_NAMES = ('gms-update-' + 'monitor', 'mainline-known-' + 'issues')


def test_retired_scanner_packages_are_absent():
    for name in OLD_PACKAGES:
        directory = MODULE / name
        assert not any(directory.rglob('*.py')), name
        assert not any(directory.rglob('*.js')), name
        assert not any(directory.rglob('*.html')), name
    assert not (MODULE / ('reference_' + 'sync.py')).exists()
    assert (ROOT / 'features/gms/api.py').is_file()
    assert (ROOT / 'features/gms/radar/api.py').is_file()


def test_production_source_never_calls_retired_api_or_imports_retired_module():
    pattern = re.compile(
        r'/api/(?:' + '|'.join(map(re.escape, OLD_API_NAMES)) + r')(?=[/?\x22\x27\s]|$)'
        r'|features\.system\.(?:' + '|'.join(map(re.escape, OLD_PACKAGES)) + r')\b'
        r'|features/system/(?:' + '|'.join(map(re.escape, OLD_PACKAGES)) + r')/'
    )
    offenders = []
    for base in ('bootstrap', 'features', 'web', 'tools', 'scripts', '.github/workflows'):
        for path in (ROOT / base).rglob('*'):
            if path.suffix not in {'.py', '.js', '.html', '.css', '.yml', '.yaml'}:
                continue
            if 'tests' in path.relative_to(ROOT).parts or path.name.startswith('test_'):
                continue
            source = path.read_text(encoding='utf-8', errors='ignore')
            if pattern.search(source):
                offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, offenders
