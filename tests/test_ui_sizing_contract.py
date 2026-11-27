"""UI geometry entrypoint and no-wrap guarantees across page roots."""

import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "web/static/css"
ENTRYPOINTS = (
    "web/shell/components/head.html",
    "web/templates/architecture.html",
    "features/automation/ui/page.html",
    "features/cluster/ui/page.html",
    "features/devices/ui/page.html",
    "features/gerrit/ui/page.html",
    "features/redmine/ui/page.html",
    "features/gms/ui/page.html",
    "features/gms/radar/ui/page.html",
    "features/devices/deployment/api.py",
    "features/devices/debug/api.py",
)


# Geometry belongs to the shared table template, not to page-level rules.
_TABLE_SELECTOR = re.compile(
    r"(?:^|[\s>,+~:(])(?:th|td|tr|thead|tbody)(?:$|[\s.#:\[>,+~)])"
    r"|suite-file-(?:row|table-head)\b|\.dcfg-(?:th|td)\b",
    re.I,
)
_GEOMETRY = re.compile(
    r"(?:^|[;\s])(?:min-height|max-height|height|padding(?:-[\w-]+)?|"
    r"vertical-align|box-sizing)\s*:", re.I,
)
_TABLE_TOKEN = re.compile(r"--ui-table-[\w-]+\s*:")
_CELL_TAG = re.compile(
    r"""<(?:th|td|tr|thead|tbody)\b[^>]*?\bstyle\s*=\s*(["'])(.*?)\1""",
    re.I | re.S,
)
_CELL_DOM = re.compile(
    r"\b(?:td|th|tr|cell|row|thead|tbody)\w*\.style\."
    r"(?:padding\w*|(?:min|max)?Height|height|verticalAlign|boxSizing)\s*=",
    re.I,
)
# Explicit non-data-table exceptions; new exceptions require a named reason.
_SPECIAL_TABLE_RULES = {
    "features/redmine/ui/page.css": {
        ".analysis-doc .md-table th, .analysis-doc .md-table td",  # Markdown prose
    },
    "web/static/css/common.css": {
        ".input-table td", ".input-table td:first-child", ".input-table td:last-child",
    },
    "web/static/css/common-page.css": {".input-table td"},  # responsive form
    "web/static/css/other-workspaces.css": {
        "#page-test.page-ui-rework .input-table td",
        "#page-test.page-ui-rework .input-table td:last-child",
    },
    "web/shell/components/head.html": {".project-guide-map th, .project-guide-map td"},
    # These selectors target interactive controls, not cells. Touch sizes differ.
    "features/cluster/ui/page.html": {"td button,td select,td input"},
}
_EXTERNAL_MAIL_STYLES = {
    "web/static/js/pages/api-docs.js": {"padding:4px 12px 4px 0;color:#666;", "padding:4px 0;"},
    "features/gms/radar/notifications.py": {
        "padding:10px 11px;text-align:{align};'\n"
        "        'background:#f3f5f9;color:#495465;border-bottom:1px solid #dce1e9;'\n"
        "        f'font-weight:600;",
    },
}


