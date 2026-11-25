"""Read-only browser audit of the deployed update center; aggregate evidence only."""
import base64
import hashlib
import json
import subprocess
from pathlib import Path

from playwright.sync_api import sync_playwright

from tools.scripts.testing.audit_critical_fix_live import BASE, auth


TABS = {
    "changes": ("来源", "/api/gms-update-center/changes", ["关联标识", "变更摘要"]),
    "requirements": ("章节", "/api/gms-update-center/requirements", ["要求摘要", "Requirement ID"]),
    "packages": ("Gerrit Tag", "/api/gms-update-center/packages", ["Android", "文件"]),
    "mainline": ("PRELOAD版本", "/api/gms-update-center/mainline", ["Partner Zip", "Notes"]),
    "artifacts": ("套件文件名", "/api/gms-update-center/artifacts", ["套件", "Android"]),
    "exemptions": ("豁免ID", "/api/gms-update-center/exemptions", ["测试模块", "测试用例"]),
    "patch": ("CVE 编号", "/api/gms-update-center/security-patches", ["Mainline 模块"]),
    "critical": ("Gerrit合入", "/api/gms-update-center/critical-patches", ["补丁ID", "Android版本"]),
}

MEASURE = r"""() => {
  const table=document.querySelector('#tables-area table');
  const wrap=table.closest('.table-split'), scroll=table.closest('.table-scroll');
  const box=el=>{const r=el.getBoundingClientRect();return {x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height),bottom:Math.round(r.bottom),right:Math.round(r.right)}};
  const pager=document.querySelector('.pager'), toolbar=document.querySelector('.mainline-toolbar');
  const rows=[...table.tBodies[0].rows], data=rows.filter(r=>r.cells.length>1);
  const heads=[...table.tHead.rows[0].cells];
  const columns=heads.map((h,i)=>{
    const cells=data.map(r=>r.cells[i]).filter(Boolean);
    return {header:h.textContent.trim(),width:Math.round(h.getBoundingClientRect().width),
      clipped:cells.filter(c=>c.scrollWidth>c.clientWidth+2||[...c.querySelectorAll('button,code')].some(e=>e.scrollWidth>e.clientWidth+2)).length,
      detail:cells.filter(c=>c.querySelector('[data-record-detail],[data-critical-detail]')).length,
      empty:cells.filter(c=>!c.textContent.trim()||c.textContent.trim()==='—').length};
  });
  const heights=data.map(r=>Math.round(r.getBoundingClientRect().height));
  const tabs=document.querySelector('.tabs'), filters=document.querySelector('.filters');
  const badge=document.querySelector('#section-badge');
  const info=document.querySelector('#pageinfo').textContent.trim();
  const buttons=[...pager.querySelectorAll('button')];
  return {viewport:[innerWidth,innerHeight],table:box(wrap),pager:box(pager),toolbar:box(toolbar),
    tabs:box(tabs),filters:box(filters),gap:box(pager).y-box(wrap).bottom,
    table_horizontal_overflow:scroll.scrollWidth-scroll.clientWidth,
    toolbar_horizontal_overflow:toolbar.scrollWidth-toolbar.clientWidth,
    rows:data.length,columns,header_rows:table.tHead.rows.length,
    row_height:heights.length?[Math.min(...heights),Math.max(...heights)]:[],
    pageinfo:/^([0-9]+-[0-9]+|0) \/ [0-9]+$/.test(info)?info:'unexpected',
    previous_disabled:buttons[0].disabled,next_disabled:buttons[1].disabled,
    detail_buttons:table.querySelectorAll('[data-record-detail]').length,
    raw_json_rows:data.filter(r=>[...r.cells].some(c=>/^\s*\{"[^"]+":/.test(c.textContent))).length,
    warning_visible:!badge.hidden,
    gerrit_busy:document.querySelector('#gerrit-refresh')?.getAttribute('aria-busy')==='true',
    gerrit_states:[...table.querySelectorAll('.gerrit-cell .gerrit-badge')].reduce((a,e)=>{const k=[...e.classList].find(c=>/^gerrit-(merged|partial|open|missing|unknown)$/.test(c));if(k)a[k]=(a[k]||0)+1;return a},{}),
    body_overflow:document.documentElement.scrollWidth-innerWidth
  };
}"""


