import ast

from tests.architecture._import_refs import module_names


def test_import_scanner_resolves_static_and_dynamic_import_targets():
    tree = ast.parse(
        'import foundation.config\n'
        'from . import helpers\n'
        'from importlib import import_module as load\n'
        'import importlib as loader\n'
        'load("features.devices.api")\n'
        'loader.import_module(name=".internal", package="features.example")\n'
        '__import__("worker.app")\n'
        'load(dynamic_name)\n'
    )
    assert module_names(tree, "features.example") == {
        "foundation.config", "features.example", "importlib",
        "features.devices.api", "features.example.internal", "worker.app",
    }
    assert module_names(tree, "features.example", include_dynamic=False) == {
        "foundation.config", "features.example", "importlib",
    }


def test_import_scanner_resolves_feature_package_imports():
    tree = ast.parse(
        'from features import devices as device_api, reports\n'
        'from features import users\n'
    )
    assert module_names(tree, "features.example") == {
        "features", "features.devices", "features.reports", "features.users",
    }


def test_import_scanner_resolves_worker_aliases_and_builtin_imports():
    tree = ast.parse(
        'from worker import adb_proxy as proxy\n'
        'import builtins as loaders\n'
        'from builtins import __import__ as load\n'
        'load("features.reports.api")\n'
        'loaders.__import__(name="worker.suite_actions")\n'
    )
    assert module_names(tree, "features.example") == {
        "worker", "worker.adb_proxy", "builtins",
        "features.reports.api", "worker.suite_actions",
    }
    assert module_names(tree, "features.example", include_dynamic=False) == {
        "worker", "worker.adb_proxy", "builtins",
    }
