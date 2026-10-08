"""Keep runtime modules ordered and free of parser-blocking downloads."""

from html.parser import HTMLParser

from tests.contract.snapshot_tools import read_shell_template


class ShellScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "script" and attrs.get("src"):
            self.scripts.append(attrs)


def test_shell_runtime_scripts_are_deferred_in_dependency_order():
    parser = ShellScripts()
    parser.feed(read_shell_template())
    blocking = [attrs["src"].split("?")[0] for attrs in parser.scripts if "defer" not in attrs]
    assert blocking == ["/static/js/shell/shell-early-boot.js"]
    assert all("async" not in attrs for attrs in parser.scripts)
    deferred = [attrs["src"].split("?")[0] for attrs in parser.scripts if "defer" in attrs]
    assert deferred[0] == "/static/js/shell/shell-config.js"
    assert deferred.index("/static/js/shell/shell-main.js") < deferred.index("/static/js/navigation.js")
    assert deferred.index("/static/js/state.js") < deferred.index("/static/js/api.js")
    assert deferred.index("/static/js/api.js") < deferred.index("/static/js/navigation.js")
