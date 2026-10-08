"""Serve ordered Shell scripts/styles as cached, versioned static resources."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import NotModifiedResponse, StaticFiles


_RUNTIME_SCRIPT = re.compile(
    r'<script src="(/static/js/shell/[a-z0-9-]+\.js)(?:\?[^\"]*)?" defer data-shell-runtime></script>'
)
RUNTIME_PATH = "js/shell/shell-runtime.js"
STYLES_PATH = "css/shell-style.css"
_STYLESHEET = re.compile(
    r'<link rel="stylesheet" href="(/static/css/[a-z0-9-]+\.css)(?:\?[^\"]*)?" data-shell-styles>'
)
_CSS_IMPORT = re.compile(r'@import\s+url\("(/static/css/[a-z0-9-]+\.css)(?:\?[^\"]*)?"\)\s*;')
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


@dataclass(frozen=True)
class ShellAsset:
    content: bytes
    digest: str
    sources: tuple[tuple[Path, int, int], ...]

    def is_current(self):
        try:
            for path, mtime, size in self.sources:
                stat = path.stat()
                if (stat.st_mtime_ns, stat.st_size) != (mtime, size):
                    return False
            return True
        except OSError:
            return False


class ShellRuntimeBundle:
    def __init__(self, static_directory: Path):
        self.directory = static_directory
        self.asset: ShellAsset | None = None

    def rewrite(self, source: str):
        matches = list(_RUNTIME_SCRIPT.finditer(source))
        if not matches:
            return source, None
        if any(source[first.end():second.start()].strip() for first, second in pairwise(matches)):
            raise ValueError("Shell runtime scripts must be consecutive to preserve execution order")
        chunks, sources = [], []
        for match in matches:
            filename = match.group(1).removeprefix("/static/")
            path = self.directory / filename
            stat = path.stat()
            code = path.read_text(encoding="utf-8-sig")
            sources.append((path, stat.st_mtime_ns, stat.st_size))
            # Keep classic-script globals shared. A newline/semicolon prevents
            # adjacent files or trailing comments from joining expressions.
            chunks.append(f"// Source: /static/{filename}\n{code}\n;\n")
        content = "".join(chunks).encode("utf-8")
        asset = ShellAsset(content, hashlib.sha256(content).hexdigest(), tuple(sources))
        self.asset = asset
        tag = f'<script src="/static/{RUNTIME_PATH}?v={asset.digest}" defer></script>'
        return source[:matches[0].start()] + tag + source[matches[-1].end():], asset


class ShellStylesBundle:
    def __init__(self, static_directory: Path):
        self.directory = static_directory
        self.asset: ShellAsset | None = None

    def rewrite(self, source: str):
        matches = list(_STYLESHEET.finditer(source))
        if not matches:
            return source, None
        if any(source[first.end():second.start()].strip() for first, second in pairwise(matches)):
            raise ValueError("Shell stylesheets must be consecutive to preserve cascade order")
        sources, expanded = {}, {}

        def expand(filename, stack=()):
            if filename in stack:
                raise ValueError("cyclic Shell stylesheet import")
            if filename in expanded:
                return expanded[filename]
            path = self.directory / filename
            stat = path.stat()
            css = path.read_text(encoding="utf-8-sig")
            sources[path] = (path, stat.st_mtime_ns, stat.st_size)
            # Only unscoped imports from this CSS directory can be flattened:
            # relative url() values keep the same base in the generated URL.
            normalized = _CSS_COMMENT.sub("", css)
            remaining = normalized.lstrip()
            while import_match := _CSS_IMPORT.match(remaining):
                remaining = remaining[import_match.end():].lstrip()
            if ("@import" in remaining or "@charset" in normalized or "@namespace" in normalized
                    or len(_CSS_IMPORT.findall(css)) != len(_CSS_IMPORT.findall(normalized))):
                raise ValueError("scoped or external stylesheet import")
            css = _CSS_IMPORT.sub(lambda match: expand(match.group(1).removeprefix("/static/"), (*stack, filename)), css)
            expanded[filename] = css
            return css

        try:
            content = "\n".join(expand(match.group(1).removeprefix("/static/")) for match in matches).encode("utf-8")
        except ValueError:
            # Preserve normal browser semantics for media/layer imports and
            # external URLs instead of changing their scope or relative base.
            self.asset = None
            return source, None
        asset = ShellAsset(content, hashlib.sha256(content).hexdigest(), tuple(sources.values()))
        self.asset = asset
        tag = f'<link rel="stylesheet" href="/static/{STYLES_PATH}?v={asset.digest}">'
        return source[:matches[0].start()] + tag + source[matches[-1].end():], asset


class ShellStaticFiles(StaticFiles):
    def __init__(self, *, directory: Path, runtime_bundle=None, style_bundle=None):
        super().__init__(directory=directory)
        self.runtime_bundle = runtime_bundle
        self.style_bundle = style_bundle

    async def get_response(self, path, scope):
        bundle, media_type = {
            RUNTIME_PATH: (self.runtime_bundle, "text/javascript"),
            STYLES_PATH: (self.style_bundle, "text/css"),
        }.get(path, (None, None))
        asset = bundle.asset if bundle is not None else None
        if asset is None:
            return await super().get_response(path, scope)
        if scope["method"] not in ("GET", "HEAD"):
            raise HTTPException(status_code=405)
        response = Response(
            asset.content if scope["method"] == "GET" else b"",
            media_type=media_type,
            headers={"ETag": f'"{asset.digest}"', "Content-Length": str(len(asset.content))},
        )
        if self.is_not_modified(response.headers, Headers(scope=scope)):
            return NotModifiedResponse(response.headers)
        return response
