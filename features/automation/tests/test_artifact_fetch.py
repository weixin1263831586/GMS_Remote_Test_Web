"""Jenkins artifact downloads stay on the configured origin."""

from unittest.mock import MagicMock, patch

import pytest

from features.automation.artifact_fetch import open_jenkins_artifact


def test_cross_origin_redirect_is_not_contacted():
    redirected = MagicMock(status_code=302)
    redirected.headers = {'Location': 'http://169.254.169.254/latest/meta-data'}
    with (
        patch(
            'features.automation.artifact_fetch.requests.get',
            return_value=redirected,
        ) as get,
        pytest.raises(ValueError, match='outside its configured origin'),
    ):
        open_jenkins_artifact(
            'https://jenkins.example/job/a/artifact/update.img',
            configured_url='https://jenkins.example/',
            auth=('user', 'token'),
            verify=True,
        )

    get.assert_called_once()
    assert get.call_args.kwargs['allow_redirects'] is False
    redirected.close.assert_called_once()


def test_same_origin_redirect_is_followed_with_auth():
    redirected = MagicMock(status_code=302)
    redirected.headers = {'Location': '/job/a/artifact/update.img'}
    body = MagicMock(status_code=200)
    body.headers = {}
    with patch(
        'features.automation.artifact_fetch.requests.get',
        side_effect=[redirected, body],
    ) as get:
        response = open_jenkins_artifact(
            'https://jenkins.example/alias',
            configured_url='https://jenkins.example/',
            auth=('user', 'token'),
            verify=True,
        )

    assert response is body
    assert get.call_count == 2
    assert get.call_args_list[1].args[0] == 'https://jenkins.example/job/a/artifact/update.img'
    assert get.call_args_list[1].kwargs['auth'] == ('user', 'token')
    redirected.close.assert_called_once()
