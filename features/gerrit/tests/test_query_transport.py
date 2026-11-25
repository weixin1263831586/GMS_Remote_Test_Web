"""Gerrit SSH query transport and quoting regressions."""

import asyncio

from features.gerrit.service import _ssh_gerrit_query_args


SHA = 'a' * 40
CFG = {'base_url': 'https://gerrit.example', 'ssh_host': 'gerrit.example', 'ssh_user': 'bot'}


def test_commit_message_ssh_args_are_opt_in():
    default = _ssh_gerrit_query_args('owner:a@example.com limit:200', start=400)
    assert default == [
        'gerrit', 'query', '--format=JSON', '--submit-records',
        '--start', '400', '--', 'owner:a@example.com limit:200',
    ]
    asked = _ssh_gerrit_query_args(
        f'(commit:{SHA}) (status:merged OR status:open)', commit_message=True,
    )
    assert asked[:5] == ['gerrit', 'query', '--format=JSON', '--submit-records', '--current-patch-set']
    assert '--commit-message' in asked
    assert asked[-1].startswith('(commit:')


def test_ssh_transport_preserves_gerrit_query_quotes(monkeypatch):
    import shlex

    from features.gerrit import service

    query = '(subject:"[Fix] AppBinding callback" OR subject:"Don\'t drop listeners") status:merged'
    monkeypatch.setattr(service, '_gerrit_ssh_prefix', lambda _cfg: ['ssh', 'gerrit.example'])

    class Process:
        returncode = 0

        async def communicate(self):
            return b'{"type":"stats","rowCount":0}\\n', b''

    async def spawn(*cmd, **_kwargs):
        # OpenSSH joins remote argv; the Gerrit SSH parser consumes one
        # quoting layer before the actual change-query parser runs.
        assert shlex.split(' '.join(cmd[2:]))[-1] == query
        return Process()

    monkeypatch.setattr(service.asyncio, 'create_subprocess_exec', spawn)
    result = asyncio.run(service._query_gerrit_via_ssh_once(CFG, query, commit_message=True))
    assert not result['error']
