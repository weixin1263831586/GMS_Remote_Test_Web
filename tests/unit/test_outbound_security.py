from unittest.mock import patch

import pytest

from foundation.outbound import (
    UnsafeOutboundURL,
    same_http_origin,
    validate_outbound_url,
)


def test_outbound_url_rejects_private_resolution():
    with patch(
        "foundation.outbound.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("127.0.0.1", 80))],
    ), pytest.raises(UnsafeOutboundURL):
        validate_outbound_url("http://attacker.example/report.zip")


def test_outbound_url_allows_exact_configured_private_host():
    assert validate_outbound_url(
        "https://redmine.internal:8443/attachments/1",
        allowed_private_hosts={"redmine.internal"},
    ).startswith("https://redmine.internal:8443/")


def test_same_origin_rejects_suffix_and_port_confusion():
    assert not same_http_origin(
        "https://redmine.example.com.attacker.invalid/file",
        "https://redmine.example.com",
    )
    assert not same_http_origin(
        "https://redmine.example.com:444/file",
        "https://redmine.example.com",
    )
    assert same_http_origin(
        "https://REDMINE.example.com/issues/1",
        "https://redmine.example.com",
    )
