"""Compile the split Shell as one template without caching request data."""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import FileSystemLoader, TemplateNotFound, TemplateSyntaxError, meta, nodes
from starlette.templating import Jinja2Templates

from bootstrap.shell_assets import ShellRuntimeBundle, ShellStylesBundle


_INCLUDE = re.compile(r"{%\s*include\s+(['\"])([^'\"]+)\1\s*%}")


class _ScopedTemplateError(Exception):
    """A template requires Jinja's normal include scope or whitespace rules."""


class ShellTemplateLoader(FileSystemLoader):
    def __init__(self, directory, *, runtime_bundle=None, style_bundle=None):
        super().__init__(directory)
        self.runtime_bundle = runtime_bundle
        self.style_bundle = style_bundle

    def get_source(self, environment, template):
        if template != "shell.html":
            return super().get_source(environment, template)
        self.flattened_entry = False
        sources = {}
        checks = []

        def expand(name, stack=()):
            if name in stack:
                raise TemplateSyntaxError("cyclic Shell include", 1, name=name)
            if name in sources:
                return sources[name][0]
            source, filename, current = super(ShellTemplateLoader, self).get_source(environment, name)
            checks.append(current)
            tree = environment.parse(source, name=name, filename=filename)
            includes = list(tree.find_all(nodes.Include))
            matches = list(_INCLUDE.finditer(source))
            # Flatten only output-only HTML fragments. Assignments, loops,
            # macros and special include forms retain their normal Jinja scope.
            if (not name.endswith(".html")
                    or any(not isinstance(node, (nodes.Output, nodes.Include)) for node in tree.body)
                    or len(includes) != len(matches)
                    or any(not isinstance(node.template, nodes.Const)
                           or node.template.value != match.group(2)
                           or not node.with_context or node.ignore_missing
                           for node, match in zip(includes, matches))):
                raise _ScopedTemplateError
            if name != template and not environment.keep_trailing_newline:
                # Jinja drops one final newline when rendering each included
                # file. Preserve that rule when composing their source text.
                source = re.sub(r"(?:\r\n|\r|\n)\Z", "", source)
            assembled = _INCLUDE.sub(lambda match: expand(match.group(2), (*stack, name)), source)
            sources[name] = (assembled, filename)
            return assembled

        try:
            source = expand(template)
        except _ScopedTemplateError:
            return super().get_source(environment, template)
        for bundle in (self.style_bundle, self.runtime_bundle):
            if bundle is not None:
                source, asset = bundle.rewrite(source)
                if asset is not None:
                    checks.append(asset.is_current)
        self.flattened_entry = True
        return source, sources[template][1], lambda: all(check() for check in checks)


def _warm_templates(environment, name, seen=None):
    seen = set() if seen is None else seen
    if name in seen:
        return
    seen.add(name)
    environment.get_template(name)
    if name == "shell.html" and environment.loader.flattened_entry:
        return
    source = environment.loader.get_source(environment, name)[0]
    for dependency in meta.find_referenced_templates(environment.parse(source)):
        if dependency:
            try:
                _warm_templates(environment, dependency, seen)
            except TemplateNotFound:
                # Optional includes/fallback lists retain their runtime
                # behavior. A required missing template still fails on use.
                continue


def create_shell_templates(
    directory: str | Path, *, production: bool = False, bundle_assets: bool = True,
) -> Jinja2Templates:
    directory = Path(directory)
    templates = Jinja2Templates(directory=directory)
    static_directory = directory.parent / "static"
    build_assets = bundle_assets and static_directory.is_dir()
    templates.runtime_bundle = ShellRuntimeBundle(static_directory) if build_assets else None
    templates.style_bundle = ShellStylesBundle(static_directory) if build_assets else None
    templates.env.loader = ShellTemplateLoader(
        directory, runtime_bundle=templates.runtime_bundle, style_bundle=templates.style_bundle,
    )
    # Deployed source is immutable for the lifetime of a process. Development
    # still reloads changes in any fragment, including nested includes.
    templates.env.auto_reload = not production
    templates.env.globals["url_for"] = lambda endpoint, filename="": (
        f"/static/{filename}" if endpoint == "static" else f"/{endpoint}"
    )
    # Compile before serving requests; globals/config/user data remain bound
    # separately for each response rather than caching rendered HTML.
    # API-only application fixtures/deployments may not contain the Web shell.
    if (directory / "shell.html").is_file():
        _warm_templates(templates.env, "shell.html")
    return templates
