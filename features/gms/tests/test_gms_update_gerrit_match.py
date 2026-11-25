"""Critical Fix matching uses a full SHA or matching project and subject, not a bug ID."""

import asyncio
import json
import sqlite3

import pytest

from features.gms import mainline_queries as mainline_api
from features.gms.gerrit_match import (
    bug_number,
    change_matches_patch,
    classify_patch,
    clear_gerrit_match_cache,
    match_critical_patches,
    text_has_bug,
    text_has_sha,
    token_query,
    upstream_commits,
    upstream_patches,
)


SHA = 'a' * 40
OTHER = 'b' * 40
NEIGHBOR = 'c' * 40
BUG = '568181732'
CFG = {'base_url': 'https://gerrit.example', 'ssh_host': 'gerrit.example', 'ssh_user': 'bot'}


@pytest.fixture(autouse=True)
def _clear_match_cache():
    clear_gerrit_match_cache()
    yield
    clear_gerrit_match_cache()


def test_upstream_commits_keep_googlesource_shas_only():
    detail = '\n'.join((
        f'https://partner-android.googlesource.com/platform%2Fframeworks%2Fbase/+/{SHA}%5E%21',
        f'https://android.googlesource.com/platform/frameworks/av/+/{SHA}',
        f'https://partner-android.googlesource.com/platform/frameworks/av/+/{OTHER}',
        'https://drive.google.com/file/d/download',
        'https://r.android.com/3151227',
    ))
    found = upstream_commits(detail)
    assert found == [
        {'sha': SHA, 'project': 'platform/frameworks/base'},
        {'sha': OTHER, 'project': 'platform/frameworks/av'},
    ]


def test_bug_and_sha_matches_stop_at_token_boundaries():
    assert bug_number('A-568181732') == BUG
    assert bug_number('A-12') == ''
    assert text_has_bug('Bug: 568181732\n', BUG)
    assert not text_has_bug('Bug: 5681817329\n', BUG)
    assert text_has_sha(f'cherry picked from commit {SHA}', SHA)
    assert not text_has_sha(SHA + 'a', SHA)


def test_token_query_is_parenthesized_and_rejects_free_text():
    query = token_query([SHA, BUG])
    assert query == (
        f'(commit:{SHA} OR message:{SHA} OR message:{BUG}) '
        '(status:merged OR status:open)'
    )
    with pytest.raises(ValueError):
        token_query(['Bluetooth correction', 'A-12'])


def _rows(*pairs):
    return [{'sha': sha, 'project': 'platform/frameworks/av', 'subject': ''} for sha in pairs]


def test_classify_patch_counts_each_upstream_commit():
    merged = {'number': '1', 'status': 'MERGED', 'branch': 'vendor', 'via': ['commit']}
    opened = {'number': '2', 'status': 'NEW', 'via': ['commit']}
    merged_all = classify_patch(
        patches=_rows(SHA, OTHER), hits={SHA: [merged], OTHER: [merged]}, truncated=set(),
    )
    assert merged_all['status'] == 'merged'
    partial = classify_patch(patches=_rows(SHA, OTHER), hits={SHA: [merged], OTHER: []}, truncated=set())
    assert partial['status'] == 'partial'
    assert partial['upstream_merged'] == 1 and partial['upstream_total'] == 2
    assert classify_patch(patches=_rows(SHA), hits={SHA: [opened]}, truncated=set())['status'] == 'open'
    # A merged change that only mentions the bug is not a hit for this commit.
    assert classify_patch(patches=_rows(SHA), hits={SHA: []}, truncated=set())['status'] == 'missing'
    assert classify_patch(patches=_rows(SHA), hits={SHA: []}, truncated={SHA})['status'] == 'unknown'
    assert classify_patch(patches=[], hits={}, truncated=set())['status'] == 'unknown'


def _patch(detail, patch_id='A-568181732'):
    return [{'patch_id': patch_id, 'detail_text': detail}]


def _change(number, status, message, subject=''):
    return {
        'number': number,
        'status': status,
        'project': 'internal/frameworks/av',
        'branch': 'vendor',
        'subject': subject or 'bluetooth',
        'url': f'https://gerrit.example/c/{number}',
        'commitMessage': message,
        'currentPatchSet': {'revision': NEIGHBOR},
    }


