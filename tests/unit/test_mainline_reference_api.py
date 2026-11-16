"""Exemption keyword matches cannot turn into unrelated security-patch hits."""

import sqlite3

from features.system import init_mainline_issues_db
from features.system.mainline_issues import api


async def test_security_patch_keyword_does_not_correlate_every_cve(tmp_path, monkeypatch):
    database = tmp_path / "mainline.sqlite3"
    with sqlite3.connect(database) as conn:
        init_mainline_issues_db(conn)
        conn.execute(
            "INSERT INTO mainline_known_issues (source_url,source_title,release_year,release_label,"
            "product_section,issue_type,android_versions,category,test_module,test_case,exemption_id,"
            "issue_text,first_seen_at,last_seen_at) VALUES ('https://example.com/exemption','Notes',2026,"
            "'October','Android','CTS','17','test','CtsBluetoothTestCases','pkg.Test#testFoo',"
            "'568181732','Unrelated exemption','2026-11-14','2026-11-14')"
        )
        conn.execute(
            "INSERT INTO mainline_security_patches (source_url,source_title,release_year,release_label,"
            "product_section,cve_id,mainline_module,version_note,first_seen_at,last_seen_at) "
            "VALUES ('https://example.com/patch','Notes',2026,'October','Android',"
            "'CVE-2026-58835','Bluetooth','Fix ag/40830952','2026-11-14','2026-11-14')"
        )
    monkeypatch.setattr(api, "DB_PATH", database)
    assert (await api.list_mainline_security_patches(q="568181732", limit=100, offset=0))["total"] == 0
    assert (await api.list_mainline_security_patches(q="CVE-2026-58835", limit=100, offset=0))["total"] == 1
