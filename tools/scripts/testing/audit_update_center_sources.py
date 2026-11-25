"""One-shot source reconciliation; only aggregate results leave the dedicated host."""
import argparse
import json
import re
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from lxml import html

from features.gms.fetching import build_session, fetch_source
from features.gms.parsers import PARSERS
from features.gms.parsers import mainline as ml
from features.gms.parsers.critical import _cf_month_link
from features.gms.sources import SOURCES
from tools.scripts.testing.audit_critical_fix_live import BASE, auth


M = '/api/gms-update-center'
TABLES = {
    'changes': (M+'/changes', ('id',)),
    'requirements': (M+'/requirements', ('source_key','tag_key')),
    'packages': (M+'/packages', ('source_key','item_key')),
    'mainline': (M+'/mainline', ('source_key','item_key')),
    'artifacts': (M+'/artifacts', ('source_key','item_key')),
    'exemptions': (M+'/exemptions', ('product_section','issue_type','test_module','test_case','exemption_id','android_versions')),
    'patch': (M+'/security-patches', ('source_url','cve_id','mainline_module')),
    'critical': (M+'/critical-patches', ('source_key','item_key')),
}
ARGS = argparse.Namespace(browser='auto', cookie_file=None, cookie_header=None, verbose=False,
                          index_url=ml.DEFAULT_INDEX_URL)
FAILURES = []

def emit(stage, **kw):
    if (stage.endswith('_error') or stage == 'scan_pending'
            or (stage == 'scan_start' and not kw.get('accepted'))
            or (stage == 'scan_end' and (kw.get('returncode') != 0 or kw.get('error')))
            or (stage == 'compare' and any(kw.get(f) for f in
                ('fresh_duplicates','source_only','api_only','field_mismatches','missing_fields')))
            or (stage == 'release_coverage' and kw.get('failed'))
            or (stage == 'coverage' and (not kw.get('discovered')
                or kw.get('statuses',{}).get('ready',0) != kw.get('discovered')))
            or (stage == 'critical_headings' and
                (not kw.get('heading_candidates') or kw.get('candidates_not_indexed')))
            or (stage == 'independent' and kw.get('missing_links'))):
        FAILURES.append(stage)
    print('SOURCE_AUDIT='+json.dumps(dict(stage=stage, **kw), ensure_ascii=True), flush=True)

def key(row, fields):
    return tuple(str(row.get(f,'')) for f in fields)

def get_api(session, path, **params):
    response = session.get(BASE+path, params=params, timeout=45, allow_redirects=False)
    response.raise_for_status()
    body = response.json()
    if not body.get('success'):
        raise ValueError('unsuccessful API')
    return body

def snapshot(session, name):
    path, fields = TABLES[name]
    rows = []
    total = None
    while total is None or len(rows) < total:
        body = get_api(session, path, limit=500, offset=len(rows))
        batch = body.get('data', {}).get('items', body.get('items', []))
        count = body.get('meta', {}).get('total', body.get('total'))
        if not isinstance(count, int) or count > 50000 or (total is not None and count != total):
            raise ValueError('unstable pagination')
        total = count
        if not batch and len(rows) < total:
            raise ValueError('incomplete pagination')
        rows.extend(batch)
    if len(rows) != total or len({key(r,fields) for r in rows}) != total:
        raise ValueError('duplicate or missing API records')
    return rows

def all_snapshots(session, phase):
    data = {}
    for name in TABLES:
        try:
            data[name] = snapshot(session,name)
            emit('api', phase=phase, table=name, total=len(data[name]))
        except Exception as exc:
            emit('api_error', phase=phase, table=name, error=type(exc).__name__)
    return data

def start(session, name, path):
    status = get_api(session,path+'/scan/status')
    s = status.get('data',{}).get('status', status.get('status',{}))
    if s.get('running'):
        emit('scan_start', scanner=name, accepted=False, already_running=True)
        return False
    response = session.post(BASE+path+'/scan', params={'mode':'full'},
                            headers={'Origin':BASE,'Referer':BASE+'/gms-update-center'},
                            timeout=30, allow_redirects=False)
    emit('scan_start', scanner=name, http=response.status_code, accepted=response.status_code==200)
    return response.status_code == 200 and response.json().get('success') is True

