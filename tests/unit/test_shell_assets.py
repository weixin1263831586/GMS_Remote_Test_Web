"""The bundled Shell keeps dependency order and normal static HTTP semantics."""

import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from bootstrap.shell_assets import RUNTIME_PATH, STYLES_PATH, ShellAsset, ShellStaticFiles
from bootstrap.shell_templates import create_shell_templates


ROOT = Path(__file__).resolve().parents[2]


class ScriptSources(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.sources = []
        self.styles = []
        self.bundled_styles = []
        self.feed(source)

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "script" and "src" in attributes:
            self.sources.append(attributes["src"])
        if tag == "link" and attributes.get("rel") == "stylesheet":
            self.styles.append(attributes["href"])
            if "data-shell-styles" in attributes:
                self.bundled_styles.append(attributes["href"])


def runtime_fixture(tmp_path, *, production=False, between=""):
    directory = tmp_path / "shell"
    directory.mkdir()
    static = tmp_path / "static/js/shell"
    static.mkdir(parents=True)
    (static / "shell-first.js").write_text("let value = 1 // trailing comment")
    (static / "shell-second.js").write_text("window.result = value + 2;")
    (directory / "shell.html").write_text(
        '<script src="/static/js/before.js" defer></script>\n'
        '<script src="/static/js/shell/shell-first.js?v=old" defer data-shell-runtime></script>\n'
        + between +
        '<script src="/static/js/shell/shell-second.js" defer data-shell-runtime></script>\n'
        '<script src="/static/js/after.js" defer></script>{{ owner }}'
    )
    return create_shell_templates(directory, production=production), static


def test_real_shell_replaces_only_consecutive_runtime_scripts():
    templates = create_shell_templates(ROOT / "web/shell", production=True)
    source = templates.env.loader.get_source(templates.env, "shell.html")[0]
    originals = create_shell_templates(ROOT / "web/shell", production=True, bundle_assets=False)
    old_source = originals.env.loader.get_source(originals.env, "shell.html")[0]
    old_scripts = ScriptSources(old_source).sources
    new_scripts = ScriptSources(source).sources
    start = next(index for index, src in enumerate(old_scripts) if "/shell-client-identity.js" in src)
    end = next(index for index, src in enumerate(old_scripts) if "/shell-main.js" in src) + 1
    asset = templates.runtime_bundle.asset
    assert end - start == 13
    # 非 bundle 资源保持原始路径，但 ?v= 统一被 ShellAssetVersioner 重写为
    # 内容摘要（旧手工日期标签不再参与），因此断言仅比较 path 部分。
    expected_paths = [src.split("?")[0] for src in old_scripts]
    expected_paths[start:end] = [f"/static/{RUNTIME_PATH}"]  # bundle 合并为一条
    actual_paths = [src.split("?")[0] for src in new_scripts]
    assert actual_paths == expected_paths
    assert len(new_scripts) == len(old_scripts) - (end - start - 1)
    # 所有 URL 的 ?v= 均为 12 位内容摘要（手工日期标签已消灭）。
    for src in new_scripts:
        version = src.split("?v=")[-1]
        assert re.fullmatch(r"[0-9a-f]{12}|[0-9a-f]{64}", version), src
    assert [path.name for path, *_ in asset.sources] == [Path(src.split("?")[0]).name for src in old_scripts[start:end]]
    assert asset.digest == hashlib.sha256(asset.content).hexdigest()
    old_styles = ScriptSources(old_source).styles
    new_styles = ScriptSources(source).styles
    styles = templates.style_bundle.asset
    bundled = ScriptSources(old_source).bundled_styles
    start = old_styles.index(bundled[0])
    assert old_styles[start:start + len(bundled)] == bundled
    # 样式同样只比较路径（?v= 已由 ShellAssetVersioner 统一改为内容摘要）。
    expected_style_paths = [href.split("?")[0] for href in old_styles]
    expected_style_paths[start:start + len(bundled)] = [f"/static/{STYLES_PATH}"]
    assert [href.split("?")[0] for href in new_styles] == expected_style_paths
    assert b"@import" not in styles.content
    assert {"common.css", "common-components.css", "common-layout.css", "common-page.css",
            "apk-analysis.css", "form-controls.css"} <= {path.name for path, *_ in styles.sources}


def test_cached_asset_preserves_get_head_etag_and_other_static_files(tmp_path):
    templates, static = runtime_fixture(tmp_path, production=True)
    (static / "other.js").write_text("other")
    asset = templates.runtime_bundle.asset
    assert b"// trailing comment\n;\n// Source:" in asset.content
    app = Starlette(routes=[Mount("/", app=ShellStaticFiles(
        directory=tmp_path / "static", runtime_bundle=templates.runtime_bundle,
    ))])
    with TestClient(app) as client:
        first = client.get(f"/{RUNTIME_PATH}")
        assert first.status_code == 200
        assert first.content == asset.content
        assert first.headers["content-type"].startswith("text/javascript")
        with patch.object(ShellAsset, "is_current", side_effect=AssertionError("request checked source files")), \
                patch.object(Path, "read_text", side_effect=AssertionError("request read source files")):
            html = templates.env.get_template("shell.html").render(owner="alice")
            assert html.endswith("alice")
            head = client.head(f"/{RUNTIME_PATH}")
            assert head.status_code == 200 and head.content == b""
            assert head.headers["content-length"] == str(len(asset.content))
            unchanged = client.get(f"/{RUNTIME_PATH}", headers={"If-None-Match": f'W/{first.headers["etag"]}'})
            assert unchanged.status_code == 304 and unchanged.content == b""
            assert unchanged.headers["etag"] == first.headers["etag"]
        assert client.post(f"/{RUNTIME_PATH}").status_code == 405
        assert client.get("/js/shell/other.js").text == "other"


def test_development_script_edits_refresh_content_and_version(tmp_path):
    templates, static = runtime_fixture(tmp_path)
    first = templates.env.get_template("shell.html").render(owner="alice")
    original = templates.runtime_bundle.asset
    (static / "shell-second.js").write_text("window.result = value + 100;")
    second = templates.env.get_template("shell.html").render(owner="bob")
    updated = templates.runtime_bundle.asset
    assert original.digest != updated.digest
    assert original.digest in first and updated.digest in second
    assert b"value + 100" in updated.content
    assert second.endswith("bob")


def test_runtime_scripts_cannot_move_across_another_dependency(tmp_path):
    with pytest.raises(ValueError, match="consecutive"):
        runtime_fixture(tmp_path, between='<script src="/static/js/dependency.js" defer></script>')


def test_static_asset_urls_are_versioned_by_content_digest(tmp_path):
    from bootstrap.shell_assets import ShellAssetVersioner

    static = tmp_path / "static"
    (static / "js").mkdir(parents=True)
    (static / "js" / "widget.js").write_text("window.w = 1;")
    page = tmp_path / "page.html"
    page.write_text(
        '<script src="/static/js/widget.js?v=20260101-old" defer></script>\n'
        '<script src="/static/js/missing.js?v=old" defer></script>\n'
    )
    versioner = ShellAssetVersioner(static)
    rewritten = versioner.rewrite(page.read_text())

    # 已存在的资源：手工日期标签被内容摘要替换。
    match = re.search(r'widget\.js\?v=([0-9a-f]{12})', rewritten)
    assert match, rewritten
    # 文件缺失的资源保持原样（不做失败即崩）。
    assert "missing.js?v=old" in rewritten

    # 内容变化 → 摘要变化（浏览器缓存自动失效，无需人工 bump ?v=）。
    (static / "js" / "widget.js").write_text("window.w = 2;")
    assert versioner.rewrite(page.read_text()) != rewritten

    # 开发模式判定：文件再次变动后 is_current 必须为 False，触发模板刷新。
    # （stat 检查依赖 size/mtime，用不同长度内容避免 fs 时间戳粒度问题。）
    assert versioner.is_current()
    (static / "js" / "widget.js").write_text("window.w = 333;")
    assert not versioner.is_current()


def test_api_only_application_does_not_require_shell_files(tmp_path):
    templates = create_shell_templates(tmp_path / "shell", production=True)
    assert templates.runtime_bundle is None


def style_fixture(tmp_path, *, imports='@import url("/static/css/part.css?v=old");', production=False):
    directory = tmp_path / "shell"
    directory.mkdir()
    static = tmp_path / "static/css"
    static.mkdir(parents=True)
    (static / "common.css").write_text(imports + '\n.main { background: url("icon.svg"); }')
    (static / "part.css").write_text(".part { color: blue; }")
    (static / "tail.css").write_text(".tail { color: green; }")
    (directory / "shell.html").write_text(
        '<link rel="stylesheet" href="/static/css/common.css?v=old" data-shell-styles>\n'
        '<link rel="stylesheet" href="/static/css/tail.css" data-shell-styles>\n'
        '<link rel="stylesheet" href="/static/vendor/other.css">'
    )
    return create_shell_templates(directory, production=production), static


def test_styles_preserve_import_cascade_relative_urls_and_cached_http(tmp_path):
    templates, _ = style_fixture(tmp_path, production=True)
    asset = templates.style_bundle.asset
    assert asset.content.index(b".part") < asset.content.index(b".main") < asset.content.index(b".tail")
    assert b'url("icon.svg")' in asset.content
    assert b"@import" not in asset.content
    html = templates.env.get_template("shell.html").render()
    assert html.count('rel="stylesheet"') == 2
    assert html.index(STYLES_PATH) < html.index("/static/vendor/other.css")
    app = Starlette(routes=[Mount("/", app=ShellStaticFiles(
        directory=tmp_path / "static", style_bundle=templates.style_bundle,
    ))])
    with TestClient(app) as client:
        with patch.object(ShellAsset, "is_current", side_effect=AssertionError("request checked styles")), \
                patch.object(Path, "read_text", side_effect=AssertionError("request read styles")):
            html = templates.env.get_template("shell.html").render()
            response = client.get(f"/{STYLES_PATH}")
        assert response.content == asset.content
        assert response.headers["content-type"].startswith("text/css")
        assert client.head(f"/{STYLES_PATH}").headers["content-length"] == str(len(asset.content))
        assert client.get(f"/{STYLES_PATH}", headers={"If-None-Match": response.headers["etag"]}).status_code == 304


def test_canonical_nested_typography_imports_bundle_in_production(tmp_path):
    _, static = style_fixture(tmp_path, imports="", production=False)
    (static / "typography").mkdir()
    (static / "typography/part.css").write_text(".role { font-size: 13px; }")
    (static / "common.css").write_text(
        '@import url("/static/css/typography/part.css?v=canonical");\n'
        '.main { background: url("icon.svg"); }'
    )
    # Build after the nested dependency exists, as in the real repository.
    templates = create_shell_templates(tmp_path / "shell", production=True)
    asset = templates.style_bundle.asset
    assert asset is not None
    assert b".role { font-size: 13px; }" in asset.content
    assert b"@import" not in asset.content
    assert any(path.as_posix().endswith("/typography/part.css") for path, *_ in asset.sources)


def test_development_import_edit_refreshes_style_version(tmp_path):
    templates, static = style_fixture(tmp_path)
    first = templates.env.get_template("shell.html").render()
    original = templates.style_bundle.asset
    (static / "part.css").write_text(".part { color: orange; }")
    second = templates.env.get_template("shell.html").render()
    updated = templates.style_bundle.asset
    assert original.digest != updated.digest
    assert original.digest in first and updated.digest in second
    assert b"color: orange" in updated.content


@pytest.mark.parametrize("imports", [
    '@import url("/static/css/part.css") screen;',
    '@import url("/static/css/part.css") layer(example);',
    '@import url("https://example.test/theme.css");',
    '@media screen { @import url("/static/css/part.css"); }',
    '/* @import url("/static/css/part.css"); */',
    '@namespace example url("http://example.test");',
])
def test_scoped_external_or_commented_imports_keep_browser_semantics(tmp_path, imports):
    templates, _ = style_fixture(tmp_path, imports=imports)
    assert templates.style_bundle.asset is None
    assert STYLES_PATH not in templates.env.get_template("shell.html").render()
