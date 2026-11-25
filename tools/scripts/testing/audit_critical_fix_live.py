"""Live read-only Critical Fix evidence audit; outputs aggregate statistics only."""
import argparse
import json
import os
import re
import sqlite3
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

import requests
from lxml import html

from features.gms.fetching import build_session, fetch_source
from features.gms.parsers.critical import _cf_month_link, parse_gms_bulletins
from features.gms.parsing import text_content
from features.gms.sources import SOURCES
from features.gms.update_records import bulletin_detail_coverage
from tools.scripts.testing.resolve_soak_host_config import resolve


BASE = 'https://172.16.14.233:5001'
API = '/api/gms-update-center/critical-patches'
FIELDS = ('year','bulletin_month','patch_id','patch_kind','android_versions','description',
          'deadline','bulletin_url','detail_text','patch_rows_json')
STATES = ('merged','partial','open','missing','unknown')
CASE_IDS = {'A-528269366','A-568181732','A-543052093','R-543243062','A-445016017','A-376407624'}


def emit(stage, **data):
    print('CF_AUDIT='+json.dumps({'stage':stage, **data}, ensure_ascii=True), flush=True)


def guarded(stage, fn):
    try:
        return fn()
    except Exception as exc:
        emit(stage, completed=False, error_type=type(exc).__name__)
        return None


def index(db):
    with sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True) as conn:
        conn.row_factory=sqlite3.Row
        rows=[dict(r) for r in conn.execute('SELECT * FROM gms_update_bulletin_patches')]
        cov=bulletin_detail_coverage(conn)
    kinds=Counter(r['patch_kind'] for r in rows)
    emit('index',completed=True,records=len(rows),months_scanned=cov['months_scanned'],
         months_discovered=cov['months_discovered'],incomplete_records=cov['incomplete_records'],
         kinds={k:kinds[k] for k in ('mandatory','recommended','strongly_recommended','functional_unspecified','')},
         non_numbered=sum(not r['patch_id'] for r in rows),no_version=sum(not r['android_versions'] for r in rows),
         november=sum(r['year']=='2026' and r['bulletin_month']=='November' for r in rows),
         duplicate_month_id=sum(n-1 for n in Counter((r['year'],r['bulletin_month'],r['patch_id'])
                  for r in rows if r['patch_id']).values() if n>1))
    return rows


def compare(rows, fresh, stage, **extra):
    old={r['item_key']:r for r in rows}
    new={r['item_key']:r for r in fresh}
    shared=old.keys() & new.keys()
    emit(stage,completed=True,old_count=len(old),new_count=len(new),
         old_only=len(old.keys()-new.keys()),new_only=len(new.keys()-old.keys()),
         field_mismatches={f:sum(old[k].get(f)!=new[k].get(f) for k in shared) for f in FIELDS},**extra)


def source(rows):
    args=argparse.Namespace(browser='auto',cookie_file=None,cookie_header=None,verbose=False)
    src=next(s for s in SOURCES if s.key=='gms_bulletins')
    independent=set()
    visited=set()
    pattern=re.compile(r'^([APR])(?:\s*[-‐‑‒–—−]\s*|\s+)(\d+)\b')
    with build_session(args) as session:
        class Observed:
            def get(self,url,**kwargs):
                response=session.get(url,**kwargs)
                month=_cf_month_link(src.url,response.url)
                if response.status_code==200 and month:
                    doc=html.fromstring(response.content)
                    articles=doc.xpath('//article[contains(@class,"devsite-article")]')
                    if articles:
                        visited.add(month[1:])
                        for node in articles[0].xpath('.//h2|.//h3|.//h4|.//h5|.//h6'):
                            m=pattern.match(text_content(node))
                            if m:
                                independent.add((month[1],month[2],m[1]+'-'+m[2]))
                return response
        observed=Observed()
        parsed=parse_gms_bulletins(fetch_source(observed,src,20),session=observed,timeout=20)
    fresh=[asdict(r) for r in parsed.bulletin_patches]
    ids={(r['year'],r['bulletin_month'],r['patch_id']) for r in rows}
    nov={r['patch_id'] for r in fresh if r['year']=='2026' and r['bulletin_month']=='November'}
    compare(rows,fresh,'source',visited_months=len(visited),ready_months=sum(s=='ready' for s in parsed.bulletin_month_status.values()),
            discovered_months=len(parsed.bulletin_month_status),independent_heading_ids=len(independent),
            independent_heading_ids_missing=len(independent-ids),november=len(nov),
            november_expected=nov==CASE_IDS-{'A-528269366'})


