"""Keep password reveal markup and asset ownership consistent."""
import re
from pathlib import Path

from lxml import html


ROOT = Path(__file__).resolve().parents[2]
SECRET_FIELDS = {'auth-bootstrap-token', 'settingRedmineApiKey'}


def _classes(element):
    return set(element.get('class', '').split())


def _check_markup(markup, source):
    for field in html.fromstring(markup).xpath('//input[@type="password"]'):
        field_id = field.get('id')
        if field_id in SECRET_FIELDS:
            assert 'password-input-secret' in _classes(field), (source, field_id)
            assert 'password-input-wrap' not in _classes(field.getparent()), (source, field_id)
            continue
        wrapper = field.getparent()
        assert 'password-input-wrap' in _classes(wrapper), (source, field_id)
        buttons = wrapper.xpath('./button')
        assert len(buttons) == 1, (source, field_id)
        button = buttons[0]
        assert 'password-visibility-toggle' in _classes(button), (source, field_id)
        assert button.get('type') == 'button', (source, field_id)
        assert button.get('data-click') == 'togglePasswordVisibility', (source, field_id)
        assert button.get('data-r0') == 'el', (source, field_id)
        assert button.get('aria-controls') == field_id, (source, field_id)
        assert button.get('aria-label') == '显示密码', (source, field_id)
        assert not button.xpath('ancestor::label'), (source, field_id)


def test_all_static_password_inputs_use_shared_control_or_mask_only_policy():
    for source in sorted([*ROOT.glob('web/shell/**/*.html'), *ROOT.glob('features/**/ui/*.html')]):
        markup = source.read_text(encoding='utf-8')
        if 'type="password"' in markup:
            _check_markup(markup, source)


def test_dynamic_build_prompt_uses_same_password_markup():
    source = ROOT / 'features/automation/ui/build-config.js'
    markup = source.read_text(encoding='utf-8')
    match = re.search(r'<span class="password-input-wrap">.*?id="build-password-input".*?</span>', markup, re.S)
    assert match is not None
    _check_markup(match.group(), source)


def test_shared_password_assets_loaded_by_every_password_workspace():
    version = '20261125-unified-password-control'
    shell = ROOT / 'web/shell/components/head.html'
    assert f'/static/js/password-controls.js?v={version}' in shell.read_text(encoding='utf-8')
    assert f'/static/css/form-controls.css?v={version}' in (
        ROOT / 'web/static/css/common.css'
    ).read_text(encoding='utf-8')
    for page in ('cluster', 'gerrit', 'redmine', 'automation'):
        markup = (ROOT / f'features/{page}/ui/page.html').read_text(encoding='utf-8')
        assert markup.count(f'/static/js/password-controls.js?v={version}') == 1
        assert markup.count(f'/static/css/form-controls.css?v={version}') == 1
