"""Split templates render without per-request compilation or context reuse."""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.templating import Jinja2Templates

from bootstrap.shell_templates import create_shell_templates


ROOT = Path(__file__).resolve().parents[2]


def context(owner="alice", hostname="first.test"):
    return {
        "config": {"ubuntu_user": owner, "ubuntu_host": hostname},
        "initial_title": f"{owner}'s page",
        "local_worker_id": f"worker-{owner}",
        "request": SimpleNamespace(url=SimpleNamespace(scheme="https", netloc=hostname)),
    }


def test_real_shell_matches_standard_jinja_and_keeps_each_request_context():
    directory = ROOT / "web/shell"
    standard = Jinja2Templates(directory=directory).env
    optimized = create_shell_templates(directory, production=True, bundle_assets=False).env
    for owner, hostname in [("alice", "first.test"), ("<bob>", "second.test")]:
        values = context(owner, hostname)
        assert optimized.get_template("shell.html").render(values) == standard.get_template("shell.html").render(values)
    with patch.object(optimized.loader, "get_source", side_effect=AssertionError("request read templates")), \
            patch.object(optimized, "compile", side_effect=AssertionError("request compiled templates")):
        first = optimized.get_template("shell.html").render(context("alice"))
        second = optimized.get_template("shell.html").render(context("bob", "second.test"))
    assert "worker-alice" in first and "worker-bob" not in first
    assert "worker-bob" in second and "worker-alice" not in second


def test_development_reload_tracks_nested_fragment_changes(tmp_path):
    (tmp_path / "shell.html").write_text("{% include 'outer.html' %}")
    (tmp_path / "outer.html").write_text("{% include 'inner.html' %}")
    inner = tmp_path / "inner.html"
    inner.write_text("before {{ owner }}")
    templates = create_shell_templates(tmp_path)
    assert templates.env.get_template("shell.html").render(owner="alice") == "before alice"
    timestamp = inner.stat().st_mtime
    inner.write_text("after {{ owner }}")
    os.utime(inner, (timestamp + 2, timestamp + 2))
    assert templates.env.get_template("shell.html").render(owner="bob") == "after bob"


@pytest.mark.parametrize("entry,fragment", [
    ("{% set owner = 'parent' %}{% include 'fragment.html' %}{{ owner }}", "{% set owner = 'child' %}{{ owner }}"),
    ("{% include 'fragment.html' without context %}", "{{ owner|default('anonymous') }}"),
    ("{% include 'fragment.html' %}", "{% for item in items %}{{ item }}{% endfor %}"),
    ("before {%- include 'fragment.html' -%} after", " fragment "),
    ("{% include 'absent.html' ignore missing %}ready", "unused"),
])
def test_scoped_templates_keep_normal_jinja_semantics(tmp_path, entry, fragment):
    (tmp_path / "shell.html").write_text(entry)
    (tmp_path / "fragment.html").write_text(fragment)
    standard = Jinja2Templates(directory=tmp_path).env
    optimized = create_shell_templates(tmp_path, production=True).env
    values = {"owner": "request", "items": ["a", "b"]}
    assert optimized.get_template("shell.html").render(values) == standard.get_template("shell.html").render(values)


def test_scoped_includes_are_also_compiled_before_requests(tmp_path):
    (tmp_path / "shell.html").write_text("{% include 'fragment.html' %}")
    (tmp_path / "fragment.html").write_text("{% for item in items %}{{ item }}{% endfor %}")
    environment = create_shell_templates(tmp_path, production=True).env
    with patch.object(environment, "compile", side_effect=AssertionError("late compilation")):
        assert environment.get_template("shell.html").render(items=["ready"]) == "ready"


@pytest.mark.parametrize("keep_newline", [True, False])
def test_composition_preserves_include_trailing_newlines(tmp_path, keep_newline):
    from bootstrap.shell_templates import ShellTemplateLoader

    (tmp_path / "shell.html").write_text("start\n{% include 'fragment.html' %}\nend\n")
    (tmp_path / "fragment.html").write_text("{% include 'inner.html' %}\n\n")
    (tmp_path / "inner.html").write_text("fragment\n")
    standard = Jinja2Templates(directory=tmp_path).env
    optimized = Jinja2Templates(directory=tmp_path).env
    standard.keep_trailing_newline = optimized.keep_trailing_newline = keep_newline
    optimized.loader = ShellTemplateLoader(tmp_path)
    assert optimized.get_template("shell.html").render() == standard.get_template("shell.html").render()