def emit(**data):
    print("UPDATE_CENTER_AUDIT=" + json.dumps(data, ensure_ascii=True), flush=True)


def main():
    host = Path.home() / "GMS_Remote_Test_Web"
    session = auth(host)
    if session is None:
        emit(stage="auth", ok=False)
        return 1
    # Chromium does not necessarily use the requests CA bundle. Trust only
    # the configured local certificate public keys, never every HTTPS site.
    pins = []
    for cert in ("gms-local-ca.crt", "gms-local.crt"):
        path = host / "configs/secrets/certs" / cert
        if path.is_file():
            key = subprocess.check_output(["openssl", "x509", "-in", str(path), "-pubkey", "-noout"])
            der = subprocess.check_output(["openssl", "pkey", "-pubin", "-outform", "DER"], input=key)
            pins.append(base64.b64encode(hashlib.sha256(der).digest()).decode("ascii"))
    failures = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--ignore-certificate-errors-spki-list=" + ",".join(pins)] if pins else [])
        try:
            for width, height in ((1792, 856), (1440, 900)):
                ctx = browser.new_context(viewport={"width": width, "height": height})
                ctx.add_cookies([{"name": c.name, "value": c.value, "url": BASE}
                                 for c in session.cookies])
                page = ctx.new_page()
                page_errors = []
                page.on("pageerror", lambda _, errors=page_errors: errors.append(1))
                for tab, (header, endpoint, expected) in TABS.items():
                    page_errors.clear()
                    phase = "navigation"
                    try:
                        # GET navigation only; do not click scan, download or recheck.
                        response = page.goto(BASE + "/gms-update-center?tab=" + tab,
                                             wait_until="domcontentloaded", timeout=25000)
                        phase = "wait_for_table"
                        page.wait_for_function(
                            r"""arg => [...document.querySelectorAll('#tables-area thead th')].some(h=>h.textContent.trim()===arg) && /^([0-9]+-[0-9]+|0) \/ [0-9]+$/.test(document.querySelector('#pageinfo').textContent.trim())""",
                            arg=header, timeout=30000,
                        )
                        phase = "measure"
                        initial = page.evaluate(MEASURE)
                        if tab == "critical" and initial["gerrit_busy"]:
                            try:
                                page.wait_for_function("document.querySelector('#gerrit-refresh').getAttribute('aria-busy')==='false'", timeout=100000)
                            except Exception:
                                pass
                        metrics = page.evaluate(MEASURE)
                        # Independent GET, compare live API totals to rendered pagination.
                        phase = "api_compare"
                        result = session.get(BASE + endpoint, params={"limit": 100, "offset": 0},
                                             timeout=25, allow_redirects=False)
                        result.raise_for_status()
                        payload = result.json()
                        api_items = payload.get("data", {}).get("items", payload.get("items", []))
                        api_total = payload.get("meta", {}).get("total", payload.get("total"))
                        headers = [c["header"] for c in metrics["columns"]]
                        expected_ok = all(v in headers for v in expected)
                        total_ok = api_total is not None and metrics["pageinfo"].split(" / ")[-1] == str(api_total)
                        emit(stage="tab", tab=tab, http=response.status if response else None,
                             metrics=metrics, api_total=api_total, api_rows=len(api_items),
                             total_match=total_ok, row_count_match=metrics["rows"]==len(api_items),
                             expected_headers=expected_ok, page_errors=len(page_errors),
                             initial_table_y=initial["table"]["y"], initial_columns=len(initial["columns"]),
                             initial_busy=initial["gerrit_busy"])
                        if not (expected_ok and total_ok and metrics["rows"]==len(api_items)) or page_errors:
                            failures += 1
                    except Exception as exc:
                        failures += 1
                        emit(stage="tab", tab=tab, viewport=[width,height], ok=False,
                             error_type=type(exc).__name__, phase=phase,
                             category=next((v for v in ("ERR_CERT_AUTHORITY_INVALID", "ERR_CERT_COMMON_NAME_INVALID", "ERR_CERT_DATE_INVALID", "ERR_CONNECTION_REFUSED", "ERR_ABORTED", "SyntaxError", "TypeError", "Timeout") if v in str(exc)), "other"))
                ctx.close()
        finally:
            browser.close()
            session.close()
    emit(stage="complete", failures=failures)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
