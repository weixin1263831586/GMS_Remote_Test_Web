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


def test_share_list_requires_elevated_admin_when_auth_required(
    tmp_path: Path, monkeypatch
):
    """GET /api/firmware-shares 与同文件其他路由一致：认证部署下需提权管理员。

    列表暴露内网 host/user/path；普通登录用户读取必须被 403 拒绝，
    开发模式（未启用认证）不受影响。
    """
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from features.auth import CurrentUser
    from features.firmware import runtime

    store = tmp_path / "shares.json"
    monkeypatch.setattr(shares_api, "_store_path", lambda: store)
    monkeypatch.setattr(
        runtime, "firmware_share_store", str(store)
    )

    app = FastAPI()
    app.include_router(shares_api.router)
    client = TestClient(app)

    # 开发模式：未启用认证 → 列表可读。
    monkeypatch.setattr(
        "features.auth.access.authentication_required", lambda: False
    )
    response = client.get("/api/firmware-shares")
    assert response.status_code == 200
    assert response.json()["data"]["records"] == []

    # 认证部署：普通用户无提权 → 403。
    monkeypatch.setattr(
        "features.auth.access.authentication_required", lambda: True
    )
    monkeypatch.setattr(
        "features.auth.access.require_elevated_admin",
        lambda _request: (_ for _ in ()).throw(HTTPException(status_code=403)),
    )
    response = client.get("/api/firmware-shares")
    assert response.status_code == 403

    # 认证部署：提权管理员 → 列表可读。
    monkeypatch.setattr(
        "features.auth.access.require_elevated_admin",
        lambda _request: CurrentUser(id="a1", username="admin", role="admin"),
    )
    response = client.get("/api/firmware-shares")
    assert response.status_code == 200
