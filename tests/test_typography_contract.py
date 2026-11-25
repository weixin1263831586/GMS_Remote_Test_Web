"""Contracts for the single typography stylesheet used by every UI root."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STYLE = ROOT / "web/static/css/typography.css"
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
)


class TypographyContractTests(unittest.TestCase):
    def test_all_ui_entrypoints_load_the_canonical_stylesheet(self):
        for path in ENTRYPOINTS:
            with self.subTest(path=path):
                data = (ROOT / path).read_text(encoding="utf-8")
                self.assertEqual(
                    data.count('/static/css/typography.css'), 1,
                    f"{path} should load the shared typography stylesheet exactly once",
                )

    def test_required_tokens_cover_semantic_text_roles(self):
        css = STYLE.read_text(encoding="utf-8")
        for name, value in {
            "compact": 11, "small": 12, "body": 13, "card": 14,
            "section": 15, "dialog": 16, "page": 18, "stat": 24,
        }.items():
            with self.subTest(role=name):
                self.assertRegex(
                    css, rf"--ui-type-size-{name}:\s*{value}px\s*;",
                )
        self.assertIn("--ui-type-font-mono:", css)
        self.assertIn("--ui-type-line-page: 24px", css)
        self.assertIn("button:not(.modal-close)", (STYLE.parent / "typography/roles-and-pages.css").read_text(encoding="utf-8"))

    def test_canonical_typography_has_no_shadowed_duplicates(self):
        """Former per-module copies must not survive after shared roles own them."""
        folder = STYLE.parent / "typography"
        shared = (folder / "shared-diagrams.css").read_text(encoding="utf-8")
        shell = (folder / "shell-layout.css").read_text(encoding="utf-8")
        roles = (folder / "roles-and-pages.css").read_text(encoding="utf-8")
        self.assertIn(
            ".tech-dialog .tech-title-group",
            roles,
        )
        self.assertNotIn(
            ".tech-dialog .tech-title-group :is(.modal-title,h2,h3)",
            shared,
        )
        self.assertEqual(
            shared.count(
                'html[data-ui-type-root="architecture"] '
                ".architecture-v2 .arch-v2-timeline li { font-size:"
            ),
            1,
        )
        self.assertNotIn(
            "@media(max-width: 800px) { :is(.modal,.modal-backdrop,.auth-gate)",
            shared,
        )
        self.assertNotIn(
            '@media (max-width: 768px) { html[data-ui-type-root="shell"] '
            ".tool-title { font-size:",
            shell,
        )

    def test_canonical_rule_chunks_are_reviewable(self):
        css = STYLE.read_text(encoding="utf-8")
        parts = (
            "roles-and-pages.css", "shell-layout.css",
            "shell-workspaces.css", "shared-diagrams.css",
            "inline-and-exceptions.css",
        )
        # Asset versions change when individual typography chunks are edited.
        # Validate all five imported paths and nonempty versions instead of
        # pinning a stale cache-busting token in the contract itself.
        imports = re.findall(
            r'@import url\("/static/css/typography/([^"?]+)\?v=([A-Za-z0-9._-]+)"\);',
            css,
        )
        self.assertEqual([path for path, _version in imports], list(parts))
        self.assertEqual(css.count("@import url("), len(parts))
        for part in parts:
            with self.subTest(part=part):
                file = STYLE.parent / "typography" / part
                self.assertTrue(file.is_file(), f"missing typography chunk: {part}")
                self.assertLessEqual(file.stat().st_size, 50 * 1024)
        for entrypoint in ENTRYPOINTS:
            with self.subTest(entrypoint=entrypoint):
                page = (ROOT / entrypoint).read_text(encoding="utf-8")
                self.assertNotRegex(
                    page, r'<link[^>]+href="/static/css/typography/',
                    "load only the global typography entrypoint",
                )

    def test_standalone_review_pages_load_global_typography(self):
        for path in ("features/devices/deployment/api.py", "features/devices/debug/api.py"):
            with self.subTest(path=path):
                source = (ROOT / path).read_text(encoding="utf-8")
                self.assertIn('data-ui-type-root="review"', source)
                self.assertEqual(source.count("/static/css/typography.css"), 1)
                self.assertNotIn("/static/css/typography/", source,
                                 "review page must only load the single typography entrypoint")
        review = (ROOT / "web/static/css/device-deployment-review.css").read_text(encoding="utf-8")
        self.assertNotRegex(review, r"(?<![\w-])font\s*:")

    def test_modules_cannot_reintroduce_local_type_aliases(self):
        forbidden = ("--tech-font-title", "--tech-font-body", "--tech-font-caption",
                     "--analysis-control-font-size", "--ui-font-mono", "--arch-font")
        paths = [
            ROOT / "web/static/css/modal-tech-dialogs.css",
            ROOT / "web/static/css/analysis-workbench-tokens.css",
            ROOT / "web/static/css/ui-foundations.css",
            ROOT / "web/static/css/architecture-layers.css",
            *sorted((ROOT / "web/static/css/typography").glob("*.css")),
        ]
        for file in paths:
            with self.subTest(path=str(file.relative_to(ROOT))):
                content = file.read_text(encoding="utf-8")
                for alias in forbidden:
                    self.assertNotIn(alias, content)

    def test_authored_css_does_not_own_type_declarations(self):
        styles = list((ROOT / "web/static/css").glob("*.css"))
        styles += list((ROOT / "features").glob("*/ui/*.css"))
        styles += list((ROOT / "features/redmine/ui").glob("*.css"))
        pattern = re.compile(r"(?<![\w-])(?:font-size|font-family|font-weight|font)\s*:")
        for file in sorted(set(styles)):
            if file == STYLE:
                continue
            with self.subTest(path=str(file.relative_to(ROOT))):
                text = file.read_text(encoding="utf-8")
                text = re.sub(r"/\*[\s\S]*?\*/", "", text)
                self.assertNotRegex(text, pattern, "font rules belong in typography/")

    def test_dashboard_stats_and_form_controls_are_semantic(self):
        css = (STYLE.parent / "typography/roles-and-pages.css").read_text(encoding="utf-8")
        self.assertIn('html[data-ui-type-root="cluster"] .cluster-main .dash-stat-card .num', css)
        self.assertIn('html[data-ui-type-root="gerrit"] .stat-card .value', css)
        self.assertIn('html[data-ui-type-root="redmine"] .stat-card .value', css)
        rules = (STYLE.parent / "typography/roles-and-pages.css").read_text(encoding="utf-8")
        self.assertIn('html[data-ui-type-root="automation"] .stat-card .stat-value', rules)

    def test_authored_css_avoids_unreadably_small_literal_text_sizes(self):
        styles = list((ROOT / "web/static/css").glob("*.css"))
        styles += list((ROOT / "features").glob("*/ui/*.css"))
        styles += list((ROOT / "features/redmine/ui").glob("*.css"))
        pattern = re.compile(r"\bfont-size\s*:\s*(?:9|10)(?:\.\d+)?px\b")
        for file in sorted(set(styles)):
            with self.subTest(path=str(file.relative_to(ROOT))):
                self.assertNotRegex(
                    file.read_text(encoding="utf-8"), pattern,
                    "ordinary label text should use the compact token, not a 9/10px literal",
                )



    def test_standard_controls_never_use_compact_token(self):
        """Compact 11px is reserved for annotated information, not controls."""
        pattern = re.compile(
            r"\bbutton\b|\binput\b|\bselect\b|\btextarea\b|"
            r"\bth\b|\btd\b|\.btn(?:-|\b)|\.filter-btn|"
            r"\.copy-btn|\.ka-btn|\.log-tab-btn|\.page-title-action"
        )
        for path in sorted((STYLE.parent / "typography").glob("*.css")):
            content = re.sub(r"/\*[\s\S]*?\*/", "", path.read_text(encoding="utf-8"))
            for selectors, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", content):
                if "--ui-type-size-compact" in declarations and pattern.search(selectors):
                    self.fail(f"{path.name}: compact size on control {selectors.strip()}")

    def test_canonical_font_stack_and_standard_weights(self):
        for path in sorted((STYLE.parent / "typography").glob("*.css")):
            content = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotRegex(content, r"font-weight:\s*(?:450|560|800)\b")
                self.assertNotRegex(
                    content,
                    r"font(?:-family)?\s*:[^;{}]*\b(?:Inter|Monaco|Cascadia Code|Fira Code)\b",
                )


    def test_canonical_entrypoint_owns_only_tokens_and_imports(self):
        entry = STYLE.read_text(encoding="utf-8")
        self.assertEqual(entry.count("@import url("), 5)
        self.assertNotIn('html[data-ui-type-root=', entry)
        self.assertNotRegex(entry, r"(?<!-)\bfont-size\s*:")
        roles = (STYLE.parent / "typography/roles-and-pages.css").read_text(encoding="utf-8")
        self.assertIn('html[data-ui-type-root="review"] button', roles)
        self.assertIn('html[data-ui-type-root="cluster"] #dashboard .dash-panel-title', roles)


    def test_shared_normal_controls_and_descriptions_are_semantic(self):
        shell = (STYLE.parent / "typography/shell-layout.css").read_text(encoding="utf-8")
        workspaces = (STYLE.parent / "typography/shell-workspaces.css").read_text(encoding="utf-8")
        pages = (STYLE.parent / "typography/roles-and-pages.css").read_text(encoding="utf-8")
        diagrams = (STYLE.parent / "typography/shared-diagrams.css").read_text(encoding="utf-8")
        exceptions = (STYLE.parent / "typography/inline-and-exceptions.css").read_text(encoding="utf-8")
        required = (
            (shell, 'html[data-ui-type-root="shell"] .adb-proxy-assignments { font-size: var(--ui-type-size-body); }'),
            (shell, 'html[data-ui-type-root="shell"] .page-header-actions select { font-size: var(--ui-type-size-body); }'),
            (shell, 'html[data-ui-type-root="shell"] .list-table th { font-size: var(--ui-type-size-small); }'),
            (shell, 'html[data-ui-type-root="shell"] .users-table th { font-size: var(--ui-type-size-small); }'),
            (shell, 'html[data-ui-type-root="shell"] .btn-terminal { font-size: var(--ui-type-size-small);'),
            (shell, 'html[data-ui-type-root="shell"] .tools-add-category { font-size: var(--ui-type-size-small);'),
            (diagrams, '.device-routing-notes { font-size: var(--ui-type-size-body); }'),
            (workspaces, '#page-api-docs .api-param-row { font-size: var(--ui-type-size-body); }'),
            (pages, 'html[data-ui-type-root="automation"] .panel-description { font-size: var(--ui-type-size-body);'),
            (pages, 'html[data-ui-type-root="redmine"] .field-content { font-size: var(--ui-type-size-body); }'),
            (exceptions, '.project-guide-steps li { font-size: var(--ui-type-size-body); }'),
        )
        for stylesheet, rule in required:
            with self.subTest(rule=rule):
                self.assertIn(rule, stylesheet)
        self.assertNotIn('.page-header-actions :is(button, select)', shell)
        weekly = (ROOT / "web/static/js/shell/weekly-report.js").read_text(encoding="utf-8")
        self.assertNotRegex(weekly, r"\bfont-(?:size|weight)\s*:")
        self.assertIn('data-ui-type-size=', weekly)
        self.assertIn('data-ui-type-weight=', weekly)
        api = (ROOT / "web/static/js/pages/api-docs.js").read_text(encoding="utf-8")
        self.assertIn('class="api-param-row"', api)

    def test_dynamic_ui_cannot_reintroduce_literal_control_fonts(self):
        """Dynamic HTML templates must not bypass the canonical typography roles."""
        sources = (
            "web/static/js/shell/dnd-sort.js",
            "web/static/js/shell/shell-device-groups.js",
            "web/static/js/pages/test-control.js",
            "web/static/js/pages/test-reports.js",
            "web/static/js/shell/shell-user-management.js",
            "web/static/js/shell/shell-device-override.js",
            "web/static/js/pages/test-suite-browser-reports.js",
            "web/static/js/pages/api-docs.js",
            "features/redmine/ui/knowledge.js",
            "features/system/gms_assistant_proxy.py",
        )
        for name in sources:
            content = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertNotRegex(content, r"\bfont-size\s*:\s*(?:11|12|13|14|15|16)px\b")
                self.assertNotRegex(content, r"\.style\.fontSize\s*=\s*['\"]\d+(?:\.\d+)?px['\"]")
        groups = (ROOT / "web/static/js/shell/shell-device-groups.js").read_text(encoding="utf-8")
        self.assertNotIn("font-size:var(--ui-type-size-compact)", groups)
        assistant = (ROOT / "features/system/gms_assistant_proxy.py").read_text(encoding="utf-8")
        self.assertEqual(assistant.count("/static/css/typography.css"), 2)

    def test_report_ai_copy_selection_is_independent_of_css(self):
        source = (ROOT / "web/static/js/pages/report-analysis.js").read_text(encoding="utf-8")
        self.assertIn('querySelectorAll(".ai-report-copy-text")', source)
        self.assertNotIn('div[style*="font-size"]', source)
        self.assertIn('ai-report-source-heading', source)

    def test_normal_component_roles_are_not_overridden_by_special_pages(self):
        main = (STYLE.parent / "typography/roles-and-pages.css").read_text(encoding="utf-8")
        exceptions = (STYLE.parent / "typography/inline-and-exceptions.css").read_text(encoding="utf-8")
        self.assertIn('html[data-ui-type-root="review"] button { font-size: var(--ui-type-size-small);', main)
        self.assertIn('html[data-ui-type-root="review"] :is(input,select,textarea) { font-size: var(--ui-type-size-body);', main)
        self.assertIn('html[data-ui-type-root="gerrit"] .gerrit-wiki-btn { font-size: var(--ui-type-size-small); }', exceptions)
        self.assertIn('html[data-ui-type-root="update-radar"] button { font-family: inherit; font-size: var(--ui-type-size-small); }', exceptions)

    def test_operational_copy_and_responsive_tabs_use_standard_roles(self):
        shell = (STYLE.parent / "typography/shell-layout.css").read_text(encoding="utf-8")
        spaces = (STYLE.parent / "typography/shell-workspaces.css").read_text(encoding="utf-8")
        diagrams = (STYLE.parent / "typography/shared-diagrams.css").read_text(encoding="utf-8")
        exceptions = (STYLE.parent / "typography/inline-and-exceptions.css").read_text(encoding="utf-8")
        self.assertIn('.category-tab { font-size: var(--ui-type-size-small);', shell)
        self.assertNotRegex(shell, r"@media[^\n]*\.category-tab\s*\{")
        for role in (".file-browser-error-host", ".file-browser-error-help", ".history-mail-count"):
            self.assertIn(role, shell)
        self.assertIn('.host-workspace-title-state { font-size: var(--ui-type-size-small);', spaces)
        self.assertIn('[data-terminal-pane-mode-label] { font-size: var(--ui-type-size-small);', spaces)
        self.assertIn('.arch-scene-sidecard h3 { font-size: var(--ui-type-size-card);', diagrams)
        self.assertNotIn('.arch-scene-sidecard h3', exceptions)
        self.assertEqual(diagrams.count('.arch-scene-chip { font-size:'), 1)
        self.assertIn('.apk-perm-short { font-size: var(--ui-type-size-small);', shell)
        self.assertIn('.apk-file-path { font-family: var(--ui-type-font-mono); font-size: var(--ui-type-size-small);', shell)
        self.assertIn('.apk-tree-item { font-family: var(--ui-type-font-mono); font-size: var(--ui-type-size-small);', spaces)
        self.assertIn('#apk-file-path { font: var(--ui-type-size-small)/16px var(--ui-type-font-mono);', spaces)
        for name in (
            "web/static/js/pages/file-browser.js",
            "web/static/js/pages/test-control.js",
            "web/static/js/shell/shell-terminal-workspace.js",
        ):
            with self.subTest(name=name):
                content = (ROOT / name).read_text(encoding="utf-8")
                self.assertNotRegex(content, r"font-size\s*:")

if __name__ == "__main__":
    unittest.main()
