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
    # 由外部（用户浏览器书签/Controller /api/agent/install 直接返回）
    # 使用，不在仓库 HTML/JS 中出现。
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


class DocsLinkTests(unittest.TestCase):
    """docs/ Markdown 的仓库相对链接必须可解析。"""

    _LINK_RE = re.compile(r'\[[^\]]*\]\(([^)]+)\)')

    def test_relative_doc_links_resolve(self):
        offenders = []
        doc_files = list(DOCS.rglob('*.md'))
        self.assertTrue(doc_files, 'docs/ 扫描到 0 个文件，检查器可能失效')
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
            self.skipTest('routes.json snapshot 不存在，契约由 tests/contract 维护')
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
