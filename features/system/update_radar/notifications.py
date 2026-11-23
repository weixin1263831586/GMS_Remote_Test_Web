"""Plain-text compatibility summary and safely escaped table-based HTML mail."""

import json
from datetime import datetime
from html import escape
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from .models import CATEGORIES


def build_email(run, changes):
    summary = run['summary']
    zone = ZoneInfo(run['config']['timezone'])

    def local_time(value):
        return datetime.fromisoformat(value).astimezone(zone).strftime('%Y-%m-%d %H:%M:%S') + f' ({zone.key})' if value else '—'
    count = summary.get('changes_total', 0)
    subject = f"[GMS更新雷达] 扫描 #{run['id']} · {count} 项变更"
    if summary.get('errors'):
        subject += ' · 扫描未完整完成'
    lines = [subject, f"开始：{local_time(run['started_at'])}", f"结束：{local_time(run['finished_at'])}",
             '对比：本次扫描前的平台数据 → 本次成功扫描后的数据。', '']
    tasks = summary.get('tasks', run['config']['tasks'])
    lines += ['监听任务：' + '、'.join(CATEGORIES[key] for key in tasks), '']
    if summary.get('initial_baseline'):
        lines += ['首次建立基线：扫描前数据库为空，本轮成功获取的条目按新增记录。', '']
    for category in tasks:
        label = CATEGORIES[category]
        values = summary.get('categories', {}).get(category)
        if values:
            group = 'exemptions' if category == 'exemptions' else 'updates'
            if group in summary.get('errors', {}):
                lines.append(f"{label}：扫描失败，保留 {values['before']} 项原数据")
            else:
                lines.append(f"{label}：{values['before']} → {values['after']}，"
                             f"新增 {values['added']} / 修改 {values['modified']} / 移除 {values['removed']}")
    for group, error in summary.get('errors', {}).items():
        name = {'updates': 'GMS更新', 'exemptions': 'Mainline包豁免项', 'radar': '雷达任务'}.get(group, group)
        lines += ['', f'扫描失败 [{name}]：{error}', '本组保留扫描前数据，不能据此认定无更新。']
    lines += ['', '变更明细（最多展示前 50 条，完整记录见雷达页面）：']
    kinds = {'added': '新增', 'modified': '修改', 'removed': '移除'}
    for item in changes[:50]:
        old, new = item['before_json'] or {}, item['after_json'] or {}
        lines.append(f"\n{CATEGORIES[item['category']]} · {kinds[item['change_type']]}")
        for key in sorted(old.keys() | new.keys()):
            if old.get(key) != new.get(key):
                before = json.dumps(old.get(key), ensure_ascii=False)[:300]
                after = json.dumps(new.get(key), ensure_ascii=False)[:300]
                lines.append(f'  {key}: {before} → {after}')
    url = run['config'].get('dashboard_url')
    if url:
        parts = urlsplit(str(url))
        query = dict(parse_qsl(parts.query))
        query['run'] = str(run['id'])
        lines += ['', '完整记录：' + urlunsplit(parts._replace(query=urlencode(query)))]
    return subject, '\n'.join(lines)


# Inline colors work in Outlook and webmail; every upstream value is escaped.
_HUES = {
    'added': ('新增', '#137a4b', '#e9f8f0'),
    'modified': ('修改', '#a26608', '#fff4d8'),
    'removed': ('移除', '#b4233b', '#fff0f2'),
}
_SKIP_FIELDS = {'id', 'source_key', 'row_key', 'row_index', 'table_index',
                'section_key', 'headers_json', 'values_json', 'item_key',
                'content_hash', 'first_seen_at', 'last_seen_at'}
_LABELS = {'file_name': '文件', 'title': '名称', 'test_case': '测试用例',
           'test_module': '测试模块', 'package_name': '包名',
           'description': '说明', 'remark': '备注', 'section_title': '章节',
           'row_text': '要求内容', 'issue_text': '问题描述',
           'patch_id': '补丁编号', 'month_label': '月份', 'version': '版本',
           'product_section': '产品分类', 'exemption_id': '豁免编号'}


def _short(value, limit=130):
    """Visible business text, never raw JSON dumps or HTML from upstream."""
    if value is None:
        return '—'
    if isinstance(value, (dict, list)):
        value = ', '.join(str(x) for x in value) if isinstance(value, list) else '；'.join(
            f'{key}: {item}' for key, item in value.items())
    text = ' '.join(str(value).split())
    return text[:limit - 1] + '…' if len(text) > limit else text or '—'


def _business_record(raw):
    """Normalize requirement table JSON into the same readable fields as packages."""
    record = dict(raw or {})
    headers, values = record.get('headers_json'), record.get('values_json')
    try:
        headers = json.loads(headers) if isinstance(headers, str) else headers
        values = json.loads(values) if isinstance(values, str) else values
    except (TypeError, ValueError):
        headers = values = None
    if isinstance(headers, list) and isinstance(values, list):
        for header, value in zip(headers, values):
            if isinstance(header, str) and header.strip() and value is not None:
                record.setdefault(header.strip(), value)
    return record