def auth(host):
    cfg,_=resolve(host/'configs/local',dict(os.environ))
    session=requests.Session()
    session.trust_env=False
    ca=host/'configs/secrets/certs/gms-local-ca.crt'
    if not ca.is_file():
        ca=host/'configs/secrets/certs/gms-local.crt'
    if ca.is_file():
        session.verify=str(ca)
    token=cfg.get('GMS_SOAK_SESSION_TOKEN')
    if token:
        session.cookies.set('gms_session',token,domain=urlsplit(BASE).hostname,path='/',secure=True)
    elif cfg.get('GMS_SOAK_USERNAME') and cfg.get('GMS_SOAK_PASSWORD'):
        for path in ('/api/auth/login','/api/auth/elevate'):
            result=session.post(BASE+path,json={'username':cfg['GMS_SOAK_USERNAME'],'password':cfg['GMS_SOAK_PASSWORD']},
                                timeout=20,allow_redirects=False)
            if result.status_code!=200:
                emit('auth',completed=False,http=result.status_code)
                return None
    else:
        emit('auth',completed=False,credentials_configured=False)
        return None
    result=session.get(BASE+'/api/auth/status',timeout=20,allow_redirects=False)
    emit('auth',completed=result.status_code==200,http=result.status_code)
    return session if result.status_code==200 else None


def get(session,path,params=None,timeout=100):
    r=session.get(BASE+path,params=params,timeout=timeout,allow_redirects=False)
    r.raise_for_status()
    return r.json()


def platform(session,rows):
    page=session.get(BASE+'/gms-update-center?tab=critical',timeout=20,allow_redirects=False)
    page.raise_for_status()
    doc=html.fromstring(page.content)
    items=[]
    sizes=[]
    total=None
    while total is None or len(items)<total:
        result=get(session,API,{'limit':100,'offset':len(items)})
        total=result['total']
        sizes.append(len(result['items']))
        if not result['items']:
            break
        items.extend(result['items'])
        if len(items)>10000:
            raise ValueError('unexpected index size')
    compare(rows,items,'platform',page_http=page.status_code,page_table=bool(doc.xpath('//*[@id="tables-area"]')),
            api_total=total,page_sizes=sizes,coverage=bool(result.get('coverage_complete')),
            associated_exemptions=sum(bool(r.get('exemptions')) for r in items))
    return items


