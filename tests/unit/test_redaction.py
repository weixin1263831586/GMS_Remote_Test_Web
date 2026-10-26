"""Unit tests: foundation.redaction 边界行为。

覆盖外部深度评审指出的两类问题：
- 含空格/引号的自由文本凭据此前只在第一个空格前脱敏（P1）；
- ``input_tokens`` 等 token 用量统计字段被误当凭据掩码（P2）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import foundation.redaction as redaction
from foundation.redaction import redact_sensitive_data, redact_sensitive_text


def test_quoted_value_with_spaces_is_fully_redacted():
    text = 'payload={"password":"alpha beta suffix"} done'
    out = redact_sensitive_text(text)
    assert 'alpha' not in out and 'beta' not in out and 'suffix' not in out
    assert out.startswith('payload={"password":"***REDACTED***"} done')


def test_single_quoted_and_bare_key_quoted_values_are_redacted():
    for text in (
        "password='my secret phrase' ok",
        'password: "three words here" ok',
        "'password'='quoted key value' ok",
    ):
        out = redact_sensitive_text(text)
        assert 'secret phrase' not in out
        assert 'three words' not in out
        assert 'quoted key value' not in out


def test_unquoted_values_keep_original_behavior():
    out = redact_sensitive_text('password=abc123, next=keep')
    assert 'abc123' not in out
    assert 'keep' in out


def test_json_string_is_redacted_structurally():
    raw = '{"username": "u1", "password": "alpha beta", "n": 3}'
    out = redact_sensitive_text(raw)
    assert 'alpha beta' not in out
    assert '"username": "u1"' in out
    assert '"n": 3' in out


def test_url_and_bearer_and_openai_style_still_redacted():
    out = redact_sensitive_text(
        'https://user:pw@host/x and Authorization: Bearer abc.def and key sk-abc123'
    )
    assert 'user:pw' not in out
    assert 'abc.def' not in out
    assert 'sk-abc123' not in out


def test_embedded_json_fragment_in_error_prefix_is_redacted():
    """「错误前缀 + JSON 片段」不再依赖整段可解析：复合键 access_token 命中。"""
    text = 'Request failed: {"access_token":"very secret"}'
    out = redact_sensitive_text(text)
    assert 'very secret' not in out
    assert 'Request failed:' in out


def test_escaped_quote_does_not_truncate_quoted_secret():
    """值中的转义引号（\\\"）不再提前结束匹配，后续敏感字符全部遮蔽。"""
    text = 'error: password="ab\\"cd ef" tail'
    out = redact_sensitive_text(text)
    assert 'ab' not in out and 'cd ef' not in out
    assert 'tail' in out


def test_composite_token_keys_in_free_text_are_redacted():
    for text in (
        'access_token=abcdef123',
        "refresh_token: 'qq ww ee'",
        'auth_token = zz9x8y',
    ):
        out = redact_sensitive_text(text)
        assert 'abcdef123' not in out and 'qq ww ee' not in out and 'zz9x8y' not in out


def test_embedded_json_fragment_via_structured_path():
    out = redact_sensitive_data(
        {"error": 'Request failed: {"access_token":"very secret", "n": 3}'}
    )
    assert 'very secret' not in out["error"]
    assert '"n": 3' in out["error"]


def test_counter_fields_keep_value_and_type():
    stats = {
        "token_count": 2048,
        "input_tokens": 1500,
        "output_tokens": 548,
        "total_tokens": 2048,
        "prompt_tokens": 100,
        "max_tokens": 4096,
        "tokens_used": 12,
        "used_token_count": 7,
    }
    out = redact_sensitive_data(stats)
    assert out == stats
    assert all(isinstance(out[key], int) for key in stats)


def test_credential_fields_are_still_masked():
    data = {
        "access_token": "tok",
        "api_key": "k",
        "token_id": "tid",
        "password": "p",
        "private_key": "pk",
        "token_count": 5,
    }
    out = redact_sensitive_data(data)
    assert out["access_token"] == '***'
    assert out["api_key"] == '***'
    assert out["token_id"] == '***'
    assert out["password"] == '***'
    assert out["private_key"] == '***'
    assert out["token_count"] == 5


def test_sensitive_dict_value_with_spaces_is_fully_masked():
    out = redact_sensitive_data({"password": "alpha beta gamma"})
    assert out["password"] == '***'
    assert 'alpha' not in str(out)