def _item_title(record):
    for key in ('file_name', 'Name', 'name', 'title', 'test_case',
                'package_name', 'Package name', 'patch_id', 'section_title',
                'test_module', 'exemption_id', 'item_key', 'row_text'):
        if record.get(key):
            return _short(record[key], 105)
    return '数据条目'


def _item_summary(item):
    before = _business_record(item.get('before_json'))
    after = _business_record(item.get('after_json'))
    primary = after if item.get('change_type') != 'removed' else before
    title = _item_title(primary)
    names = list(dict.fromkeys([*after, *before]))
    meaningful = [key for key in names
                  if key not in _SKIP_FIELDS
                  and not key.endswith('_key')
                  and before.get(key) != after.get(key)]
    # Highlight business differences, never expose storage internals.
    if item.get('change_type') == 'modified':
        details = [f"{_LABELS.get(key, key)}：{_short(before.get(key), 76)} → {_short(after.get(key), 76)}"
                   for key in meaningful[:2]]
    else:
        # The object name is already its own column; surface package and
        # requirement details rather than repeating the same title twice.
        preferred = ('Package name', 'package_name', 'Description', 'description',
                     'Remark', 'remark', 'Type', 'type', 'issue_text',
                     'row_text', 'section_title', 'version', 'file_name')
        selected = [key for key in preferred if key in meaningful]
        selected += [key for key in meaningful
                     if key not in selected and (key in _LABELS or key in
                     ('Name', 'Package name', 'Remark', 'Description'))]
        details = [f"{_LABELS.get(key, key)}：{_short(primary.get(key), 110)}"
                   for key in selected[:2]]
    if not details:
        details = ['业务字段已变更，完整字段差异见雷达页面。']
    return title, '；'.join(details)


def _html_cell(value, *, align='left', color='', bold=False, background=''):
    styling = ('padding:9px 11px;border-bottom:1px solid #e5e8ee;'
               f'text-align:{align};vertical-align:top;overflow-wrap:anywhere;')
    if color:
        styling += f'color:{color};'
    if background:
        styling += f'background:{background};'
    if bold:
        styling += 'font-weight:700;'
    return f'<td style="{styling}">{escape(str(value), quote=True)}</td>'


def _html_table(headers, rows):
    header = ''.join(
        f'<th scope="col" style="padding:10px 11px;text-align:{"right" if index else "left"};'
        'background:#f3f5f9;color:#495465;border-bottom:1px solid #dce1e9;'
        f'font-weight:600;">{escape(str(value))}</th>'
        for index, value in enumerate(headers))
    return ('<table role="table" cellpadding="0" cellspacing="0" '
            'style="border-collapse:collapse;width:100%;font-size:13px;'
            'table-layout:fixed;color:#243043;">'
            f'<thead><tr>{header}</tr></thead><tbody>{"".join(rows)}</tbody></table>')


def _run_link(run):
    url = run.get('config', {}).get('dashboard_url')
    if not url:
        return ''
    parts = urlsplit(str(url))
    if parts.scheme not in ('https', 'http') or not parts.netloc:
        return ''
    query = dict(parse_qsl(parts.query))
    query['run'] = str(run['id'])
    return urlunsplit(parts._replace(query=urlencode(query)))


