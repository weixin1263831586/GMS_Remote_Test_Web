"""Untrusted source paths and remote responses fail with semantic errors."""

from unittest.mock import MagicMock, patch

import pytest

from features.system.source_codesearch import CodesearchProvider
from features.system.source_provider_contract import (
    MAX_BLOB_BYTES,
    ProviderConfig,
    SourceProviderError,
    _safe_repo_path,
)


@pytest.mark.parametrize("path", ["/etc/passwd", "a/..", "a/../b", "a/.git", "C:\\private", "a/.git/config"])
def test_source_path_rejects_private_and_parent_paths(path):
    with pytest.raises(SourceProviderError) as caught:
        _safe_repo_path(path)
    assert caught.value.status_code == 422


def test_codesearch_bounds_response_before_decoding():
    provider = CodesearchProvider(ProviderConfig("source", "codesearch", base_url="https://source.example"), b"test")
    response = MagicMock()
    response.read.return_value = b"x" * (MAX_BLOB_BYTES + 1)
    with patch("features.system.source_codesearch.urllib.request.urlopen") as opening:
        opening.return_value.__enter__.return_value = response
        with pytest.raises(SourceProviderError) as caught:
            provider._request("/api/v1/search", {})
    assert caught.value.status_code == 502
    response.read.assert_called_once_with(MAX_BLOB_BYTES + 1)


def test_codesearch_rejects_non_object_json():
    provider = CodesearchProvider(ProviderConfig("source", "codesearch"), b"test")
    with patch.object(provider, "_request", return_value=b"[]"), pytest.raises(SourceProviderError) as caught:
        provider._request_json("/api/v1/search", {})
    assert caught.value.status_code == 502
