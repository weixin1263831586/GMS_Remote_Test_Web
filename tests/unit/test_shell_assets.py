"""The bundled Shell keeps dependency order and normal static HTTP semantics."""

import hashlib
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
        self.feed(source)

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "script" and "src" in attributes:
            self.sources.append(attributes["src"])
        if tag == "link" and attributes.get("rel") == "stylesheet":
            self.styles.append(attributes["href"])


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
    end = next(index for index, src in enumerate(old_scripts) if "/shell-late-init.js" in src) + 1
    asset = templates.runtime_bundle.asset
    bundled_url = f"/static/{RUNTIME_PATH}?v={asset.digest}"
    assert end - start == 14
    assert new_scripts == [*old_scripts[:start], bundled_url, *old_scripts[end:]]
    assert len(new_scripts) == len(old_scripts) - 13
    assert [path.name for path, *_ in asset.sources] == [Path(src.split("?")[0]).name for src in old_scripts[start:end]]
    assert asset.digest == hashlib.sha256(asset.content).hexdigest()
    old_styles = ScriptSources(old_source).styles
    new_styles = ScriptSources(source).styles
    styles = templates.style_bundle.asset
    assert new_styles == [f"/static/{STYLES_PATH}?v={styles.digest}", *old_styles[4:]]
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