def build_email_html(run, changes):
    """Table-based HTML for GMS update radar, with visible scan uncertainty.

    All dynamic values from scraped pages / data stores are escaped. The
    original build_email remains the plain-text compatibility representation.
    """
    summary = run['summary']
    zone = ZoneInfo(run['config']['timezone'])
    def local_time(value):
        return (datetime.fromisoformat(value).astimezone(zone).strftime('%Y-%m-%d %H:%M:%S')
                if value else '—')

    errors = summary.get('errors') or {}
    count = int(summary.get('changes_total') or 0)
    pending = run.get('status') in ('running', 'interrupted')
    incomplete = bool(errors) or run.get('status') in ('partial', 'failed', 'interrupted')
    state = '扫描未完整完成' if incomplete else '扫描中' if pending else '完成'
    state_color = '#b45309' if incomplete else '#0c8262'
    subject, _ = build_email(run, changes)
    tasks = summary.get('tasks', run['config']['tasks'])
    def heading(title):
        return ('<h2 style="font-size:15px;color:#233049;'
                'margin:22px 0 9px;">' + escape(title) + '</h2>')
    summary_row = '<tr>' + ''.join(
        _html_cell(value, bold=i in (0, 4), color=state_color if i == 3 else '')
        for i, value in enumerate((
            '#' + str(run['id']), local_time(run.get('started_at')),
            local_time(run.get('finished_at')), state, count
        ))) + '</tr>'
    category_rows = []
    for task in tasks:
        entry = (summary.get('categories') or {}).get(task)
        if not entry:
            category_rows.append('<tr>' + _html_cell(CATEGORIES.get(task, task))
                                 + _html_cell('—') * 5
                                 + _html_cell('无可用数据', color='#b45309') + '</tr>')
            continue
        failed = 'exemptions' if task == 'exemptions' else 'updates'
        is_failed = failed in errors or 'radar' in errors
        values = (
            CATEGORIES.get(task, task), entry.get('before', 0),
            entry.get('after', 0), entry.get('added', 0),
            entry.get('modified', 0), entry.get('removed', 0)
        )
        row = '<tr>' + _html_cell(values[0], bold=True)
        row += _html_cell(values[1], align='right') + _html_cell(values[2], align='right')
        for value, kind in zip(values[3:], ('added', 'modified', 'removed')):
            color = _HUES[kind][1] if value else '#697386'
            row += _html_cell('—' if is_failed else value, align='right',
                              color=color, bold=bool(value) and not is_failed,
                              background=_HUES[kind][2] if value and not is_failed else '')
        row += _html_cell('保留旧数据' if is_failed else '成功',
                          color='#b45309' if is_failed else '#137a4b', bold=True) + '</tr>'
        category_rows.append(row)
    error_rows = []
    for group, detail in errors.items():
        label = {'updates': 'GMS更新', 'exemptions': 'Mainline包豁免项',
                 'radar': '雷达任务'}.get(group, group)
        error_rows.append('<tr>' + _html_cell(label, bold=True)
                          + _html_cell('扫描失败', color='#b4233b', bold=True)
                          + _html_cell(_short(detail, 700)) + '</tr>')
    change_rows = []
    for item in changes[:20]:
        kind = item.get('change_type', '')
        label, color, bg = _HUES.get(kind, ('变更', '#536075', '#edf0f3'))
        item_title, detail = _item_summary(item)
        change_rows.append('<tr>'
                           + _html_cell(CATEGORIES.get(item.get('category'), item.get('category', '')), bold=True)
                           + _html_cell(label, color=color, background=bg, bold=True)
                           + _html_cell(item_title, bold=True)
                           + _html_cell(_short(detail, 320)) + '</tr>')
    link = _run_link(run)
    intro = '本次扫描前的平台数据 → 本次成功扫描后的数据。'
    if incomplete:
        intro = ('部分数据源扫描失败或被中断：失败组保留旧数据；'
                 '0 项变更不代表所有来源均无更新。')
    contents = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"></head>',
        '<body style="margin:0;padding:22px;background:#f5f7fa;'
        'font-family:Arial,Microsoft YaHei,sans-serif;color:#253045;">',
        '<div style="max-width:940px;margin:0 auto;background:#fff;'
        'padding:24px;border:1px solid #e2e7ef;border-radius:8px;">',
        '<h1 style="font-size:20px;margin:0 0 7px;color:#243b64;">GMS 更新雷达</h1>',
        f'<div style="font-size:13px;color:#667287;">{escape(subject)}</div>',
        heading('扫描摘要'),
        _html_table(['批次', '开始时间', '结束时间', '状态', '变更数'], [summary_row]),
        f'<p style="margin:10px 0;font-size:12px;color:{state_color};">{escape(intro)}</p>',
    ]
    if summary.get('initial_baseline'):
        contents.append('<p style="font-size:12px;color:#5b6578;">首次建立基线：扫描前数据库为空。</p>')
    contents.extend([
        heading('监控项统计'),
        _html_table(['监控项', '扫描前', '扫描后', '新增', '修改', '移除', '状态'], category_rows),
    ])
    if error_rows:
        contents.extend([
            heading('扫描异常'),
            _html_table(['模块', '级别', '原因'], error_rows),
            '<p style="font-size:12px;font-weight:700;color:#b45309;">'
            '失败组保留扫描前的数据，不能据此判定无更新。</p>',
        ])
    if count:
        contents.extend([
            heading('变更明细（最多 20 条）'),
            _html_table(['类别', '动作', '对象', '变更摘要'], change_rows),
        ])
        if count > len(change_rows):
            contents.append(
                '<p style="font-size:12px;color:#687386;">其余变更请查看完整记录。</p>'
            )
    else:
        contents.append(
            '<p style="font-size:13px;color:#667287;margin-top:17px;">'
            + ('成功扫描的项目未发现变化，其他项目的扫描结果尚不完整。'
               if incomplete else '本次扫描没有发现变化。') + '</p>'
        )
    if link:
        safe = escape(link, quote=True)
        contents.extend([
            '<p style="margin:24px 0 0;font-size:13px;">'
            f'<a href="{safe}" style="color:#375fbe;text-decoration:underline;">'
            '查看完整记录与字段证据</a></p>',
        ])
    contents.append('</div></body></html>')
    return ''.join(contents)
