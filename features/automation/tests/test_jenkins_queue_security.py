"""Jenkins credentials remain bound to the configured server and account."""
import json

import pytest

from features.automation.executors import _jenkins_config_from_run
from features.automation.jenkins_client import JenkinsClient

from .test_integrations import FakeResponse, FakeSession


@pytest.mark.parametrize('url', [
    'https://other.example/jenkins/queue/item/9/', '//other.example/jenkins/queue/item/9/',
    'http://jenkins/jenkins/queue/item/9/', 'https://user:pass@jenkins/jenkins/queue/item/9/',
    'https://jenkins/other/queue/item/9/', '/jenkins/script', '',
    '/jenkins/queue/item/9/?redirect=elsewhere', 'https://[broken/queue/item/9/',
])
def test_untrusted_queue_location_never_uses_authenticated_transport(url):
    session = FakeSession()
    client = JenkinsClient({'base_url': 'https://jenkins/jenkins', 'username': 'build', 'api_token': 'test-only'},
                           session=session)
    assert client.get_queue_item(url)['success'] is False
    assert session.calls == []


@pytest.mark.parametrize('url', [
    'https://jenkins/jenkins/queue/item/9/', '/jenkins/queue/item/9/', 'queue/item/9/',
])
def test_queue_location_under_configured_context_supports_relative_urls(url):
    session = FakeSession()
    session.queue(FakeResponse(json_data={'executable': {'number': 42}}))
    client = JenkinsClient({'base_url': 'https://jenkins/jenkins'}, session=session)
    assert client.get_queue_item(url)['build_number'] == '42'
    assert session.calls[0][1] == 'https://jenkins/jenkins/queue/item/9/api/json'
    assert session.calls[0][2]['allow_redirects'] is False


def test_queue_redirect_is_an_explicit_failure_without_another_request():
    session = FakeSession()
    session.queue(FakeResponse(status_code=302, headers={'Location': 'https://other.example/'}))
    result = JenkinsClient({'base_url': 'https://jenkins'}, session=session).get_queue_item('/queue/item/9/')
    assert result['success'] is False
    assert 'redirect' in result['error']
    assert len(session.calls) == 1


@pytest.mark.parametrize('overrides', [
    {'base_url': 'https://other.example/jenkins'}, {'base_url': 'http://jenkins/jenkins'},
    {'base_url': 'https://jenkins/other'}, {'username': 'different-account'},
])
def test_run_cannot_reuse_fallback_credentials_for_another_target_or_account(overrides):
    fallback = {'base_url': 'https://jenkins/jenkins', 'username': 'build', 'api_token': 'test-only'}
    config = _jenkins_config_from_run({'test_plan_json': json.dumps({'jenkins': overrides})}, fallback)
    assert not config.get('api_token') and not config.get('token')
    assert fallback['api_token'] == 'test-only'


def test_same_server_keeps_fallback_credentials_but_explicit_token_replaces_both_aliases():
    fallback = {'base_url': 'https://jenkins/jenkins', 'username': 'build', 'api_token': 'old'}
    run = {'test_plan_json': '{"jenkins":{"base_url":"https://jenkins/jenkins/"}}'}
    assert _jenkins_config_from_run(run, fallback)['api_token'] == 'old'
    run['test_plan_json'] = '{"jenkins":{"token":"new"}}'
    config = _jenkins_config_from_run(run, fallback)
    assert 'api_token' not in config and config['token'] == 'new'
