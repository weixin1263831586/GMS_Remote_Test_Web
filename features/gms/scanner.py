"""Unified, source-transactional GMS scanner; no legacy database reads."""
import argparse
import json
from contextlib import closing
from dataclasses import asdict
from urllib.parse import urlsplit

from foundation.redaction import redact_sensitive_text

from . import mainline_records as mr
from .fetching import BatchFetcher, build_session, canonical_url, fetch_source
from .models import ParsedSource
from .parsers import PARSERS, mainline_entries, parse_mainline_package
from .parsers import mainline as ml
from .parsing import stable_hash, utc_now
from .repository import connect
from .sources import MAINLINE, select_sources
from .update_records import bulletin_detail_coverage, record_change, sync_source


# Bump whenever parser semantics change. A new parser is a reindex, not
# evidence that Google published new information.
PARSER_VERSION = '5'
ISSUE_KEYS = ('product_section','issue_type','test_module','test_case','exemption_id','android_versions')
CVE_KEYS = ('source_url','cve_id','mainline_module')
VOLATILE = {'id','first_seen_at','last_seen_at','content_hash','detail_fetched_at'}


def recover_interrupted(conn):
    now = utc_now()
    conn.execute("""UPDATE gms_update_source_results SET state='interrupted',error='scan interrupted'
                    WHERE state IN ('pending','running')""")
    conn.execute("""UPDATE gms_update_scan_runs SET state='interrupted',finished_at=?,success=0,
                    error='scan interrupted' WHERE state='running'""", (now,))


def begin_run(conn, mode, sources, trigger):
    with conn:
        recover_interrupted(conn)
        stamp = utc_now()
        cursor = conn.execute("""INSERT INTO gms_update_scan_runs
            (mode,source_filter,started_at,finished_at,sources_scanned,sources_skipped,
             artifacts_total,packages_total,requirement_sections_total,requirement_table_rows_total,
             changes_total,success,error,state,parser_version,trigger)
            VALUES(?,?,?,?,0,0,0,0,0,0,0,0,'','running',?,?)""",
            (mode, ','.join(s.key for s in sources), stamp, stamp, PARSER_VERSION, trigger))
        run_id = cursor.lastrowid
        conn.executemany("""INSERT INTO gms_update_source_results(run_id,source_key,state)
                         VALUES(?,?,'pending')""", [(run_id, s.key) for s in sources])
    return run_id


def mainline_data(fetched, fetcher, timeout, depth):
    """Discover all pages and parse each verified DOM once for all outputs."""
    index = fetched.doc
    years = ml.extract_release_years(index)
    if not years:
        raise ValueError('no Mainline release years discovered')
    pages = {}
    for year in years:
        entries = ml.extract_release_pages(index, fetched.final_url, year)
        if not entries:
            raise ValueError('Mainline release year has no pages')
        for p in entries:
            url = canonical_url(p.url)
            path = urlsplit(url).path
            if not path.startswith('/mainline/release/release-notes/'):
                raise ValueError('unexpected Mainline release navigation link')
            pages[url] = ml.ReleasePage(p.year, p.label, url)
    preload_entries=mainline_entries(fetched,depth)
    if not preload_entries:
        raise ValueError('no Mainline PRELOAD links discovered')
    preload={}
    for entry in mainline_entries(fetched,0):
        year,_,slug,url=entry
        url=canonical_url(url)
        pages.setdefault(url,ml.ReleasePage(year,slug,url))
    for entry in preload_entries:
        preload[canonical_url(entry[3])]=entry
    # Register discovered pages before fetching so cancellation is observable.
    for url in pages:
        fetcher.documents.setdefault(url,(0,'','pending',0))
        if fetcher.on_document:
            fetcher.on_document(url,fetcher.documents[url])
    results=[]
    packages=[]
    failures=[]
    for p in sorted(pages.values(), key=lambda x: (x.year, x.url), reverse=True):
        try:
            f=ml.fetch_page(fetcher,p.url,timeout)
            if fetcher.documents[p.url][2]!='ready':
                raise ValueError('release document redirected or missing article')
            issues=ml.extract_known_issues(p,f.doc)
            patches=ml.extract_security_patches(p,f.doc)
            if p.url in preload:
                packages.append(parse_mainline_package(fetched, preload[p.url], f.doc))
            results.append((p, f, issues, patches))
        except Exception as exc:
            failures.append((p.url,type(exc).__name__))
            code,digest,state,attempts=fetcher.documents[p.url]
            if state=='ready':
                fetcher.documents[p.url]=(code,digest,'parse_failed',attempts)
                if fetcher.on_document:
                    fetcher.on_document(p.url,fetcher.documents[p.url])
    if not results or not packages:
        raise ValueError(f'Mainline no usable release or PRELOAD evidence; {len(failures)}/{len(pages)} pages failed')
    return results, ParsedSource(mainline_packages=packages), failures


def business(row):
    return {k:v for k,v in dict(row).items() if k not in VOLATILE}


