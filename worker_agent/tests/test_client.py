from __future__ import annotations

from unittest.mock import MagicMock, patch

from worker_agent.client import ControllerClient
from worker_agent.config import WorkerConfig


def _client() -> ControllerClient:
    config = WorkerConfig(
        worker_id="worker 246",
        controller_url="https://controller",
        token="token",
    )
    return ControllerClient(config)


def _json_response(payload: bytes) -> MagicMock:
    response = MagicMock()
    response.read.return_value = payload
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def test_heartbeat_quotes_worker_id_in_cluster_url():
    client = _client()
    with patch(
        "worker_agent.client.urllib.request.urlopen",
        return_value=_json_response(b"{}"),
    ) as opened:
        client.heartbeat({"devices": []})

    assert opened.call_args.args[0].full_url == (
        "https://controller/api/cluster/workers/worker%20246/heartbeat"
    )


def test_poll_quotes_worker_id_in_cluster_url():
    client = _client()
    with patch(
        "worker_agent.client.urllib.request.urlopen",
        return_value=_json_response(b'{"commands": []}'),
    ) as opened:
        assert client.poll() == []

    assert opened.call_args.args[0].full_url == (
        "https://controller/api/cluster/workers/worker%20246/commands/poll"
    )


def test_ack_quotes_worker_and_command_ids_in_cluster_url():
    client = _client()
    with patch(
        "worker_agent.client.urllib.request.urlopen",
        return_value=_json_response(b"{}"),
    ) as opened:
        client.ack("cmd 7/failed", "failed", error="boom")

    assert opened.call_args.args[0].full_url == (
        "https://controller/api/cluster/workers/worker%20246/"
        "commands/cmd%207%2Ffailed/ack"
    )
