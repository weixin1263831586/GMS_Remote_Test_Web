import json

from features.assistant import api
from features.assistant.payloads import session_payload
from foundation.security_audit import SecurityAuditLogger


def test_parameters_messages_steps_responses_and_audit_hide_credentials(tmp_path):
    params = {
        'ssid': 'lab', 'password': 'wifi secret with spaces',
        'access_token': 'long-access-value', 'apiKey': 'api-key-value',
        'nested': [{'PrivateKey': 'private-key-value'}],
    }
    secrets = ['wifi secret with spaces', 'long-access-value', 'api-key-value', 'private-key-value']
    text = '参数摘要 ' + ' '.join(secrets)
    session = {'messages': [{'content': text}], 'steps': [], 'pending_plan': {'params': params},
               'context': {'turns': [{'summary': text}]}}
    formatted = api._format_params(params)
    api._append_message(session, 'assistant', formatted, data={'params': params})
    api._append_step(session, '确认', detail=text, data={'params': params})
    assert session['pending_plan']['params']['password'] == secrets[0]
    session['pending_plan'] = None
    audit = SecurityAuditLogger(str(tmp_path / 'audit.jsonl'))
    for result in [formatted, session['messages'], session['steps'], session_payload(session),
                   audit.sanitize_mapping({'params': params, 'detail': text})]:
        public = json.dumps(result, ensure_ascii=False)
        assert all(secret not in public for secret in secrets)
    assert params['password'] == secrets[0]
    assert 'lab' in formatted
    assert all(secret not in json.dumps(session['context']) for secret in secrets)


def test_session_payload_redacts_mixed_case_and_embedded_authorization():
    original = {
        'params': {'Authorization': 'Bearer auth-value', 'refreshToken': 'refresh-value'},
        'messages': [{'content': '返回 refresh-value; Authorization: Bearer auth-value'}],
    }
    public = json.dumps(session_payload(original))
    assert 'auth-value' not in public
    assert 'refresh-value' not in public
    assert original['params']['refreshToken'] == 'refresh-value'


def test_nested_secret_containers_also_redact_free_text():
    public = session_payload({'tokens': ['first-secret', {'value': 'second-secret'}],
                              'message': 'first-secret second-secret'})
    assert 'first-secret' not in json.dumps(public)
    assert 'second-secret' not in json.dumps(public)


def test_immediate_tool_result_redacts_execution_only_credentials():
    session = {'messages': [], 'steps': [], 'context': {'last_result_summary': 'secret with spaces'}}
    params = {'password': 'secret with spaces'}
    api._append_message(session, 'assistant', 'Result: secret with spaces', private_params=params)
    assert 'secret with spaces' not in json.dumps(session)
    assert params['password'] == 'secret with spaces'