def publish_mainline(conn, run_id, results, stamp, *, complete):
    old_urls = {r[0] for r in conn.execute('SELECT source_url FROM mainline_release_note_pages')}
    urls = {p.url for p, *_ in results}
    if old_urls - urls and complete:
        raise ValueError('previously indexed Mainline documents missing from navigation')
    issues = ml.dedupe_issues([r for _,_,items,_ in results for r in items], set())
    patches = [r for _,_,_,items in results for r in items]
    total_changes = 0
    for table, keys, records, upsert, entity in (
        ('mainline_known_issues', ISSUE_KEYS, issues, mr.upsert_issues, 'mainline_exemption'),
        ('mainline_security_patches', CVE_KEYS, patches, mr.upsert_security_patches, 'mainline_cve'),
    ):
        def identity(row, keys=keys):
            return tuple(row[k] for k in keys)
        before = {identity(r):dict(r) for r in conn.execute(f'SELECT * FROM {table}')}
        after = {identity(asdict(r)):asdict(r) for r in records}
        if len(after) != len(records):
            raise ValueError('conflicting Mainline record identity')
        if complete and before and not after:
            raise ValueError('Mainline parser produced no records; previous evidence retained')
        if not complete:
            # An unavailable newer page must not be replaced by an older
            # occurrence of the same exemption. Never delete on partial coverage.
            after={k:r for k,r in after.items() if k not in before or before[k]['source_url'] in urls}
            records=[r for r in records if identity(asdict(r)) in after]
        for key in sorted(before.keys() | after.keys()):
            if not complete and key not in after:
                continue
            old, new = before.get(key), after.get(key)
            if old and new and business(old) == business(new):
                continue
            typ = 'added' if old is None else 'removed' if new is None else 'changed'
            record_change(conn, run_id=run_id, source_key=MAINLINE, entity_type=entity,
                entity_key=stable_hash(list(key))[:32], change_type=typ,
                before_json=json.dumps(business(old),ensure_ascii=False) if old else '',
                after_json=json.dumps(business(new),ensure_ascii=False) if new else '', detected_at=stamp)
            total_changes += 1
            if new is None:
                conn.execute(f'DELETE FROM {table} WHERE '+' AND '.join(k+'=?' for k in keys),key)
        upsert(conn, records, stamp)
    for p,f,items,_ in results:
        old = mr.get_page_state(conn,p.url)
        mr.upsert_page_state(conn,p,f,len(items),stamp,changed=not old or old['content_hash']!=f.content_hash)
    return len(issues), len(patches), total_changes


def record_documents(conn, run_id, source, docs):
    conn.executemany('''INSERT OR REPLACE INTO gms_update_documents
        (run_id,source_key,url,status_code,content_hash,state,attempts) VALUES(?,?,?,?,?,?,?)''',
        [(run_id,source,url,*value) for url,value in docs.items()])