def test_match_counts_cherry_pick_and_rejects_neighbor_bug():
    detail = f'https://partner-android.googlesource.com/platform/frameworks/av/+/{SHA}'
    calls = []

    async def query(_cfg, query, _limit, _page_size, commit_message=False):
        calls.append(query)
        assert commit_message is True
        if SHA in query:
            message = f'cherry picked from commit {SHA}\nBug: {BUG}\n'
            return {'items': [_change(7, 'MERGED', message)], 'stats': {}, 'error': ''}
        return {'items': [], 'stats': {}, 'error': ''}

    payload = asyncio.run(match_critical_patches(_patch(detail), CFG, query))
    assert payload['configured'] is True
    row = payload['items']['A-568181732']
    assert row['status'] == 'merged'
    assert row['changes'][0]['number'] == '7'
    assert calls
    assert 'SECRET' not in str(payload)
    assert 'detail_text' not in str(payload)


def test_neighbor_only_change_is_not_merged_and_hidden_body_is_unknown():
    detail = f'https://android.googlesource.com/platform/frameworks/av/+/{SHA}'

    async def neighbor(_cfg, query, _limit, _page_size, commit_message=False):
        assert BUG in query or SHA in query
        return {'items': [_change(9, 'MERGED', f'Bug: {BUG}9\n', subject=f'Fix {BUG}9')], 'stats': {}, 'error': ''}

    missed = asyncio.run(match_critical_patches(_patch(detail), CFG, neighbor))
    assert missed['items']['A-568181732']['status'] == 'missing'

    async def hidden_body(_cfg, _query, _limit, _page_size, commit_message=False):
        return {'items': [{
            'number': 11, 'status': 'MERGED', 'subject': 'bluetooth',
            'project': 'internal/av', 'branch': 'vendor', 'url': 'https://gerrit.example/c/11',
        }], 'stats': {}, 'error': ''}

    found = asyncio.run(match_critical_patches(_patch(detail), CFG, hidden_body, refresh=True))
    assert found['items']['A-568181732']['status'] == 'unknown'
    assert found['items']['A-568181732']['changes'] == []


def test_truncated_single_token_is_unknown_and_errors_are_not_cached():
    detail = f'https://android.googlesource.com/platform/frameworks/av/+/{SHA}'
    calls = {'n': 0}

    async def truncated(_cfg, _query, _limit, _page_size, commit_message=False):
        calls['n'] += 1
        return {'items': [], 'stats': {'moreChanges': True}, 'error': ''}

    first = asyncio.run(match_critical_patches(_patch(detail), CFG, truncated))
    assert first['items']['A-568181732']['status'] == 'unknown'
    assert first['cached'] is False
    second = asyncio.run(match_critical_patches(_patch(detail), CFG, truncated))
    assert second['cached'] is True
    assert calls['n'] == 1

    async def broken(_cfg, _query, _limit, _page_size, commit_message=False):
        calls['n'] += 1
        raise RuntimeError('gerrit down ' + ('x' * 400))

    clear_gerrit_match_cache()
    failed = asyncio.run(match_critical_patches(_patch(detail), CFG, broken, refresh=True))
    assert 'gerrit down' in failed['error']
    assert len(failed['error']) <= 300
    assert failed['cached'] is False
    again = asyncio.run(match_critical_patches(_patch(detail), CFG, broken, refresh=False))
    assert again['cached'] is False
    assert 'gerrit down' in again['error']


