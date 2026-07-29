from unittest.mock import patch

import pytest

from features.devices.adb_proxy_security import (
    create_pair_grant,
    pair_code_for_worker,
    validate_pair_grant,
)
from worker_agent.adb_proxy import pair_code_from_grant


def test_remote_worker_pair_code_matches_assignment_grant_derivation():
    with patch(
        "features.cluster.worker_auth.worker_tokens",
        return_value={"worker-source": "source-token"},
    ):
        code = pair_code_for_worker(
            "worker-source",
            "worker-local",
            "signed-grant",
        )

    assert code == pair_code_from_grant("source-token", "signed-grant")


def test_pair_grant_is_target_bound_and_rejects_tampering():
    with patch(
        "features.cluster.worker_auth.worker_tokens",
        return_value={"worker-source": "source-token"},
    ):
        grant = create_pair_grant(
            "worker-source", "worker-target", "worker-local"
        )
        validate_pair_grant(
            grant, "worker-source", "worker-target", "worker-local"
        )
        with pytest.raises(ValueError, match="host mismatch"):
            validate_pair_grant(
                grant, "worker-source", "other-target", "worker-local"
            )
        with pytest.raises(ValueError, match="invalid"):
            validate_pair_grant(
                grant + "x",
                "worker-source",
                "worker-target",
                "worker-local",
            )
