from .repository import init_db, query_exemption_match


def __getattr__(name: str):
    if name != 'api':
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    from importlib import import_module

    api = import_module(f'{__name__}.api')
    globals()['api'] = api
    return api


__all__ = ["api", "init_db", "query_exemption_match"]