def test_same_subject_and_project_match_without_using_the_bug_number():
    base = '98178ed0b877e544d8cc73b4386b06fd97f09c73'
    build = '6e702f59fd6b84992a653c376227604b446d23fe'
    detail = '\n'.join((
        'A-528269366 Fix system server crash',
        'Android version Subject Project Download '
        '17 Fix potential double execution of AppServiceCallback platform/frameworks/base Download '
        '17 Register backported fix for AllowlistService system server crash platform/build Download',
        f'https://partner-android.googlesource.com/platform/frameworks/base/+/{base}',
        f'https://partner-android.googlesource.com/platform/build/+/{build}',
    ))
    parsed = upstream_patches(detail)
    assert [item['project'] for item in parsed] == ['platform/frameworks/base', 'platform/build']
    assert parsed[0]['subject'] == 'Fix potential double execution of AppServiceCallback'
    bug_only = _change(
        332774, 'MERGED', 'builds that apply A-528269366 also report the fix\nBug: 530305604\n',
        subject='Register backported fix for AllowlistService system server crash.',
    )
    bug_only['project'] = 'android/platform/build'
    bug_only['branch'] = 'rk35/mid/17.0/develop'
    base_change = _change(
        332766, 'MERGED', 'Bug: 520461787\n',
        subject='Fix potential double execution of AppServiceCallback',
    )
    base_change['project'] = 'android/frameworks/base'
    base_change['branch'] = 'rk35/mid/17.0/develop'
    assert change_matches_patch(bug_only, parsed[0]) == ''
    assert change_matches_patch(bug_only, parsed[1]) == 'subject'
    assert change_matches_patch(base_change, parsed[0]) == 'subject'
    calls = []

    async def query(_cfg, query, _limit, _page_size, commit_message=False):
        calls.append(query)
        assert 'message:528269366' not in query
        items = []
        if 'AppServiceCallback' in query:
            items.append(base_change)
        if 'AllowlistService' in query:
            items.append(bug_only)
        return {'items': items, 'stats': {}, 'error': ''}

    payload = asyncio.run(match_critical_patches(
        [{'patch_id': 'A-528269366', 'detail_text': detail}], CFG, query, refresh=True,
    ))
    row = payload['items']['A-528269366']
    assert row['status'] == 'merged'
    assert row['upstream_merged'] == 2 and row['upstream_total'] == 2
    assert [item['changes'][0]['number'] for item in row['upstreams']] == ['332766', '332774']
    assert calls and all('528269366' not in call for call in calls)


def test_unconfigured_gerrit_does_not_query():
    async def fail(*_args, **_kwargs):
        raise AssertionError('query must not run')

    payload = asyncio.run(match_critical_patches(_patch(''), {}, fail))
    assert payload['configured'] is False
    assert payload['items'] == {}


def _bulletin_db(path, detail):
    conn = sqlite3.connect(path)
    conn.execute(
        '''
        CREATE TABLE gms_update_bulletin_patches (
            source_key TEXT NOT NULL, item_key TEXT NOT NULL, year TEXT NOT NULL,
            patch_id TEXT NOT NULL, patch_kind TEXT NOT NULL DEFAULT '',
            android_versions TEXT NOT NULL DEFAULT '', description TEXT NOT NULL,
            bulletin_month TEXT NOT NULL, deadline TEXT NOT NULL,
            bulletin_url TEXT NOT NULL DEFAULT '', detail_text TEXT NOT NULL DEFAULT '',
            detail_status TEXT NOT NULL DEFAULT 'not_scanned',
            detail_fetched_at TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            PRIMARY KEY (source_key, item_key)
        )
        '''
    )
    conn.execute(
        '''
        INSERT INTO gms_update_bulletin_patches VALUES (
            'gms_bulletins', 'A-568181732', '2026', 'A-568181732', 'mandatory', '17',
            'Bluetooth', 'November', '2027-03-26', 'https://docs.example/cf', ?,
            'ready', '', 'hash', 't', 't'
        )
        ''',
        (detail,),
    )
    conn.commit()
    conn.close()


