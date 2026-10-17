"""Unit tests: foundation.redaction 边界行为。

覆盖外部深度评审指出的两类问题：
- 含空格/引号的自由文本凭据此前只在第一个空格前脱敏（P1）；
- ``input_tokens`` 等 token 用量统计字段被误当凭据掩码（P2）。
"""

from __future__ import annotations

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


def test_token_counter_fields_keep_value_and_type():
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