def test_known_values_are_replaced_in_free_text():
    out = redact_sensitive_data(
        {"error": "connect failed with hunter2", "detail": "x"},
        known_values=["hunter2"],
    )
    assert 'hunter2' not in out["error"]


def test_counter_fields_nested_and_non_string_values_untouched():
    data = {"usage": {"input_tokens": 10, "output_tokens": 2}, "list": [{"total_tokens": 9}]}
    assert redact_sensitive_data(data) == data


def test_long_identifiers_and_values_do_not_stall_redaction():
    # Run in a child so a quadratic-regex regression fails with a bounded
    # timeout instead of hanging the entire test runner.
    code = """
from foundation.redaction import redact_sensitive_text
for text in ('a' * 100_000, 'token' * 20_000, 'safe-' * 20_000):
    assert redact_sensitive_text(text) == text
assert redact_sensitive_text('password=' + 'a' * 100_000) == 'password=***REDACTED***'
"""
    subprocess.run(
        [sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[2],
        timeout=3, check=True, capture_output=True,
    )


def test_ordinary_quoted_message_does_not_hide_embedded_credentials():
    text = '''message='connection failed password="private words" tail' ok=keep'''
    out = redact_sensitive_text(text)
    assert 'private words' not in out
    assert 'connection failed' in out and 'tail' in out and 'ok=keep' in out


def test_hyphenated_keys_and_multiline_quoted_secrets_are_masked():
    text = 'access-token="first\nsecond" total_tokens=42 safe_name=keep'
    out = redact_sensitive_text(text)
    assert 'first' not in out and 'second' not in out
    assert 'total_tokens=42' in out and 'safe_name=keep' in out


def test_unterminated_quoted_secret_is_still_masked():
    assert redact_sensitive_text('password="private words') == 'password="***REDACTED***'


def test_deep_or_oversized_json_preserves_outer_event_and_masks_tail():
    cases = [
        '[' * 1500,
        '{"nested":' * 1100,
        '{"padding":"' + 'x' * 70_000 + '","password":"hidden"}',
    ]
    for payload in cases:
        out = redact_sensitive_data({
            'operation': 'device.read',
            'query': {'q': 'upstream: ' + payload + ' password=hidden'},
        })
        assert out['operation'] == 'device.read'
        assert out['query']['q'].startswith('upstream: ')
        assert 'hidden' not in json.dumps(out)
        assert '***' in out['query']['q']


def test_structured_depth_is_bounded_as_well_as_embedded_json():
    nested = {'password': 'buried-private-value'}
    for _ in range(1500):
        nested = {'nested': nested}
    out = redact_sensitive_data({'operation': 'read', 'data': nested})
    assert out['operation'] == 'read'
    assert 'buried-private-value' not in json.dumps(out)


def test_json_attempt_budget_is_shared_and_never_exposes_escaped_keys(monkeypatch):
    monkeypatch.setattr(redaction, '_MAX_JSON_FRAGMENTS', 2)
    decoder = Mock(wraps=redaction._JSON_DECODER)
    monkeypatch.setattr(redaction, '_JSON_DECODER', decoder)
    out = redact_sensitive_data({
        'first': '{} {}',
        'second': 'response: {"pass\\u0077ord":"private-value"}',
    })
    assert decoder.decode.call_count == 2
    assert 'private-value' not in out['second']
    assert out['second'].startswith('response: ')


def test_json_character_budget_masks_the_unparsed_fragment(monkeypatch):
    monkeypatch.setattr(redaction, '_MAX_JSON_TOTAL_CHARS', 8)
    out = redact_sensitive_data({
        'first': '{}', 'second': '{"pass\\u0077ord":"private-value"}',
    })
    assert out['first'] == '{}'
    assert out['second'] == '***'


def test_invalid_json_with_escaped_keys_is_masked_but_log_tags_survive():
    text = '[WARN] payload=[{"pass\\u0077ord":"private-value"},]'
    out = redact_sensitive_data({'error': text})['error']
    assert out.startswith('[WARN] payload=')
    assert 'private-value' not in out


def test_decoder_recursion_error_falls_back_to_a_mask(monkeypatch):
    decoder = Mock()
    decoder.decode.side_effect = RecursionError('fixture decoder limit')
    monkeypatch.setattr(redaction, '_JSON_DECODER', decoder)
    out = redact_sensitive_data({'error': 'payload=[{"pass\\u0077ord":"private-value"}]'})
    assert out['error'] == 'payload=***'