def test_gerrit_route_does_not_echo_bulletin_text(tmp_path, monkeypatch):
    detail = f'SECRET_BULLETIN_BODY https://partner-android.googlesource.com/platform/frameworks/av/+/{SHA}'
    _bulletin_db(tmp_path / 'gms_update.sqlite3', detail)
    monkeypatch.setattr(mainline_api, 'DB_PATH', tmp_path / 'gms_update.sqlite3')
    queries = []

    class Manager:
        def for_owner(self, owner_id):
            assert owner_id == 'owner-1'
            return self

        def get_gerrit_dashboard_config(self):
            return dict(CFG)

    async def query(_cfg, query, _limit, _page_size, commit_message=False):
        queries.append(query)
        assert commit_message is True
        assert 'SECRET_BULLETIN_BODY' not in query
        assert 'Bluetooth' not in query
        return {'items': [dict(_change(7, 'MERGED', f'cherry picked from commit {SHA}\n'),
                               branch='rk35/mid/17.0/develop')], 'stats': {}, 'error': ''}

    monkeypatch.setattr('features.users.owner_id_from_request', lambda _request: 'owner-1')
    monkeypatch.setattr(mainline_api, '_gerrit_config_provider',
                        lambda owner: Manager().for_owner(owner).get_gerrit_dashboard_config())
    monkeypatch.setattr(mainline_api, '_gerrit_query_provider', query)
    payload = asyncio.run(mainline_api.mainline_critical_patch_gerrit(object(), q='', refresh=1))
    assert payload['success'] is True
    assert payload['available'] is True
    assert payload['items']['A-568181732']['status'] == 'merged'
    dumped = str(payload)
    assert 'SECRET_BULLETIN_BODY' not in dumped
    assert 'detail_text' not in dumped
    assert queries


def test_gerrit_route_reports_unconfigured_and_query_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(mainline_api, 'DB_PATH', tmp_path / 'gms_update.sqlite3')
    monkeypatch.setattr('features.users.owner_id_from_request', lambda _request: 'owner-1')

    class Bare:
        def for_owner(self, _owner_id):
            return self

        def get_gerrit_dashboard_config(self):
            return {'base_url': '', 'ssh_host': ''}

    monkeypatch.setattr(mainline_api, '_gerrit_config_provider',
                        lambda owner: Bare().for_owner(owner).get_gerrit_dashboard_config())

    async def fail(*_args, **_kwargs):
        raise AssertionError('query must not run')

    monkeypatch.setattr(mainline_api, '_gerrit_query_provider', fail)
    missing = asyncio.run(mainline_api.mainline_critical_patch_gerrit(object(), q='', refresh=0))
    assert missing['success'] is True
    assert missing['available'] is False
    assert missing['configured'] is False

    _bulletin_db(tmp_path / 'gms_update.sqlite3', 'no commits')

    class Boom:
        def for_owner(self, _owner_id):
            return self

        def get_gerrit_dashboard_config(self):
            raise RuntimeError('token exploded ' + ('y' * 400))

    monkeypatch.setattr(mainline_api, '_gerrit_config_provider',
                        lambda owner: Boom().for_owner(owner).get_gerrit_dashboard_config())
    failed = asyncio.run(mainline_api.mainline_critical_patch_gerrit(object(), q='', refresh=0))
    assert failed['success'] is True
    assert 'token exploded' in failed['error']
    assert len(failed['error']) <= 300
    assert failed['items'] == {}


def test_subject_only_rows_and_android_branch_scope():
    detail = (
        'Android version Subject Project Download '
        '17 Fix potential double execution of AppServiceCallback platform/frameworks/base Download'
    )
    wrong = _change(1, 'MERGED', '', 'Fix potential double execution of AppServiceCallback')
    wrong.update(project='android/frameworks/base', branch='rk35/mid/16.0/develop')
    right = dict(wrong, number=2, branch='rk35/mid/17.0/develop')

    async def query(_cfg, query, _limit, _page_size, commit_message=False):
        assert 'subject:' in query
        return {'items': [wrong], 'stats': {}, 'error': ''}

    row = asyncio.run(match_critical_patches(_patch(detail), CFG, query))['items']['A-568181732']
    assert row['status'] == 'missing'

    async def query_right(_cfg, _query, _limit, _page_size, commit_message=False):
        return {'items': [wrong, right], 'stats': {}, 'error': ''}

    row = asyncio.run(match_critical_patches(
        _patch(detail), CFG, query_right, refresh=True,
    ))['items']['A-568181732']
    assert row['status'] == 'merged'
    assert row['changes'][0]['number'] == '2'


