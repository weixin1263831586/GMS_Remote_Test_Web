from __future__ import annotations

import pytest
from fastapi import HTTPException

from features.cluster.execution_spec import (
    build_argv_from_spec,
    canonicalize_execution_spec,
)


def _spec(**updates):
    spec = {
        "test_type": "cts",
        "suite_path": "/srv/GMS-Suite/android-cts/tools",
        "module": "",
        "test_case": "",
        "retry_dir": "",
        "devices": ["ABC"],
        "local_server": "",
        "copy_remote": False,
        "no_retry": False,
    }
    spec.update(updates)
    return spec


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"test_case": "Case#test"}, "test_case requires module"),
        (
            {"module": "CtsFoo", "retry_dir": "2026.08.10_01.02.03"},
            "retry_dir cannot be combined",
        ),
        ({"copy_remote": True}, "copy_remote requires local_server"),
        ({"devices": []}, "requires at least one device"),
    ],
)
def test_invalid_execution_spec_combinations_are_rejected(updates, message):
    with pytest.raises(HTTPException, match=message):
        build_argv_from_spec(_spec(**updates))


def test_execution_spec_test_type_must_match_suite_family():
    with pytest.raises(HTTPException, match="incompatible with VTS"):
        canonicalize_execution_spec(
            _spec(),
            suite_path="/srv/GMS-Suite/android-vts/tools",
            suite_type="VTS",
            devices=["worker-1:ABC"],
            worker_id="worker-1",
        )
