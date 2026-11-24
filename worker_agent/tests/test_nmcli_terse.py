from foundation.networking import parse_vpn_connection_names


def test_vpn_parser_handles_nmcli_escaping_and_active_state():
    output = "\n".join([r"a\:b:vpn:activated", r"a\\b:vpn", "tailscale:tun:activated"])
    assert parse_vpn_connection_names(output) == ["a:b", r"a\b"]
