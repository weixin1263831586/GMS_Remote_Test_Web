"""Architecture guard for explicit Actor vs ResourceOwner identity use.

ADR 0010 requires business code to classify principal identity at the point of
use.  ``require_authenticated_user(request).id`` is particularly dangerous:
the older owner-hygiene AST checks only saw ``user.id`` where the receiver was
a named variable, so a direct helper-call receiver could bypass the ratchet and
later flow into an owner-scoped sink.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PRINCIPAL_FACTORIES = {
    "get_authenticated_user",
    "require_authenticated_user",
    "require_human_principal",
    "require_elevated_admin",
}
IDENTITY_PIPELINE_PREFIXES = (
    "features/auth/",
    "features/users/clients.py",
)


def _direct_principal_id_read(node: ast.AST) -> bool:
    if not (
        isinstance(node, ast.Attribute)
        and node.attr == "id"
        and isinstance(node.value, ast.Call)
    ):
        return False
    func = node.value.func
    return isinstance(func, ast.Name) and func.id in PRINCIPAL_FACTORIES


class PrincipalIdentityClassificationTests(unittest.TestCase):
    def test_business_code_does_not_read_direct_principal_call_id(self):
        offenders: list[tuple[str, int, str]] = []
        for base in ("features", "foundation", "bootstrap", "worker_agent"):
            for path in sorted((ROOT / base).rglob("*.py")):
                relative = str(path.relative_to(ROOT))
                if "/tests/" in relative or "__pycache__" in relative:
                    continue
                if any(relative.startswith(prefix) for prefix in IDENTITY_PIPELINE_PREFIXES):
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if _direct_principal_id_read(node):
                        factory = node.value.func.id
                        offenders.append((relative, node.lineno, factory))
        self.assertEqual(
            offenders,
            [],
            "direct principal-call .id reads bypass Actor/ResourceOwner "
            "classification. Use principal_actor_id(request) for audit/runtime "
            "session identity, or principal_owner_id(request) for persisted "
            f"ownership: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
