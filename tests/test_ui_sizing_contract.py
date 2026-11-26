"""UI geometry entrypoint and no-wrap guarantees across page roots."""

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
        self.assertIn("#suite-download-url { width: 100%; height: var(--ui-control-height); }", page)
        layout = (CSS / "common-layout.css").read_text(encoding="utf-8")
        self.assertIn(".btn-act { min-height: var(--ui-control-height-dense);", layout)

    def test_global_size_roles(self):
        data = (CSS / "ui-sizing.css").read_text(encoding="utf-8")
        for key, px in {
            "page-edge": 8,
            "control-height": 32,
            "control-height-dense": 28,
            "control-height-small": 26,
            "segmented-inner-height": 24,
            "table-header-height": 42,
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
        self.assertNotIn("--workspace-header-inset:", tabs)

    def test_shared_templates_consume_tokens(self):
        mapping = {
            "common.css": ("--ui-control-height-small", "--ui-stat-height"),
            "common-components.css": ("--ui-control-height-dense", "--ui-control-height-large"),
            "other-workspaces.css": ("--ui-stat-height",),
            "device-inventory.css": ("--ui-control-height",),
            "api-docs-workbench.css": ("--ui-stat-height",),
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
        self.assertIn("var(--ui-control-height-dialog-field)", (
            ROOT / "features/redmine/ui/page.css").read_text(encoding="utf-8"))

    def test_data_table_geometry_has_one_shared_template(self):
        source = (CSS / "ui-sizing.css").read_text(encoding="utf-8")
        for selector, role in (
            ("> thead > tr > :is(th, td)", "table-header-height"),
            ("> tbody > tr:not(:where(.device-group-row, .group-row)) > :is(td, th)", "table-row-height"),
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
            "web/static/js/pages/test-suite-browser-reports.js",
        )
        for path in paths:
            with self.subTest(path=path):
                source = (ROOT / path).read_text(encoding="utf-8")
                self.assertNotRegex(
                    source, r"""<(?:th|td|tr)\b[^>]*style=["'][^"']*(?:height|padding|vertical-align)\s*:"""
                )
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


if __name__ == "__main__":
    unittest.main()