def test_failed_batch_isolated_and_later_patch_still_matches():
    detail = '\n'.join((
        f'https://android.googlesource.com/platform/frameworks/av/+/{SHA}',
        f'https://android.googlesource.com/platform/frameworks/av/+/{OTHER}',
    ))

    async def query(_cfg, query, _limit, _page_size, commit_message=False):
        if SHA in query:
            return {'items': [], 'stats': {}, 'error': "no viable alternative at character '['"}
        return {'items': [_change(2, 'MERGED', f'cherry picked from commit {OTHER}')],
                'stats': {}, 'error': ''}

    row = asyncio.run(match_critical_patches(_patch(detail), CFG, query))['items']['A-568181732']
    assert row['status'] == 'partial'
    assert [item['status'] for item in row['upstreams']] == ['unknown', 'merged']


def test_same_version_patches_on_different_branches_are_not_complete():
    detail = (
        'Android version Subject Project Download '
        '17 Fix potential double execution of AppServiceCallback platform/frameworks/base Download '
        '17 Register backported fix for AllowlistService system server crash platform/build Download'
    )
    first = _change(1, 'MERGED', '', 'Fix potential double execution of AppServiceCallback')
    first.update(project='android/platform/frameworks/base', branch='rk35/mid/17.0/develop')
    second = _change(2, 'MERGED', '', 'Register backported fix for AllowlistService system server crash')
    second.update(project='android/platform/build', branch='rk35/other/17.0/develop')

    async def query(*_args, **_kwargs):
        return {'items': [first, second], 'stats': {}, 'error': ''}

    row = asyncio.run(match_critical_patches(_patch(detail), CFG, query))['items']['A-568181732']
    assert row['status'] == 'partial'
    assert row['branch_coverage_complete'] is False


@pytest.mark.parametrize(
    ('second_branch', 'status'),
    [('android14', 'merged'), ('android15', 'partial'), ('', 'partial')],
)
def test_versionless_patches_require_a_shared_merge_branch(second_branch, status):
    detail = '\n'.join(
        f'https://android.googlesource.com/platform/frameworks/av/+/{revision}'
        for revision in (SHA, OTHER)
    )

    async def query(*_args, **_kwargs):
        first = dict(_change(1, 'MERGED', f'cherry picked from commit {SHA}'), branch='android14')
        second = dict(_change(2, 'MERGED', f'cherry picked from commit {OTHER}'), branch=second_branch)
        return {'items': [first, second], 'stats': {}, 'error': ''}

    row = asyncio.run(match_critical_patches(_patch(detail), CFG, query))['items']['A-568181732']
    assert row['status'] == status
    assert row['branch_coverage_complete'] is (status == 'merged')
    assert row['versions'] == []
    assert row['upstream_merged'] == row['upstream_total'] == 2


def test_version_rows_never_share_merge_credit_between_android_releases():
    subject = '[audiopolicy] Fix loopback device lookup in getOutputDeviceForMix'
    rows = [
        {'version': version, 'subject': subject, 'project': 'platform/frameworks/av',
         'urls': [f'https://android.googlesource.com/platform/frameworks/av/+/{SHA}']}
        for version in ('16', '16 QPR2', '17')
    ]
    patch = dict(_patch('')[0], android_versions='Android 16, Android 16 QPR2, Android 17',
                 patch_rows_json=json.dumps(rows))
    change = dict(_change(17, 'MERGED', f'cherry picked from commit {SHA}', subject),
                  branch='rk35/mid/17.0/develop')

    async def query(*_args, **_kwargs):
        return {'items': [change], 'stats': {}, 'error': ''}

    result = asyncio.run(match_critical_patches([patch], CFG, query))['items']['A-568181732']
    assert result['status'] == 'partial'
    assert [(v['version'], v['status'], v['upstream_merged'], v['upstream_total'])
            for v in result['versions']] == [
        ('16', 'missing', 0, 1), ('16 QPR2', 'missing', 0, 1), ('17', 'merged', 1, 1),
    ]


def test_version_without_evidence_does_not_inherit_merged_status():
    subject = 'Fix potential double execution of AppServiceCallback'
    patch = dict(_patch('')[0], android_versions='Android 16, Android 17',
                 patch_rows_json=json.dumps([{'version': '17', 'subject': subject,
                                              'project': 'platform/frameworks/base', 'urls': []}]))
    change = dict(_change(17, 'MERGED', '', subject), project='android/frameworks/base',
                  branch='rk35/mid/17.0/develop')

    async def query(*_args, **_kwargs):
        return {'items': [change], 'stats': {}, 'error': ''}

    result = asyncio.run(match_critical_patches([patch], CFG, query))['items']['A-568181732']
    assert result['status'] == 'partial'
    assert result['versions'][0]['status'] == 'unknown'
    assert result['versions'][1]['status'] == 'merged'


