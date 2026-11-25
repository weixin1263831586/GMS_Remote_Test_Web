"""仓库级契约扫描。

- 静态资源未引用扫描：web/static 下的资产必须至少被一处 HTML/JS/文档
  引用，防止文件拆分后遗留不可达的死资源。
- 文档链接检查：docs/ 内 Markdown 的仓库相对链接必须可解析。
- 路由/文档契约：bootstrap.routes 注册的 /api 路由前缀必须在
  tests/contract/snapshots/routes.json 中有契约锚点；snapshot 由
  tests/contract 维护，这里只防"新增路由完全无契约"的漂移。

三个检查都是"防漂移"而非"全量证明"：扫描器维护成本必须低，
误报治理优先于查全率（新形态的引用允许在 ALLOWLIST 中豁免并注明原因）。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WEB_STATIC = ROOT / 'web' / 'static'
DOCS = ROOT / 'docs'

# 已知未引用但有意保留的资产（写明原因，删除时一并移除）。
UNREFERENCED_ASSET_ALLOWLIST = {
    # 浏览器按约定 URL 直接请求 /favicon.ico（features/system/api.py
    # 的路由），页面/模板不按文件名引用。
    'web/static/favicon.ico',
}

# 运行时缓存目录：icon_fetcher 按内容哈希落盘（LOCAL_ICON_DIR），
# 文件名只存在于数据库/localStorage，不在仓库文本里，天然不可静态引用。
RUNTIME_GENERATED_DIRS = ('web/static/icons/favicons',)

# 文档链接检查豁免的 URL 前缀（外部链接不做网络请求）。
_EXTERNAL_SCHEMES = ('http://', 'https://', 'mailto:')


class StaticAssetReferenceTests(unittest.TestCase):
    """web/static 资产必须被至少一处文本引用。"""

    def test_every_static_asset_is_referenced_somewhere(self):
        assets = sorted(
            path for path in WEB_STATIC.rglob('*')
            if path.is_file() and path.suffix in {'.js', '.css', '.png', '.ico', '.svg'}
            and not str(path.relative_to(ROOT)).startswith(RUNTIME_GENERATED_DIRS)
        )
        self.assertTrue(assets, 'web/static 扫描到 0 个资产，检查器可能失效')

        # 引用面：仓库内所有 HTML/JS/PY/MD 文本（feature page.html 里的
        # script src 也算）。以文件名匹配即可：本仓库资产文件名全局唯一，
        # 名字匹配已足够防"删了引用留文件"的死资源；误报治理优于查全率。
        haystacks: list[str] = []
        for base in ('web', 'features', 'bootstrap', 'docs', 'tests', 'agent'):
            for path in (ROOT / base).rglob('*'):
                if path.is_file() and path.suffix in {'.html', '.js', '.py', '.md', '.css'}:
                    haystacks.append(path.read_text(encoding='utf-8', errors='replace'))

        offenders = []
        for asset in assets:
            relative = str(asset.relative_to(ROOT))
            if relative in UNREFERENCED_ASSET_ALLOWLIST:
                continue
            if not any(asset.name in text for text in haystacks):
                offenders.append(relative)
        self.assertEqual(offenders, [])


    def test_literal_script_and_stylesheet_targets_exist(self):
        # Reverse check: removing an asset must not leave a stale browser entrypoint.
        pattern = re.compile(
            r"""(?:src|href)\s*=\s*["'](/static/[A-Za-z0-9/_.-]+\.(?:js|css))(?:[?#][^"']*)?["']"""
        )
        offenders = []
        for base in ('web/shell', 'web/templates', 'features'):
            for page in (ROOT / base).rglob('*.html'):
                source = page.read_text(encoding='utf-8')
                for url in pattern.findall(source):
                    if not (ROOT / 'web' / url.lstrip('/')).is_file():
                        offenders.append(f'{page.relative_to(ROOT)} -> {url}')
        self.assertEqual(offenders, [])


class DocsLinkTests(unittest.TestCase):
    """docs/ Markdown 的仓库相对链接必须可解析。"""

    _LINK_RE = re.compile(r'\[[^\]]*\]\(([^)]+)\)')

    def test_relative_doc_links_resolve(self):
        offenders = []
        # Top-level onboarding and developer guides are user-facing too;
        # do not leave broken links in README while docs/ remains green.
        doc_files = [
            *DOCS.rglob('*.md'),
            ROOT / 'README.md',
            ROOT / 'AGENTS.md',
            ROOT / 'tools' / 'README.md',
        ]
        self.assertTrue(doc_files, '文档扫描到 0 个文件，检查器可能失效')
        for doc in doc_files:
            text = doc.read_text(encoding='utf-8', errors='replace')
            for match in self._LINK_RE.finditer(text):
                target = match.group(1).split('#', 1)[0].strip()
                if not target or target.startswith(_EXTERNAL_SCHEMES):
                    continue
                if target.startswith('<'):  # 占位符链接
                    continue
                resolved = (doc.parent / target).resolve()
                if not resolved.exists():
                    offenders.append(f'{doc.relative_to(ROOT)} -> {target}')
        self.assertEqual(offenders, [])


class RouteDocsContractTests(unittest.TestCase):
    """新增 /api 路由前缀必须进入 routes.json 契约。"""

    def test_route_snapshot_covers_registered_api_prefixes(self):
        snapshot = ROOT / 'tests' / 'contract' / 'snapshots' / 'routes.json'
        if not snapshot.is_file():
            self.fail('routes.json snapshot 不存在，契约由 tests/contract 维护')
        import json

        snapshot_paths = {
            entry['path']
            for entry in json.loads(snapshot.read_text(encoding='utf-8'))
            if isinstance(entry, dict) and entry.get('path')
        }
        self.assertTrue(snapshot_paths, 'routes.json 为空，契约检查器可能失效')

        # 从路由模块源码静态提取 @router.get/post/... 的路径字面量。
        # 动态注册（add_api_route 变量路径）不在此检查范围。
        declared: set[str] = set()
        pattern = re.compile(
            r'@(?:router|page_router|\w+_router)\.(?:get|post|put|delete|patch)\(\s*[\'"]([^\'"]+)[\'"]'
        )
        for path in ROOT.glob('features/**/*.py'):
            if '/tests/' in str(path):
                continue
            declared.update(pattern.findall(path.read_text(encoding='utf-8', errors='replace')))
        self.assertTrue(declared, 'features 路由提取为 0 条，检查器可能失效')

        offenders = sorted(
            route for route in declared
            if route.startswith('/api') and route not in snapshot_paths
        )
        self.assertEqual(
            offenders, [],
            '以下路由未进入 tests/contract/snapshots/routes.json，'
            '请运行 tests/contract 的快照生成流程更新契约',
        )


if __name__ == '__main__':
    unittest.main()


def test_inventory_checks_all_hand_maintained_python_entrypoints(tmp_path, monkeypatch):
    from tools.scripts.maintenance import audit_repository_inventory as audit

    files = ['app.py', 'conftest.py', 'plugins/codesearch/scripts/mcp_server.py',
             'tools/scripts/maintenance/repair_audit_chain.py', 'features/example.py',
             'plugins/gms-remote-test/generated.py']
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('def invalid(:\n', encoding='utf-8')
    sprite = tmp_path / 'ui.svg'
    sprite.write_text('<svg/>', encoding='utf-8')
    monkeypatch.setattr(audit, 'ROOT', tmp_path)
    monkeypatch.setattr(audit, 'SPRITE', sprite)
    monkeypatch.setattr(audit, 'tracked_files', lambda: files)
    result = audit.examine()
    assert result['summary']['critical'] == 5
    assert {item['path'] for item in result['findings']} == set(files[:-1])


def test_inventory_treats_deleted_documentation_targets_as_critical(tmp_path, monkeypatch):
    from tools.scripts.maintenance import audit_repository_inventory as audit

    doc = tmp_path / 'docs' / 'architecture.md'
    doc.parent.mkdir()
    doc.write_text('[removed](../features/removed.py)\n[external](https://example.com)\n', encoding='utf-8')
    sprite = tmp_path / 'ui.svg'
    sprite.write_text('<svg/>', encoding='utf-8')
    monkeypatch.setattr(audit, 'ROOT', tmp_path)
    monkeypatch.setattr(audit, 'SPRITE', sprite)
    monkeypatch.setattr(audit, 'tracked_files', lambda: ['docs/architecture.md'])
    result = audit.examine()
    assert result['summary']['critical'] == 1
    assert result['findings'] == [{'kind': 'broken_doc_link', 'path': 'docs/architecture.md',
                                   'target': '../features/removed.py'}]


def test_inventory_detects_removed_absolute_and_relative_imports(tmp_path, monkeypatch):
    from tools.scripts.maintenance import audit_repository_inventory as audit

    files = {
        'features/example/__init__.py': 'EXPORTED = 1\n',
        'features/example/valid.py': 'VALUE = 1\n',
        'features/example/consumer.py': (
            'from .valid import VALUE\n'
            'from .valid import RemovedSymbol\n'
            'from .removed import OldClass\n'
            'def deferred():\n'
            '    import foundation.removed\n'
            'from . import EXPORTED\n'
            'from foundation import missing_member\n'
            'import importlib as loader\n'
            'loader.import_module("features.example.missing_dynamic")\n'
            'loader.import_module(".missing_relative", __package__)\n'
            'loader.import_module(name=".missing_keyword", package="features.example")\n'
            'loader.import_module(name="features.example.missing_absolute_keyword")\n'
            'loader.import_module(".valid", __package__)\n'
            'import external_dependency\n'
        ),
        'foundation/__init__.py': '',
    }
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding='utf-8')
    sprite = tmp_path / 'ui.svg'
    sprite.write_text('<svg/>', encoding='utf-8')
    monkeypatch.setattr(audit, 'ROOT', tmp_path)
    monkeypatch.setattr(audit, 'SPRITE', sprite)
    monkeypatch.setattr(audit, 'tracked_files', lambda: sorted(files))
    result = audit.examine()
    assert result['summary']['critical'] == 8
    assert {item['target'] for item in result['findings']} == {
        'features.example.removed', 'foundation.removed', 'features.example.valid.RemovedSymbol',
        'foundation.missing_member', 'features.example.missing_dynamic',
        'features.example.missing_relative', 'features.example.missing_keyword',
        'features.example.missing_absolute_keyword',
    }


def test_inventory_enforces_shared_source_line_limit(tmp_path, monkeypatch):
    from tools.scripts.maintenance import audit_repository_inventory as audit

    name = "features/example.py"
    path = tmp_path / name
    path.parent.mkdir()
    path.write_text("# source\n" * (audit.MAX_SOURCE_LINES + 1), encoding="utf-8")
    sprite = tmp_path / "ui.svg"
    sprite.write_text("<svg/>", encoding="utf-8")
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    monkeypatch.setattr(audit, "SPRITE", sprite)
    monkeypatch.setattr(audit, "tracked_files", lambda: [name])
    result = audit.examine()
    assert result["summary"]["critical"] == 1
    assert result["source_layout"]["over_limit"][0]["path"] == name


def test_source_budget_scans_extended_first_party_formats(tmp_path, monkeypatch):
    from tests.architecture import _size_ratchet as sizes
    from tools.scripts.maintenance import audit_repository_inventory as audit

    files = ["features/example.kt", "features/example.SQL",
             "web/static/js/example.tsx", "web/static/js/example.jsx",
             "web/static/vendor/bundle.tsx"]
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// source\n" * (sizes.MAX_SOURCE_LINES + 1), encoding="utf-8")
    sprite = tmp_path / "ui.svg"
    sprite.write_text("<svg/>", encoding="utf-8")
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    monkeypatch.setattr(audit, "SPRITE", sprite)
    monkeypatch.setattr(audit, "tracked_files", lambda: files)
    result = audit.examine()
    assert {item["path"] for item in result["source_layout"]["over_limit"]} == set(files[:-1])

    from types import SimpleNamespace

    monkeypatch.setattr(sizes, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sizes.subprocess, "run",
                        lambda *args, **kwargs: SimpleNamespace(stdout=("\0".join(files) + "\0").encode()))
    assert {path.relative_to(tmp_path).as_posix() for path in sizes.tracked_source_paths(
        sizes.BACKEND_SOURCE_SUFFIXES | sizes.FRONTEND_SOURCE_SUFFIXES
    )} == set(files[:-1])
