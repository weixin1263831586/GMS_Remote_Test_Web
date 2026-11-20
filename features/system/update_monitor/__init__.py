"""Update monitor public API, loaded on demand by the Web application."""
from importlib import import_module


__all__ = ["api"]


def __getattr__(name: str):
    if name not in {"api", "api_support"}:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f"{__name__}.{name}")
    globals()[name] = module
    return module