def gerrit(session,items):
    cfg=get(session,'/api/gerrit-dashboard/config').get('data') or {}
    target=(urlsplit(str(cfg.get('base_url') or '')).hostname=='10.10.10.29' or cfg.get('ssh_host')=='10.10.10.29')
    emit('gerrit_target',completed=target)
    if not target:
        return None
    data=get(session,API+'/gerrit',{'refresh':1})
    entries=data.get('items') or {}
    if not isinstance(entries,dict):
        raise ValueError('unexpected result')
    keys={r['item_key']:r for r in items}
    counts=Counter(r.get('status') for r in entries.values())
    versions=Counter()
    changes={}
    inconsistent=0
    cases={}
    for key,row in entries.items():
        for v in row.get('versions',[]):
            versions[v.get('status')]+=1
            if v.get('status')=='merged' and (not v.get('upstream_total') or
                v['upstream_merged']!=v['upstream_total'] or not v.get('complete_branches')):
                inconsistent+=1
        for c in [*row.get('changes',[]),*(c for up in row.get('upstreams',[]) for c in up.get('changes',[]))]:
            n=str(c.get('number') or '')
            if n.isdigit():
                changes.setdefault(n,[]).append(c)
        record=keys.get(key,{})
        if record.get('year')=='2026' and record.get('patch_id') in CASE_IDS:
            cases.setdefault(record['patch_id'],[]).append({
                'status':row.get('status') if row.get('status') in STATES else 'unknown',
                'total':int(row.get('upstream_total') or 0),'merged':int(row.get('upstream_merged') or 0),
                'versions':[{'version':v['version'],'status':v['status'],'total':v['upstream_total'],
                             'merged':v['upstream_merged']} for v in row.get('versions',[])
                            if re.fullmatch(r'\d{1,2}(?: QPR\d+)?',str(v.get('version',''))) and v.get('status') in STATES],
                'truncated':bool(row.get('truncated'))})
    emit('gerrit_rollup',completed=True,configured=bool(data.get('configured')),query_error=bool(data.get('error')),
         rows=len(entries),missing_rows=len(keys.keys()-entries.keys()),statuses={k:counts[k] for k in STATES},
         versions={k:versions[k] for k in STATES},truncated=sum(bool(r.get('truncated')) for r in entries.values()),
         inconsistent_merged_versions=inconsistent,reported_changes=len(changes),cases=cases)
    actual={}
    errors=0
    numbers=sorted(changes,key=int)
    for i in range(0,len(numbers),15):
        batch=numbers[i:i+15]
        try:
            result=get(session,'/api/gerrit-dashboard/changes',
                       {'query':'('+' OR '.join('change:'+n for n in batch)+') limit:100'}).get('data') or {}
            errors+=bool(result.get('error') or (result.get('stats') or {}).get('moreChanges'))
            for r in result.get('items') or []:
                n=str(r.get('_number') or r.get('number') or '')
                if n in changes:
                    actual[n]=r
        except (requests.RequestException,ValueError):
            errors+=1
    mismatches=Counter()
    for n,claims in changes.items():
        if n not in actual:
            continue
        for c in claims:
            for field in ('status','branch','project'):
                if str(c.get(field) or '').casefold()!=str(actual[n].get(field) or '').casefold():
                    mismatches[field]+=1
            if c.get('subject') and not str(actual[n].get('subject') or '').startswith(str(c['subject'])):
                mismatches['subject']+=1
    emit('gerrit_independent',completed=True,requested=len(numbers),verified=len(actual),
         not_returned=len(numbers)-len(actual),failed_batches=errors,
         field_mismatches={k:mismatches[k] for k in ('status','branch','project','subject')},
         scope='reported_changes_only')
    return entries


def browser(session,items,entries):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        chromium=pw.chromium.launch(headless=True)
        try:
            ctx=chromium.new_context(viewport={'width':1600,'height':900})
            ctx.add_cookies([{'name':c.name,'value':c.value,'url':BASE} for c in session.cookies])
            page=ctx.new_page()
            page.goto(BASE+'/gms-update-center?tab=critical',wait_until='domcontentloaded',timeout=25000)
            page.wait_for_selector('#tables-area .gerrit-cell',timeout=25000)
            if entries:
                try:
                    page.wait_for_selector('#tables-area .gerrit-cell .gerrit-badge',timeout=75000)
                except Exception:
                    pass
            rendered=page.locator('#tables-area .gerrit-cell').evaluate_all("""nodes=>nodes.map(n=>{
                const b=n.querySelector('.gerrit-badge');
                const c=b&&[...b.classList].find(x=>x.startsWith('gerrit-')&&x!=='gerrit-badge');
                return {key:n.getAttribute('data-patch'),status:c&&c.slice(7)};
            })""")
            matched=[r for r in rendered if r['status'] and r['key'] in (entries or {})]
            emit('browser',completed=True,rows=len(rendered),
                 first_page_exact_order=[r['key'] for r in rendered]==[r['item_key'] for r in items[:100]],
                 gerrit_badges=len(matched),gerrit_mismatches=sum(r['status']!=entries[r['key']]['status'] for r in matched))
        finally:
            chromium.close()


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--host-root',type=Path,required=True)
    p.add_argument('--db',type=Path,required=True)
    args=p.parse_args()
    rows=guarded('index',lambda:index(args.db))
    if rows is None:
        return 1
    session=guarded('auth',lambda:auth(args.host_root))
    if session is not None:
        with session:
            items=guarded('platform',lambda:platform(session,rows))
            if items is not None:
                entries=guarded('gerrit',lambda:gerrit(session,items))
                guarded('browser',lambda:browser(session,items,entries))
    guarded('source',lambda:source(rows))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
