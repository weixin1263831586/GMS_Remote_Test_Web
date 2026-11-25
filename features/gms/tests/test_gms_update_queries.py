"""Exemption keyword matches cannot turn into unrelated security-patch hits."""

import sqlite3

import pytest

from features.gms import init_gms_update_db, queries
from features.gms import mainline_queries as api


async def test_security_patch_keyword_does_not_correlate_every_cve(tmp_path, monkeypatch):
    database = tmp_path / "mainline.sqlite3"
    with sqlite3.connect(database) as conn:
        init_gms_update_db(conn)
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
    assert (await api.list_mainline_security_patches(q="568181732", release_year=None, mainline_module="", limit=100, offset=0))["total"] == 0
    assert (await api.list_mainline_security_patches(q="CVE-2026-58835", release_year=None, mainline_module="", limit=100, offset=0))["total"] == 1
    assert (await api.list_mainline_security_patches(
        q="", release_year=2026, mainline_module="bluetooth", limit=100, offset=0
    ))["total"] == 1
    assert (await api.list_mainline_security_patches(
        q="", release_year=2025, mainline_module="Bluetooth", limit=100, offset=0
    ))["total"] == 0
    assert (await api.list_mainline_security_patches(
        q="", release_year=2026, mainline_module="NFC", limit=100, offset=0
    ))["total"] == 0

async def test_update_change_keyword_escapes_literal_wildcards(tmp_path, monkeypatch):
    database = tmp_path / "updates.sqlite3"
    with sqlite3.connect(database) as conn:
        init_gms_update_db(conn)
        for key in ("Cts%_literal", "CtsXXliteral"):
            conn.execute(
                "INSERT INTO gms_update_change_events "
                "(run_id,source_key,entity_type,entity_key,change_type,before_json,after_json,detected_at) "
                "VALUES (1,'cts_downloads','artifact',?,'new','null','null','2026-11-25')",
                (key,),
            )
    monkeypatch.setattr(queries, "DB_PATH", database)
    result = await queries.list_gms_update_changes(
        run_id=None, source_key="", entity_type="", change_type="",
        q="%_", limit=100, offset=0,
    )
    assert result["meta"]["total"] == 1
    assert result["data"]["items"][0]["entity_key"] == "Cts%_literal"


@pytest.mark.parametrize("fail", [False, True])
async def test_summary_closes_database_on_success_and_failure(tmp_path, monkeypatch, fail):
    database = tmp_path / "updates.sqlite3"
    conn = queries.connect(database)
    if fail:
        conn.execute("DROP TABLE gms_update_scan_runs")
        conn.commit()
    monkeypatch.setattr(queries, "_get_db", lambda: conn)
    if fail:
        with pytest.raises(sqlite3.OperationalError):
            await queries.gms_update_summary()
    else:
        assert (await queries.gms_update_summary())["success"]
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


async def test_mainline_summary_closes_database(tmp_path, monkeypatch):
    conn = api.connect(tmp_path / "mainline.sqlite3")
    monkeypatch.setattr(api, "_connect_db", lambda: conn)
    await api.mainline_known_issues_summary()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