def run_scan(path, mode='full', keys=None, *, trigger='manual', timeout=25,
             browser='auto', mainline_depth=0, session_factory=build_session):
    """Caller owns the single cross-process scan lock for the whole batch."""
    sources = select_sources(keys)
    if mode not in ('full','incremental'):
        raise ValueError('invalid scan mode')
    args = argparse.Namespace(browser=browser,cookie_file=None,cookie_header=None,verbose=False)
    errors = {}
    counts = dict(scanned=0,skipped=0,artifacts=0,packages=0,sections=0,rows=0,changes=0)
    with closing(connect(path)) as conn:
        run_id = begin_run(conn,mode,sources,trigger)
        try:
            with session_factory(args) as session:
                fetcher = BatchFetcher(session)
                for source in sources:
                    fetcher.begin_source()
                    def on_document(url,evidence,source_key=source.key):
                        with conn:
                            record_documents(conn,run_id,source_key,{url:evidence})
                            conn.execute('''UPDATE gms_update_source_results SET discovered=?,fetched=?
                                WHERE run_id=? AND source_key=?''',
                                (len(fetcher.documents),sum(v[2] in ('ready','parse_failed') for v in fetcher.documents.values()),run_id,source_key))
                    fetcher.on_document=on_document
                    with conn:
                        conn.execute("UPDATE gms_update_source_results SET state='running' WHERE run_id=? AND source_key=?",
                                     (run_id,source.key))
                    try:
                        fetched = fetch_source(fetcher,source,timeout)
                        if fetcher.documents[canonical_url(source.url)][2] != 'ready':
                            raise ValueError('source document redirected or missing article')
                        previous = conn.execute("""SELECT r.fingerprint,s.parser_version FROM gms_update_source_results r
                            JOIN gms_update_scan_runs s ON s.id=r.run_id
                            WHERE r.source_key=? AND r.state IN ('ready','unchanged') ORDER BY r.run_id DESC LIMIT 1""",
                            (source.key,)).fetchone()
                        parser = PARSERS[source.parser]
                        if (mode=='incremental' and source.key not in (MAINLINE,'gms_bulletins','gms_requirements')
                                and previous and previous['parser_version']==PARSER_VERSION
                                and previous['fingerprint']==stable_hash(sorted((u,v[1]) for u,v in fetcher.documents.items()))):
                            from .update_records import upsert_source
                            with conn:
                                upsert_source(conn,fetched,utc_now())
                                record_documents(conn,run_id,source.key,fetcher.documents)
                                conn.execute("""UPDATE gms_update_source_results SET state='unchanged',discovered=1,
                                    fetched=1,fingerprint=? WHERE run_id=? AND source_key=?""",
                                    (previous['fingerprint'],run_id,source.key))
                            counts['skipped']+=1
                            continue
                        # Mainline pages are shared by all three output categories.
                        results=[]
                        partial_error=''
                        if source.key==MAINLINE:
                            results,parsed,failures=mainline_data(fetched,fetcher,timeout,mainline_depth)
                            old_urls={r[0] for r in conn.execute('SELECT source_url FROM mainline_release_note_pages')}
                            discovered=set(fetcher.documents)
                            missing=old_urls-discovered
                            if failures or missing:
                                partial_error=f'Mainline coverage incomplete: {len(failures)} failed, {len(missing)} previously indexed pages absent; verified records published without deletion'
                        elif source.key=='gms_bulletins':
                            parsed=parser(fetched,session=fetcher,timeout=timeout)
                            if not parsed.bulletin_month_status or any(v!='ready' for v in parsed.bulletin_month_status.values()):
                                raise ValueError('Critical Fix monthly coverage incomplete')
                        else:
                            parsed=parser(fetched,session=fetcher)
                        fingerprint=stable_hash(sorted((u,v[1]) for u,v in fetcher.documents.items()))
                        origin=('partial' if partial_error else 'baseline' if previous is None else
                                'reindex' if previous['parser_version']!=PARSER_VERSION or previous['fingerprint']==fingerprint
                                else 'source_changed')
                        stamp=utc_now()
                        with conn:
                            _, changes, parsed=sync_source(conn,run_id,fetched,force=True,timestamp=stamp,
                                                           parsed=parsed,mainline_depth=mainline_depth)
                            extra=0
                            if source.key==MAINLINE:
                                issues,patches,additional=publish_mainline(conn,run_id,results,stamp,complete=not partial_error)
                                extra=issues+patches
                                changes+=additional
                            if source.key=='gms_bulletins' and not bulletin_detail_coverage(conn)['coverage_complete']:
                                raise ValueError('Critical Fix previous evidence not fully covered')
                            total=sum(len(getattr(parsed,f)) for f in ('artifacts','gms_packages','mainline_packages',
                                'bulletin_patches','requirement_sections','requirement_table_rows','requirement_version_tags'))+extra
                            conn.execute('UPDATE gms_update_change_events SET origin=? WHERE run_id=? AND source_key=?',
                                         (origin,run_id,source.key))
                            record_documents(conn,run_id,source.key,fetcher.documents)
                            conn.execute("""UPDATE gms_update_source_results SET state=?,discovered=?,fetched=?,
                                parsed=?,fingerprint=?,origin=?,error=? WHERE run_id=? AND source_key=?""",
                                ('partial' if partial_error else 'ready',len(fetcher.documents),
                                 sum(v[2] in ('ready','parse_failed') for v in fetcher.documents.values()),
                                 total,fingerprint,origin,partial_error,run_id,source.key))
                        if partial_error:
                            errors[source.key]=partial_error
                        else:
                            counts['scanned']+=1
                        counts['changes']+=changes
                        counts['artifacts']+=len(parsed.artifacts)
                        counts['packages']+=len(parsed.gms_packages)+len(parsed.mainline_packages)
                        counts['sections']+=len(parsed.requirement_sections)
                        counts['rows']+=len(parsed.requirement_table_rows)
                    except Exception as exc:
                        error=redact_sensitive_text(str(exc))[:1000]
                        errors[source.key]=error
                        with conn:
                            record_documents(conn,run_id,source.key,fetcher.documents)
                            conn.execute("""UPDATE gms_update_source_results SET state='failed',discovered=?,fetched=?,error=?
                                WHERE run_id=? AND source_key=?""",(len(fetcher.documents),
                                sum(v[2]=='ready' for v in fetcher.documents.values()),error,run_id,source.key))
        except Exception as exc:
            errors['scanner']=redact_sensitive_text(str(exc))[:1000]
        finally:
            state='completed' if not errors else 'partial' if (counts['scanned']+counts['skipped'] or conn.execute("SELECT 1 FROM gms_update_source_results WHERE run_id=? AND state='partial'",(run_id,)).fetchone()) else 'failed'
            with conn:
                conn.execute("""UPDATE gms_update_source_results SET state='failed',error='scan not executed'
                    WHERE run_id=? AND state IN ('pending','running')""",(run_id,))
                conn.execute("""UPDATE gms_update_scan_runs SET finished_at=?,sources_scanned=?,sources_skipped=?,
                    artifacts_total=?,packages_total=?,requirement_sections_total=?,requirement_table_rows_total=?,
                    changes_total=?,success=?,error=?,state=? WHERE id=?""",
                    (utc_now(),counts['scanned'],counts['skipped'],counts['artifacts'],counts['packages'],
                     counts['sections'],counts['rows'],counts['changes'],int(not errors),
                     json.dumps(errors,ensure_ascii=False),state,run_id))
    return {'run_id':run_id,'state':state,'errors':errors,**counts}