def _tracked_ui_sources():
    """Enumerate repository sources, including new files, not a fixed page list."""
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True,
    )
    for name in result.stdout.decode("utf-8").split("\0"):
        path = Path(name)
        if not name or path.suffix not in {".css", ".html", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte", ".py"}:
            continue
        if "vendor" in path.parts or "node_modules" in path.parts:
            continue
        if "tests" in path.parts or path.name.startswith("test_"):
            continue
        yield path


def _table_geometry_violations(path, source):
    if path == "web/static/css/ui-sizing.css":
        return []
    errors = []
    if path.endswith(".css"):
        styles = [source]
    else:
        styles = re.findall(r"<style\b[^>]*>(.*?)</style\s*>", source, re.I | re.S)
    for css in styles:
        # Python HTML templates may escape braces for f-strings.
        if path.endswith(".py"):
            css = css.replace("{{", "{").replace("}}", "}")
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        if _TABLE_TOKEN.search(css):
            errors.append("local --ui-table-* definition")
        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
            selector = " ".join(match[1].split())
            if not _TABLE_SELECTOR.search(selector) or not _GEOMETRY.search(match[2]):
                continue
            if selector in _SPECIAL_TABLE_RULES.get(path, set()):
                continue
            errors.append(f"local cell geometry: {selector}")
    if not path.endswith(".css"):
        for match in _CELL_TAG.finditer(source):
            if not _GEOMETRY.search(match[2]):
                continue
            if match[2] in _EXTERNAL_MAIL_STYLES.get(path, set()):
                continue
            line = source.count("\n", 0, match.start()) + 1
            errors.append(f"line {line}: inline table cell geometry")
        for match in _CELL_DOM.finditer(source):
            line = source.count("\n", 0, match.start()) + 1
            errors.append(f"line {line}: {match[0]}")
    return errors


class UiSizingContractTests(unittest.TestCase):
    def test_entrypoint_loaded_exactly_once_everywhere(self):
        for path in ENTRYPOINTS:
            with self.subTest(path=path):
                source = (ROOT / path).read_text(encoding="utf-8")
                self.assertEqual(source.count("/static/css/ui-sizing.css"), 1)
                self.assertEqual(source.count("/static/css/typography.css"), 1)

    def test_geometry_cache_version_is_consistent_between_ui_roots(self):
        # Independent pages are not always served through Shell's asset hasher.
        # A single shared cache version prevents old CSS from missing new roles.
        import re

        versions = {}
        for path in ENTRYPOINTS:
            source = (ROOT / path).read_text(encoding="utf-8")
            matches = re.findall(r'/static/css/ui-sizing\.css\?v=([a-zA-Z0-9_-]+)', source)
            with self.subTest(path=path):
                self.assertEqual(len(matches), 1)
            versions[path] = matches[0]
        self.assertEqual(len(set(versions.values())), 1, versions)

    def test_redundant_module_height_aliases_are_not_reintroduced(self):
        """Directly consume shared height roles instead of aliasing identical values."""
        legacy = (
            "--analysis-control-height",
            "--analysis-summary-row-height",
            "--analysis-tab-height",
            "--tech-control-height",
            "--workspace-tab-height",
        )
        styles = (
            "analysis-workbench-tokens.css",
            "apk-workbench.css",
            "report-workbench.css",
            "modal-tech-dialogs.css",
            "modal-tech-cases.css",
            "workspace-tabs.css",
        )
        for name in styles:
            with self.subTest(name=name):
                source = (CSS / name).read_text(encoding="utf-8")
                for alias in legacy:
                    self.assertNotIn(alias, source)
        page = (CSS / "common-page.css").read_text(encoding="utf-8")
        self.assertNotIn("#kb-doc-tags { height: 26px; }", page)
        self.assertIn("#suite-download-url { width: 100%; }", page)
        for name in ("agent.html", "notes.html", "test-suites.html"):
            markup = (ROOT / "web/shell/pages" / name).read_text(encoding="utf-8")
            self.assertIn("ui-workspace-toolbar", markup)
            self.assertIn("ui-workspace-control", markup)
        layout = (CSS / "common-layout.css").read_text(encoding="utf-8")
        self.assertIn(".btn-act { min-height: var(--ui-control-height-dense);", layout)

    def test_stacked_workspace_uses_one_panel_template(self):
        layout = (CSS / "ui-foundations.css").read_text(encoding="utf-8")
        self.assertRegex(layout, r"\.ui-workspace-stack:not\(\[hidden\]\)\s*\{[^}]*gap:\s*var\(--ui-spacing-sm\)")
        self.assertRegex(layout, r"\.ui-panel-frame,[^{}]*\.ui-workspace-panel,[^{}]*\{[^}]*border-radius:\s*var\(--ui-radius-card\)")
        for name in ("notes.html", "test-suites.html"):
            markup = (ROOT / "web/shell/pages" / name).read_text(encoding="utf-8")
            for role in ("ui-workspace-stack", "ui-workspace-toolbar-panel", "ui-workspace-content",
                         "ui-workspace-panel", "ui-workspace-toolbar", "ui-workspace-control"):
                self.assertIn(role, markup, (name, role))
        for name in ("common-page.css", "other-workspaces.css"):
            source = (CSS / name).read_text(encoding="utf-8")
            self.assertNotRegex(source, r"\.suite-download-section\s*\{")
            self.assertNotIn(".kb-external-layout", source)

    def test_workspace_sidebar_roles(self):
        for filename, selector in (
            ("workspace-panels.css", ".agent-layout.agent-layout-with-sessions"),
            ("common.css", ".kb-layout"),
            ("common-layout.css", ".suite-browser-layout"),
        ):
            css = (CSS / filename).read_text(encoding="utf-8")
            rule = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
            self.assertIsNotNone(rule, selector)
            self.assertIn("grid-template-columns: var(--ui-workspace-navigation-width)", rule[1])
        for name, selector in (("api-docs.html", "api-body-grid"), ("security-audit.html", "audit-content-grid")):
            markup = (ROOT / "web/shell/pages" / name).read_text(encoding="utf-8")
            self.assertRegex(markup, rf'class="{selector} ui-workspace-grid ui-workspace-grid--aside"')
        for name, selector in (("api-docs-workbench.css", ".api-body-grid"), ("other-workspaces.css", ".audit-content-grid")):
            css = (CSS / name).read_text(encoding="utf-8")
            self.assertNotRegex(css, re.escape(selector) + r"\s*\{[^}]*grid-template-columns")

    def test_console_uses_shared_auxiliary_column(self):
        markup = (ROOT / "features/devices/ui/page.js").read_text(encoding="utf-8")
        self.assertIn('class="console-layout ui-workspace-grid ui-workspace-grid--aside ui-workspace-grid--aside-persistent"', markup)
        source = (ROOT / "features/devices/ui/page.css").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"\.console-layout\s*\{[^}]*grid-template-columns")

    def test_global_size_roles(self):
        data = (CSS / "ui-sizing.css").read_text(encoding="utf-8")
        for key, px in {
            "page-edge": 8,
            "control-height": 32,
            "control-height-dense": 28,
            "workspace-toolbar-height": 36,
            "tab-height": 42,
            "tab-height-narrow": 38,
            "workspace-navigation-width": 250,
            "workspace-aside-min": 305,
            "workspace-aside-max": 350,
            "control-height-small": 26,
            "segmented-inner-height": 24,
            "table-header-height": 38,
            "table-row-height": 36,
            "table-cell-padding-block": 2,
            "table-cell-padding-inline": 10,
            "stat-height": 95,
            "stat-height-compact": 70,
            "card-heading-height": 42,
            "modal-actions-min-height": 58,
        }.items():
            with self.subTest(key=key):
                self.assertRegex(data, rf"--ui-{key}:\s*{px}px\s*;")
        self.assertNotRegex(data, r"(?<![\w-])font(?:-size|-family|-weight)?\s*:")

    def test_perimeter_has_one_shared_template_and_no_business_selectors(self):
        source = (CSS / "ui-sizing.css").read_text(encoding="utf-8")
        self.assertEqual(source.count("--ui-page-edge: 8px;"), 1)
        self.assertRegex(
            source,
            r"\.ui-page-edge,\s*\.page-content\.container\s*\{[^}]*"
            r"padding:\s*var\(--ui-page-edge\);",
        )
        self.assertNotRegex(
            source, r"#page-|workspace-scope-|data-ui-type-root|\[data-page=",
        )
        shell = (CSS / "common.css").read_text(encoding="utf-8")
        container = shell.split(".container {", 1)[1].split("}", 1)[0]
        self.assertNotIn("padding:", container)
        for name in ("host-desktop-workspace.css", "host-terminal-workspace.css"):
            self.assertNotIn(
                "padding: 5px 8px 8px",
                (CSS / name).read_text(encoding="utf-8"),
            )

    def test_page_roots_opt_in_to_same_shared_perimeter(self):
        roots = {
            "web/shell/pages/desktop.html": 0,
            "web/shell/pages/terminal.html": 0,
            "web/templates/architecture.html": 1,
            "features/automation/ui/page.html": 1,
            "features/cluster/ui/page.html": 2,
            "features/devices/ui/page.html": 2,
            "features/gerrit/ui/page.html": 3,
            "features/redmine/ui/page.html": 7,
            "features/gms/ui/page.html": 2,
            "features/gms/radar/ui/page.html": 1,
            "features/devices/deployment/api.py": 1,
            "features/devices/debug/api.py": 1,
            "features/system/gms_assistant_proxy.py": 1,
        }
        # Desktop and terminal are Shell page-content.container roots, so
        # their shared padding comes from the template, not ui-page-edge
        # applied again (which would double the outer inset).
        for path, expected in roots.items():
            with self.subTest(path=path):
                source = (ROOT / path).read_text(encoding="utf-8")
                self.assertEqual(source.count("ui-page-edge"), expected)
        # Standalone page headers use the same horizontal perimeter token.
        tabs = (CSS / "workspace-tabs.css").read_text(encoding="utf-8")
        self.assertIn("padding: 0 var(--ui-page-edge);", tabs)
        self.assertIn("--workspace-tab-width: 110px;", tabs)
        self.assertNotIn("--workspace-header-inset:", tabs)

    def test_distribution_charts_use_shared_component_geometry(self):
        foundation = (CSS / "ui-foundations.css").read_text(encoding="utf-8")
        for role in ("ui-panel-heading", "ui-distribution-chart", "ui-distribution-ring",
                     "ui-distribution-center", "ui-distribution-legend"):
            self.assertIn(f".{role}", foundation)
            for page in ("api-docs.html", "security-audit.html"):
                self.assertIn(role, (ROOT / "web/shell/pages" / page).read_text(encoding="utf-8"))
        for filename, selector in (("api-docs-workbench.css", "api-method-donut"),
                                   ("other-workspaces.css", "audit-source-donut")):
            source = (CSS / filename).read_text(encoding="utf-8")
            for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", source):
                if selector in rule[1]:
                    self.assertNotRegex(rule[2], r"(?:^|[;\s])(?:width|height|flex(?:-basis)?)\s*:")

    def test_shared_kpi_and_pagination_roles(self):
        foundation = (CSS / "ui-foundations.css").read_text(encoding="utf-8")
        self.assertIn(".ui-kpi-card--icon", foundation)
        self.assertIn(".ui-dashboard .stat-card", foundation)
        self.assertIn(".ui-kpi-stack .stat-card", foundation)
        self.assertIn(".ui-pagination button", foundation)
        self.assertIn(".ui-table-actions", foundation)
        for name in ("redmine", "gerrit"):
            page = (ROOT / "features" / name / "ui/page.html").read_text(encoding="utf-8")
            self.assertIn("ui-dashboard", page)
            self.assertIn("/static/css/ui-foundations.css", page)
            self.assertIn("/static/css/form-controls.css", page)
        for name in ("devices.html", "api-docs.html"):
            markup = (ROOT / "web/shell/pages" / name).read_text(encoding="utf-8")
            self.assertIn("ui-pagination-footer", markup)
        for path in ("features/cluster/ui/page.html", "features/cluster/ui/dash-render.js"):
            source = (ROOT / path).read_text(encoding="utf-8")
            self.assertIn("ui-kpi-card--icon", source)
            self.assertIn("ui-kpi-icon", source)

    def test_shared_templates_consume_tokens(self):
        mapping = {
            "common.css": ("--ui-control-height-small", "--ui-stat-height"),
            "common-components.css": ("--ui-control-height-dense", "--ui-control-height-large"),
            "ui-foundations.css": ("--ui-stat-height", "--ui-control-height-dense"),
            "device-inventory.css": ("--ui-control-height",),
            "form-controls.css": ("--ui-control-height", "--ui-control-height-dense"),
            "analysis-workbench-tokens.css": ("--ui-radius-card",),
            "modal-tech-dialogs.css": ("--ui-modal-actions-min-height",),
            "data-table-page.css": ("--ui-control-height",),
            "ui-sizing.css": ("--ui-table-header-height", "--ui-table-row-height", "--ui-table-cell-padding-block", "--ui-table-cell-padding-inline"),
        }
        for name, roles in mapping.items():
            source = (CSS / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                for role in roles:
                    self.assertIn(f"var({role})", source)


    def test_embedded_and_complex_surfaces_use_shared_geometry_roles(self):
        role_map = {
            "web/static/css/workspace-tabs.css": ("--ui-tab-height",),
            "web/static/css/analysis-workbench-tokens.css": (
                "--ui-radius-card",
            ),
            "web/static/css/apk-workbench.css": (
                "--ui-tab-height", "--ui-tab-height-narrow", "--ui-control-height-dense",
            ),
            "web/static/css/common-layout.css": ("--ui-control-height-icon",),
            "web/static/css/agent-access.css": ("--ui-control-height-dialog",),
            "web/static/css/modal-tech-dialogs.css": (
                "--ui-modal-header-height-dense", "--ui-modal-close-size",
            ),
            "web/static/css/modal-tech-cases.css": (
                "--ui-control-height-dialog", "--ui-control-height-dialog-field",
            ),
            "web/static/css/vpn-proxy-panel.css": ("--ui-control-height-dense",),
            "web/static/css/api-docs-workbench.css": (
                "--ui-control-height-small", "--ui-control-height-dense",
            ),
            "features/redmine/ui/page.css": (
                "--ui-control-height-dense", "--ui-touch-target-height",
                "--ui-control-height-icon",
            ),
            "features/cluster/ui/page.html": (
                "--ui-touch-target-height",
            ),
            "features/gerrit/ui/page.html": ("--ui-control-height-icon",),
            "features/gms/radar/ui/page.html": ("--ui-control-height",),
        }
        for path, roles in role_map.items():
            with self.subTest(path=path):
                content = (ROOT / path).read_text(encoding="utf-8")
                for role in roles:
                    self.assertIn(f"var({role})", content)


    def test_aoa_micro_controls_keep_audited_height_roles(self):
        """AOA recorder rows must not become 32px buttons or cause 720p overflow."""
        token_source = (CSS / "ui-sizing.css").read_text(encoding="utf-8")
        aoa_source = (CSS / "aoa-workbench.css").read_text(encoding="utf-8")
        workspace_source = (CSS / "workspace-panels.css").read_text(encoding="utf-8")
        roles = {
            "aoa-command-button-height": 20,
            "aoa-command-editor-height": 22,
            "aoa-config-control-height": 27,
            "aoa-config-control-height-short": 30,
        }
        for name, px in roles.items():
            with self.subTest(role=name):
                self.assertRegex(token_source, rf"--ui-{name}:\s*{px}px\s*;")
        for role in roles:
            self.assertIn(f"var(--ui-{role})", aoa_source + workspace_source)
        self.assertIn("--ui-control-height", (CSS / "other-workspaces.css").read_text(encoding="utf-8"))
        redmine = (ROOT / "features/redmine/ui/page.css").read_text(encoding="utf-8")
        toolbar = re.search(r"\.daily-brief-toolbar \.ka-btn\s*\{([^}]*)\}", redmine)
        self.assertIsNotNone(toolbar)
        self.assertNotRegex(toolbar[1], r"(?:^|[;\s])(?:min-height|height|max-height)\s*:")
        self.assertNotIn("--ui-control-height-dialog-field", toolbar[1])

    def test_data_table_geometry_has_one_shared_template(self):
        source = (CSS / "ui-sizing.css").read_text(encoding="utf-8")
        for selector, role in (
            ("> thead > tr > :is(th, td)", "table-header-height"),
            ("> tbody > tr:not(:where(.group-row:not(.device-group-row))) > :is(td, th)", "table-row-height"),
            (".suite-file-table-head", "table-header-height"),
            (".suite-file-row", "table-row-height"),
        ):
            with self.subTest(selector=selector):
                rule = source.split(selector, 1)[1].split("{", 1)[1].split("}", 1)[0]
                self.assertIn("var(--ui-" + role + ")", rule)
        self.assertNotIn("--ui-table-header-height-dense", source)
        self.assertNotIn("--ui-table-row-height-dense", source)
        for name in (
            "ui-foundations.css", "other-workspaces.css", "device-inventory.css",
            "agent-access.css", "audit-disk.css", "api-docs-workbench.css",
            "data-table-page.css",
        ):
            with self.subTest(name=name):
                local = (CSS / name).read_text(encoding="utf-8")
                self.assertNotIn("var(--ui-table-header-height", local)
                self.assertNotIn("var(--ui-table-row-height", local)


    def test_no_local_geometry_in_remaining_data_table_renderers(self):
        paths = (
            "web/static/js/pages/test-reports.js",
            "web/static/js/pages/security-audit.js",
            "web/static/js/shell/shell-device-management.js",
            "web/static/js/pages/firmware-burn-share.js",
            "web/shell/dialogs/firmware-share.html",
            "web/static/js/shell/shell-device-groups.js",
            "web/static/js/pages/test-control.js",
            "web/static/js/pages/test-suite-browser-reports.js",
        )
        for path in paths:
            with self.subTest(path=path):
                source = (ROOT / path).read_text(encoding="utf-8")
                self.assertNotRegex(
                    source, r"""<(?:th|td|tr)\b[^>]*style=["'][^"']*(?:height|padding|vertical-align)\s*:"""
                )
        test_control = (ROOT / "web/static/js/pages/test-control.js").read_text(encoding="utf-8")
        self.assertNotRegex(test_control, r"\bcell\.style\.(?:padding\w*|height|verticalAlign)\s*=")
        suite = (ROOT / paths[-1]).read_text(encoding="utf-8")
        self.assertNotIn("style: 'padding:", suite)
        notes = (CSS / "other-workspaces.css").read_text(encoding="utf-8")
        rule = notes.split(".kb-source-table :is(th, td) {", 1)[1].split("}", 1)[0]
        self.assertNotIn("vertical-align", rule)
        proxy = (CSS / "vpn-proxy-panel.css").read_text(encoding="utf-8")
        rule = proxy.split(".tech-dialog--vpn-proxy th, .tech-dialog--vpn-proxy td {", 1)[1].split("}", 1)[0]
        self.assertNotIn("padding:", rule)
        architecture = (CSS / "architecture-scenes.css").read_text(encoding="utf-8")
        self.assertNotIn("height:34px;padding:7px 9px", architecture)
        scene = (ROOT / "web/static/js/pages/architecture-scenes.js").read_text(encoding="utf-8")
        self.assertIn('class="ui-detail-table"', scene)

    def test_detail_tables_are_explicit_and_data_tables_have_no_cell_overrides(self):
        redmine = (ROOT / "features/redmine/ui/page.css").read_text(encoding="utf-8")
        self.assertNotRegex(redmine, r"\.dept-table\s+td\.col-person\s*\{[^}]*padding")
        for selector in (".info-table th {", ".info-table td {"):
            rule = redmine.split(selector, 1)[1].split("}", 1)[0]
            self.assertNotIn("padding:", rule)
        issues = (ROOT / "features/redmine/ui/issues.js").read_text(encoding="utf-8")
        self.assertIn('class="info-table ui-detail-table"', issues)
        components = (CSS / "common-components.css").read_text(encoding="utf-8")
        self.assertNotIn(".dcfg-tab table:not(:has(> thead))", components)
        sizing = (CSS / "ui-sizing.css").read_text(encoding="utf-8")
        self.assertNotIn(".dcfg-tab", sizing)
        self.assertIn("table.ui-detail-table > tbody > tr > :is(th, td)", sizing)

    def test_aoa_command_grid_has_one_layout_owner(self):
        source = (CSS / "aoa-workbench.css").read_text(encoding="utf-8")
        self.assertRegex(source, r"grid-template-columns:\s*3ch minmax\(0, 1\.5fr\) minmax\(0, 1fr\) 60\.5px 156px;")
        self.assertIn("grid-column: 5; grid-row: 1;", source)
        self.assertIn("grid-column: 4 / 6;", source)
        for path in CSS.rglob("*.css"):
            if path.name == "aoa-workbench.css":
                continue
            css = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
            for selector, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
                if re.search(r"\.aoa-(?:command-list-header|command-row|command-actions|command-state|actions-label|command-list)\b", selector):
                    self.assertNotRegex(
                        declarations,
                        r"(?:^|[;\s])(?:grid-template-(?:columns|rows)|grid-column|grid-row|row-gap|white-space|overflow-wrap|padding(?:-top|-bottom)?)\s*:",
                        f"{path}: {selector}",
                    )

    def test_existing_single_line_layouts_not_replaced_by_wrapping(self):
        cases = {
            "common.css": ("white-space: nowrap;", ".page-header-actions"),
            "device-inventory.css": ("white-space: nowrap;", ".device-management-table"),
            "other-workspaces.css": ("white-space:nowrap", ".users-table"),
            "api-docs-workbench.css": ("white-space:nowrap", ".api-path"),
        }
        for name, patterns in cases.items():
            source = (CSS / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                for p in patterns:
                    self.assertIn(p, source)
        self.assertIn("overflow:auto!important", (CSS / "api-docs-workbench.css").read_text(encoding="utf-8"))
        self.assertIn("white-space: nowrap", (CSS / "ui-sizing.css").read_text(encoding="utf-8"))


    def test_tracked_sources_have_no_local_data_table_geometry(self):
        paths = list(_tracked_ui_sources())
        self.assertTrue(paths, "No tracked sources were discovered")
        failures = []
        for path in paths:
            source = (ROOT / path).read_text(encoding="utf-8")
            for violation in _table_geometry_violations(path.as_posix(), source):
                failures.append(f"{path}: {violation}")
        self.assertFalse(failures, "\n" + "\n".join(failures))

    def test_table_geometry_scanner_detects_repeated_tokens_and_inline_rules(self):
        samples = (
            ("feature.css", ".ordinary td { height: 36px; }"),
            ("feature.css", "@media(max-width:600px){ .ordinary th { padding: var(--ui-table-cell-padding-block); }}"),
            ("feature.css", ".ordinary :is(th, td) { box-sizing: border-box; }"),
            ("feature.css", ":root { --ui-table-row-height:36px; }"),
            ("feature.html", '<style>.ordinary td { vertical-align: middle; }</style>'),
            ("feature.js", '<td style="padding:2px 10px">value</td>'),
            ("feature.js", "cell.style.padding = '2px 10px';"),
            ("feature.py", '<style>.ordinary td {{ height:36px; }}</style>'),
        )
        for path, source in samples:
            with self.subTest(path=path, source=source):
                self.assertTrue(_table_geometry_violations(path, source))
        self.assertFalse(_table_geometry_violations("feature.css", ".ordinary td { width: 120px; white-space: nowrap; }"))
        self.assertFalse(_table_geometry_violations("web/static/css/common.css", ".input-table td { padding: 0 5px; }"))


if __name__ == "__main__":
    unittest.main()
