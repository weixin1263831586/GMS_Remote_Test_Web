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
    "features/system/mainline_issues/ui/page.html",
    "features/system/update_monitor/ui/page.html",
    "features/system/update_radar/ui/page.html",
    "features/devices/deployment_api.py",
    "features/devices/debug_api.py",
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

    def test_global_size_roles(self):
        data = (CSS / "ui-sizing.css").read_text(encoding="utf-8")
        for key, px in {
            "page-edge": 8,
            "control-height": 32,
            "control-height-dense": 28,
            "control-height-small": 26,
            "segmented-inner-height": 24,
            "table-header-height": 42,
            "table-row-height": 44,
            "stat-height": 95,
            "stat-height-compact": 70,
            "card-heading-height": 42,
            "modal-actions-min-height": 58,
        }.items():
            with self.subTest(key=key):
                self.assertRegex(data, rf"--ui-{key}:\s*{px}px\s*;")
        self.assertNotRegex(data, r"(?<![\w-])font(?:-size|-family|-weight)?\s*:")

    def test_shared_templates_consume_tokens(self):
        mapping = {
            "common.css": ("--ui-control-height-small", "--ui-stat-height"),
            "common-components.css": ("--ui-control-height-dense", "--ui-control-height-large"),
            "other-workspaces.css": ("--ui-table-row-height", "--ui-stat-height"),
            "device-inventory.css": ("--ui-table-header-height", "--ui-control-height"),
            "api-docs-workbench.css": ("--ui-table-row-height", "--ui-stat-height"),
            "analysis-workbench-tokens.css": ("--ui-control-height",),
            "modal-tech-dialogs.css": ("--ui-modal-actions-min-height",),
            "data-table-page.css": ("--ui-table-header-height", "--ui-control-height"),
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
                "--ui-tab-height", "--ui-table-row-height",
            ),
            "web/static/css/apk-workbench.css": (
                "--ui-tab-height", "--ui-tab-height-narrow", "--ui-control-height-dense",
            ),
            "web/static/css/other-workspaces.css": (
                "--ui-table-header-height", "--ui-table-row-height",
                "--ui-table-header-height-dense",
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
                "--ui-table-row-height-dense", "--ui-touch-target-height",
            ),
            "features/gerrit/ui/page.html": ("--ui-control-height-icon",),
            "features/system/update_radar/ui/page.html": ("--ui-control-height",),
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

    def test_audit_table_geometry_uses_same_role_as_common_table(self):
        source = (CSS / "other-workspaces.css").read_text(encoding="utf-8")
        for suffix in ("audit-log-table", "audit-mail-table"):
            for row_kind, role in (
                ("thead th", "table-header-height"),
                ("tbody td", "table-row-height"),
            ):
                with self.subTest(table=suffix, part=row_kind):
                    selector = "." + suffix + " " + row_kind
                    self.assertIn(selector, source)
                    rule = source.split(selector, 1)[1].split("{", 1)[1].split("}", 1)[0]
                    self.assertIn("var(--ui-" + role + ")", rule)


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
