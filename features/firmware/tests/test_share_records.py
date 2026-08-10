from pathlib import Path

from cryptography.fernet import Fernet

from features.firmware import shares_api


def test_loading_legacy_share_migrates_plaintext_password_at_rest(
    tmp_path: Path,
    monkeypatch,
):
    store = tmp_path / "shares.json"
    monkeypatch.setattr(shares_api, "_store_path", lambda: store)
    monkeypatch.setenv("GMS_SECRET_KEY", Fernet.generate_key().decode("ascii"))
    shares_api._save_records([{
        "id": "share1",
        "host": "192.0.2.10",
        "path": "/home/test/update.img",
        "password": "legacy-secret",
    }])

    records = shares_api._load_records()

    assert "password" not in records[0]
    assert shares_api._record_password(records[0]) == "legacy-secret"
    assert "legacy-secret" not in store.read_text(encoding="utf-8")