def wait_scans(session, jobs):
    pending = dict(jobs)
    deadline = time.monotonic()+600
    while pending and time.monotonic()<deadline:
        for name,path in list(pending.items()):
            try:
                body = get_api(session,path+'/scan/status')
                state = body.get('data',{}).get('status',body.get('status',{}))
                if not state.get('running') and state.get('finished_at'):
                    scan_diagnostics(name,state)
                    emit('scan_end',scanner=name,mode=state.get('mode'),
                         returncode=state.get('returncode'), error=bool(state.get('error')),
                         stderr_present=bool(state.get('stderr')))
                    del pending[name]
            except Exception as exc:
                emit('scan_status_error',scanner=name,error=type(exc).__name__)
        if pending:
            time.sleep(5)
    for name in pending:
        emit('scan_pending',scanner=name)

def compare(name, source, old, fresh, fields=None):
    fields = fields or TABLES[name][1]
    a = {key(r,fields):r for r in old}
    b = {key(r,fields):r for r in fresh}
    common = a.keys() & b.keys()
    mismatch = Counter()
    absent = Counter()
    for k in common:
        for f,v in b[k].items():
            if f in ('content_hash','detail_fetched_at','source_title'):
                continue
            if f not in a[k]:
                absent[f]+=1
            elif a[k][f]!=v:
                mismatch[f]+=1
    emit('compare',table=name,source=source,api=len(a),fresh=len(b),
         fresh_duplicates=len(fresh)-len(b),source_only=len(b.keys()-a.keys()),
         api_only=len(a.keys()-b.keys()),field_mismatches=dict(mismatch),missing_fields=dict(absent))

def scan_diagnostics(name, state):
    stderr = str(state.get('stderr') or '')
    categories = Counter()
    for line in stderr.splitlines():
        for pattern, category in (
            (r'\b(401|403)\b', 'auth_or_forbidden'),
            (r'\b404\b', 'not_found'),
            (r'\b(429|500|502|503|504)\b', 'upstream_http'),
            (r'missing release article', 'missing_article'),
            (r'pages unavailable', 'unavailable_pages'),
            (r'(?i:timeout|timed out)', 'timeout'),
            (r'(?i:traceback|attributeerror|valueerror)', 'exception'),
            (r'(?i:authentication failure|sign in)', 'login_required'),
        ):
            if re.search(pattern, line):
                categories[category]+=1
    emit('scan_diagnostics',scanner=name,running=bool(state.get('running')),
         returncode=state.get('returncode'),categories=dict(categories))

def event_diagnostics(session, rows):
    runs = get_api(session,M+'/scan/runs',limit=10,offset=0).get('data',{}).get('items',[])
    grouped = {}
    for row in rows:
        grouped.setdefault(row.get('run_id'),[]).append(row)
    for run in runs:
        events = grouped.get(run['id'],[])
        by_type=Counter((r.get('entity_type'),r.get('change_type')) for r in events)
        field_counts=Counter()
        metadata_only=0
        for row in events:
            if row.get('change_type')!='changed':
                continue
            before=row.get('before') or {}
            after=row.get('after') or {}
            fields={f for f in before.keys()|after.keys() if before.get(f)!=after.get(f)}
            field_counts.update(fields)
            metadata_only+=bool(fields) and fields <= {'content_hash','detail_fetched_at','first_seen_at','last_seen_at'}
        emit('change_run',run_id=run['id'],mode=run.get('mode'),success=bool(run.get('success')),
             reported=run.get('changes_total'),actual=len(events),
             categories=[{'entity':k[0],'type':k[1],'count':v} for k,v in sorted(by_type.items())],
             metadata_only=metadata_only,changed_fields=dict(field_counts))

def requirement_diagnostics(rows):
    groups=Counter()
    shapes=Counter()
    by_section={}
    for row in rows:
        text=' '.join(row.get('text_excerpt','').split())
        ids=tuple(sorted(x.strip() for x in row.get('requirement_ids','').split(',') if x.strip()))
        k=(row.get('section_key'),row.get('android_version'),row.get('change_kind'))
        by_section.setdefault(k,[]).append(text)
        groups[(k,ids)]+=1
        shapes[(row.get('android_version'),row.get('change_kind'),bool(ids))]+=1
    nested=sum(sum(any(a!=b and a in b for b in set(texts)) for a in set(texts))
               for texts in by_section.values())
    emit('requirement_shapes',records=len(rows),
         distinct_section_version_kind_id_groups=len(groups),
         repeated_id_groups=sum(v>1 for k,v in groups.items() if k[1]),
         contained_text_candidates=nested,
         distribution=[{'version':k[0],'kind':k[1],'has_id':k[2],'count':v}
                       for k,v in sorted(shapes.items())])

