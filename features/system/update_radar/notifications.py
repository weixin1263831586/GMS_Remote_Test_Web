"""Readable text summaries; upstream page content never becomes email markup."""

import json
from datetime import datetime
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
