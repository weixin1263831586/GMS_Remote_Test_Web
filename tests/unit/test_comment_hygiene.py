from pathlib import Path

from tools.scripts.maintenance.audit_comment_hygiene import python_comments


def test_python_comments_ignore_business_string_literals():
    source = (
        '"""Module documentation."""\n'
        '# Real comment\n'
        'message = """business string"""\n'
        'def example():\n'
        '    """Function documentation."""\n'
        '    return """response payload"""\n'
    )
    assert set(python_comments(Path("sample.py"), source)) == {
        (1, "Module documentation."),
        (2, "Real comment"),
        (5, "Function documentation."),
    }
