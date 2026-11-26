"""Verify feature facade declarations and lazy exports."""

import importlib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class FeaturePublicSurfaceTests(unittest.TestCase):
    def test_every_declared_public_symbol_resolves(self):
        offenders = []
        for init in sorted((ROOT / "features").glob("*/__init__.py")):
            feature = init.parent.name
            module = importlib.import_module(f"features.{feature}")
            declared = getattr(module, "__all__", None)
            self.assertIsNotNone(declared, f"features.{feature} must declare __all__")
            for name in declared or ():
                try:
                    getattr(module, name)
                except Exception as exc:
                    offenders.append(f"features.{feature}:{name} -> {exc!r}")
        self.assertEqual(offenders, [], "public surfaces must resolve")

    def test_lazy_exports_match_public_declarations(self):
        for name in ("features.devices", "features.knowledge"):
            module = importlib.import_module(name)
            with self.subTest(module=name):
                self.assertEqual(set(module._LAZY_API_EXPORTS) - set(module.__all__), set())
                self.assertEqual(len(module.__all__), len(set(module.__all__)))


if __name__ == "__main__":
    unittest.main()
