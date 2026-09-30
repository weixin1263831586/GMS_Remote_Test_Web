"""Feature public surface contract: declared public symbols must resolve.

Feature facades (``features/<name>/__init__.py``) increasingly use lazy
``__getattr__`` re-exports to avoid import cycles and heavy module loads.
The downside: a broken lazy export (typo, moved symbol, runtime side effect)
only explodes on first use.  This contract walks every feature's ``__all__``
and resolves each symbol eagerly, so "declared public" and "actually
importable" can never drift apart in CI.

Symbols that genuinely cannot be imported in a test process (e.g. they
require a device connection at import time) must be listed in
``RUNTIME_SIDE_EFFECT_SYMBOLS`` with a reason — keep it empty and shrink-only.
"""

import importlib
import unittest


RUNTIME_SIDE_EFFECT_SYMBOLS: dict[str, str] = {
    # "features.example:symbol": "reason: connects to hardware on import"
}


class FeaturePublicSurfaceTests(unittest.TestCase):
    def test_every_declared_public_symbol_resolves(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2]
        offenders = []
        for init in sorted((root / "features").glob("*/__init__.py")):
            feature = init.parent.name
            module = importlib.import_module(f"features.{feature}")
            declared = getattr(module, "__all__", None)
            self.assertIsNotNone(
                declared,
                f"features.{feature} must declare __all__ (public surface)",
            )
            for name in declared or ():
                key = f"features.{feature}:{name}"
                if key in RUNTIME_SIDE_EFFECT_SYMBOLS:
                    continue
                try:
                    getattr(module, name)
                except Exception as exc:
                    offenders.append(f"{key} -> {exc!r}")
        self.assertEqual(
            offenders,
            [],
            "public surfaces must resolve: " + "; ".join(offenders),
        )

    def test_skip_manifest_points_at_real_symbols(self):
        stale = []
        for key in RUNTIME_SIDE_EFFECT_SYMBOLS:
            module_name, _, symbol = key.partition(":")
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                stale.append(key)
                continue
            if not hasattr(module, symbol) and symbol not in getattr(
                module, "__all__", ()
            ):
                stale.append(key)
        self.assertEqual(
            stale,
            [],
            f"stale skip-manifest entries: {stale}; delete them "
            "(the manifest is shrink-only)",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