def test_repeated_patch_id_keeps_independent_month_record_status():
    patches = [
        dict(_patch(f'https://android.googlesource.com/platform/frameworks/av/+/{SHA}')[0],
             record_key='sep', android_versions='17'),
        dict(_patch(f'https://android.googlesource.com/platform/frameworks/av/+/{OTHER}')[0],
             record_key='nov', android_versions='17'),
    ]

    async def query(*_args, **_kwargs):
        return {'items': [dict(_change(17, 'MERGED', f'cherry picked from commit {SHA}'),
                               branch='rk35/mid/17.0/develop')], 'stats': {}, 'error': ''}

    result = asyncio.run(match_critical_patches(patches, CFG, query, refresh=True))['items']
    assert result['sep']['status'] == 'merged'
    assert result['nov']['status'] == 'missing'


def test_obsolete_rest_revision_is_not_current_commit_evidence():
    raw = {
        'current_revision': OTHER,
        'revisions': {NEIGHBOR: {'commit': {'message': f'cherry picked from commit {SHA}'}}},
        'subject': 'Unrelated change',
    }
    assert change_matches_patch(raw, _rows(SHA)[0]) == ''


def test_hidden_body_with_rewritten_subject_is_unknown():
    subject = 'Fix potential double execution of AppServiceCallback'
    patch = dict(_patch('')[0], patch_rows_json=json.dumps([
        {'version': '17', 'subject': subject, 'project': 'platform/frameworks/base',
         'urls': [f'https://android.googlesource.com/platform/frameworks/base/+/{SHA}']},
    ]))
    change = dict(_change(17, 'MERGED', '', 'Vendor rewritten subject'),
                  branch='rk35/mid/17.0/develop', project='android/frameworks/base')

    async def query(*_args, **_kwargs):
        return {'items': [change], 'stats': {}, 'error': ''}

    row = asyncio.run(match_critical_patches([patch], CFG, query))['items']['A-568181732']
    assert row['status'] == 'unknown'
    assert row['truncated'] is True


def test_match_cache_is_bounded_and_connection_changes_invalidate(monkeypatch):
    from features.gms import gerrit_match

    monkeypatch.setattr(gerrit_match, '_CACHE_MAX_ENTRIES', 2)
    detail = f'https://android.googlesource.com/platform/frameworks/av/+/{SHA}'
    calls = []

    async def query(*_args, **_kwargs):
        calls.append(True)
        return {'items': [], 'stats': {}, 'error': ''}

    for password in ('old', 'new'):
        cfg = dict(CFG, rest_username='bot', rest_password=password)
        result = asyncio.run(match_critical_patches(_patch(detail), cfg, query))
        assert result['cached'] is False
        assert asyncio.run(match_critical_patches(_patch(detail), cfg, query))['cached'] is True
    cfg = dict(CFG, rest_username='bot', rest_password='third')
    assert asyncio.run(match_critical_patches(_patch(detail), cfg, query))['cached'] is False
    assert len(gerrit_match._cache) == 2
    cfg = dict(CFG, rest_username='bot', rest_password='old')
    assert asyncio.run(match_critical_patches(_patch(detail), cfg, query))['cached'] is False
    assert len(calls) == 4


def test_match_cache_removes_expired_entries(monkeypatch):
    from features.gms import gerrit_match

    detail = f'https://android.googlesource.com/platform/frameworks/av/+/{SHA}'

    async def query(*_args, **_kwargs):
        return {'items': [], 'stats': {}, 'error': ''}

    asyncio.run(match_critical_patches(_patch(detail), CFG, query))
    expired_key = next(iter(gerrit_match._cache))
    monkeypatch.setattr(gerrit_match, '_CACHE_TTL_SECONDS', 0)
    asyncio.run(match_critical_patches(_patch(detail, 'A-123456'), CFG, query))
    assert expired_key not in gerrit_match._cache
    assert len(gerrit_match._cache) == 1