def article(doc):
    return next(iter(doc.xpath('//article[contains(@class,"devsite-article")]')),doc)

def txt(node):
    return ' '.join(node.text_content().split())

def paths(doc, base):
    return {urlsplit(urljoin(base,a)).path.rstrip('/') for a in doc.xpath('//a/@href')
            if urlsplit(urljoin(base,a)).hostname==urlsplit(base).hostname}

def independent(source, fetched, parsed, api):
    root = article(fetched.doc)
    if source.key=='cts_downloads':
        candidates=set()
        for a in root.xpath('.//a[@href]'):
            h=a.xpath('preceding::h2[1]')
            if not h:
                continue
            m=re.search(r'Android\s+(\d+)',txt(h[0]),re.I)
            if m and int(m.group(1))>=14 and re.search(r'\bARM\b',txt(a),re.I):
                candidates.add(urljoin(fetched.final_url,a.get('href')))
        urls={r['download_url'] for r in api}
        emit('independent',source=source.key,eligible_links=len(candidates),
             missing_links=len(candidates-urls),api_other_links=len(urls-candidates))
    elif source.key=='mainline_preload':
        discovered={p for p in paths(fetched.doc,fetched.final_url)
                    if re.search(r'/release-notes/\d{4}/[a-z]+/notes-PRELOAD-',p,re.I)}
        indexed={urlsplit(r['notes_url']).path.rstrip('/') for r in api}
        cutoff=0
        month_names=['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
        def in_window(p):
            m=re.search(r'/release-notes/(\d{4})/([a-z]+)/',p,re.I)
            month=m.group(2)[:3].lower() if m else ''
            return bool(m and month in month_names and
                        int(m.group(1))*12+month_names.index(month)>=cutoff)
        missing=discovered-indexed
        emit('independent',source=source.key,discovered_links=len(discovered),
             indexed_links=len(indexed),outside_index=len(missing),
             missing_in_window=sum(in_window(p) for p in missing),
             outside_policy_window=sum(not in_window(p) for p in missing),
             indexed_not_in_navigation=len(indexed-discovered),policy_window_months=0)
    elif source.key=='gms_requirements':
        candidates=set()
        for n in root.xpath('.//p|.//li|.//td'):
            t=txt(n)
            if re.search(r'Android\s+\d{2}',t,re.I):
                candidates.update(re.findall(r'\[([A-Z]+(?:-[A-Z]+)?-[0-9][A-Z0-9_.-]*)\]',t))
        indexed={s.strip() for r in api for s in r.get('requirement_ids','').split(',') if s.strip()}
        emit('independent',source=source.key,version_mentioned_ids=len(candidates),
             ids_not_in_version_tags=len(candidates-indexed),scope='candidate IDs, not confirmed omissions')
        requirement_diagnostics(api)
    elif source.key in ('gms_downloads','gts_downloads'):
        candidates={}
        for table in root.xpath('.//table'):
            for row in table.xpath('.//tr'):
                for a in row.xpath('.//a[@href]'):
                    t=txt(a)
                    u=urljoin(fetched.final_url,a.get('href'))
                    if re.search(r'\.zip(?:$|[?#])',u,re.I) or re.search(r'\.zip\b',t,re.I):
                        context=txt(row)
                        h=table.xpath('preceding::h2[1]')
                        context+=' '+(txt(h[0]) if h else '')
                        if source.key=='gms_downloads':
                            m=re.search(r'Android\s+(\d+)',context,re.I)
                            reason=('older_android' if m and int(m.group(1))<14 else
                                    'eligible_android' if m else 'unclassified_version')
                        else:
                            m=re.search(r'\bGTS[_\s-]+(\d+)',context,re.I)
                            reason=('older_gts' if m and int(m.group(1))<13 else
                                    'eligible_gts' if m else 'unclassified_version')
                        candidates.setdefault(u,set()).add(reason)
        indexed={r['download_url'] for r in api}
        missing=candidates.keys()-indexed
        emit('independent',source=source.key,zip_link_candidates=len(candidates),
             candidates_not_indexed=len(missing),
             missing_candidate_classes=dict(Counter('+'.join(sorted(candidates[u])) for u in missing)),
             scope='candidate links; merged cells may require manual evidence')
    elif source.key=='vts_downloads':
        candidates={}
        for a in root.xpath('.//a[@href]'):
            h=a.xpath('preceding::h2[1]')
            if not h:
                continue
            m=re.search(r'Android\s+(\d+)',txt(h[0]),re.I)
            t=txt(a)
            if m and int(m.group(1))>=14 and re.search(r'arm(?:64)?\b',t,re.I) and not re.search(r'x86|gki|dashboard',t,re.I):
                h3=a.xpath('preceding::h3[1]')
                context=txt(h3[0]) if h3 else ''
                category=('excluded_gki_or_x86' if re.search(r'gki|x86',context,re.I) else
                          'gsi' if re.search(r'\bgsi\b',context,re.I) else
                          'vts' if re.search(r'\bvts\b',context,re.I) else 'unclassified')
                candidates.setdefault(urljoin(fetched.final_url,a.get('href')),set()).add(category)
        indexed={r['download_url'] for r in api}
        missing=candidates.keys()-indexed
        emit('independent',source=source.key,arm_link_candidates=len(candidates),
             candidates_not_indexed=len(missing),
             missing_candidate_classes=dict(Counter('+'.join(sorted(candidates[u])) for u in missing)),
             scope='candidate links')
    elif source.key=='gms_bulletins':
        emit('coverage',source=source.key,discovered=len(parsed.bulletin_month_status),
             statuses=dict(Counter(parsed.bulletin_month_status.values())))

def monitor_sources(data):
    attributes={'cts_downloads':('artifacts','artifacts'), 'vts_downloads':('artifacts','artifacts'),
                'gts_downloads':('artifacts','artifacts'),'gms_downloads':('packages','gms_packages'),
                'gms_requirements':('requirements','requirement_version_tags'),
                'mainline_preload':('mainline','mainline_packages'),'gms_bulletins':('critical','bulletin_patches')}
    with build_session(ARGS) as session:
        for source in SOURCES:
            try:
                fetched=fetch_source(session,source,25)
                table,attr=attributes[source.key]
                observed_ids=set()
                observed_months=set()
                class Observer:
                    def __init__(self, source, observed_months, observed_ids):
                        self.source = source
                        self.observed_months = observed_months
                        self.observed_ids = observed_ids

                    def get(self,url,**kwargs):
                        response=session.get(url,**kwargs)
                        if self.source.key=='gms_bulletins' and response.status_code==200:
                            month=_cf_month_link(self.source.url,response.url)
                            if month:
                                root=article(html.fromstring(response.content))
                                self.observed_months.add(month[1:])
                                for n in root.xpath('.//h2|.//h3|.//h4|.//h5|.//h6'):
                                    m=re.match(r'([APR])\s*[-‐‑‒–—− ]\s*(\d+)\b',txt(n))
                                    if m:
                                        self.observed_ids.add((*month[1:],m.group(1)+'-'+m.group(2)))
                        return response
                parsed=PARSERS[source.parser](fetched,session=Observer(source, observed_months, observed_ids))
                fresh=[asdict(r) for r in getattr(parsed,attr)]
                if table not in data:
                    emit('source_error',source=source.key,error='API snapshot unavailable')
                    continue
                old=[r for r in data[table] if r.get('source_key')==source.key]
                compare(table,source.key,old,fresh)
                independent(source,fetched,parsed,old)
                if source.key=='gms_bulletins':
                    indexed={(str(r['year']),r['bulletin_month'],r['patch_id']) for r in old}
                    emit('critical_headings',source=source.key,observed_months=len(observed_months),
                         heading_candidates=len(observed_ids),candidates_not_indexed=len(observed_ids-indexed),
                         scope='month identity and numbered headings')
            except Exception as exc:
                emit('source_error',source=source.key,error=type(exc).__name__)

def mainline_sources(data):
    with build_session(ARGS) as session:
        doc=ml.fetch_doc(session,ml.DEFAULT_INDEX_URL,25)
        years=ml.extract_release_years(doc)
        pages=[p for y in years for p in ml.extract_release_pages(doc,ml.DEFAULT_INDEX_URL,y)]
        independent_links={p for p in paths(doc,ml.DEFAULT_INDEX_URL)
                           if re.search(r'/release-notes/\d{4}/[a-z-]+/notes-',p,re.I)}
        selected={urlsplit(p.url).path.rstrip('/') for p in pages}
        emit('release_discovery',years=len(years),pages=len(pages),independent_links=len(independent_links),
             candidate_pages_not_selected=len(independent_links-selected))
        issues=[]
        patches=[]
        failed=0
        failure_categories=Counter()
        missing_bug_ids=0
        missing_cve_ids=0
        for p in pages:
            try:
                fetched=ml.fetch_page(session,p.url,25)
                if not fetched.doc.xpath('//article[contains(@class,"devsite-article")]'):
                    failed+=1
                    failure_categories['missing_article']+=1
                    continue
                if urlsplit(fetched.final_url).path.rstrip('/') != urlsplit(p.url).path.rstrip('/'):
                    failed+=1
                    failure_categories['redirected_path']+=1
                    continue
                a=ml.extract_known_issues(p,fetched.doc)
                b=ml.extract_security_patches(p,fetched.doc)
                issues.extend(a)
                patches.extend(b)
                root=article(fetched.doc)
                bugs={i for n in root.xpath('.//li') for m in re.finditer(r'internal bug ref\.\s*([0-9,\sand]+)',txt(n),re.I) for i in re.findall(r'\d+',m.group(1))}
                missing_bug_ids+=len(bugs-{i.exemption_id for i in a})
                cves={m.upper() for n in root.xpath('.//tr') for m in re.findall(r'CVE-\d{4}-\d{4,7}',txt(n),re.I)}
                missing_cve_ids+=len(cves-{i.cve_id for i in b})
            except Exception as exc:
                failed+=1
                response=getattr(exc,'response',None)
                category='http_'+str(response.status_code) if response is not None else type(exc).__name__
                failure_categories[category]+=1
        emit('release_coverage',discovered=len(pages),fetched=len(pages)-failed,failed=failed,
             failure_categories=dict(failure_categories),
             raw_issue_records=len(issues),raw_cve_records=len(patches),
             bug_id_candidates_not_parsed=missing_bug_ids,cve_candidates_not_parsed=missing_cve_ids,
             scope='whole-article candidates may include out-of-scope mentions')
        if not failed:
            if 'exemptions' in data:
                compare('exemptions','release_notes',data['exemptions'],
                        [asdict(r) for r in ml.dedupe_issues(issues,set())])
            if 'patch' in data:
                compare('patch','release_notes',data['patch'],[asdict(r) for r in patches])

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--full-scan',action='store_true',help='Explicitly request a production full scan')
    args=parser.parse_args()
    FAILURES.clear()
    session=auth(Path.home()/'GMS_Remote_Test_Web')
    if session is None:
        emit('auth_error')
        return 1
    with session:
        scanners=[('unified','/api/gms-update-center')]
        for name,path in scanners:
            try:
                status=get_api(session,path+'/scan/status')
                scan_diagnostics(name,status.get('data',{}).get('status',status.get('status',{})))
            except Exception as exc:
                emit('status_error',scanner=name,error=type(exc).__name__)
        before=all_snapshots(session,'before')
        after=before
        if args.full_scan:
            jobs={}
            for name,path in scanners:
                try:
                    if start(session,name,path):
                        jobs[name]=path
                except Exception as exc:
                    emit('scan_start_error',scanner=name,error=type(exc).__name__)
            wait_scans(session,jobs)
            after=all_snapshots(session,'after')
            for name in before.keys() & after.keys():
                emit('delta',table=name,before=len(before[name]),after=len(after[name]))
        try:
            event_diagnostics(session,after.get('changes',[]))
        except Exception as exc:
            emit('event_error',error=type(exc).__name__)
        monitor_sources(after)
        try:
            mainline_sources(after)
        except Exception as exc:
            emit('source_error',source='release_notes',error=type(exc).__name__)
    emit('complete',failures=len(FAILURES),failure_stages=dict(Counter(FAILURES)),
         full_scan_requested=args.full_scan)
    return int(bool(FAILURES))

if __name__=='__main__':
    raise SystemExit(main())
