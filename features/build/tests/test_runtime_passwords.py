"""Runtime credentials remain consistent across independent processes."""

import multiprocessing

from cryptography.fernet import Fernet

from features.build.runtime_passwords import RuntimePasswordStore


def _write_passwords(path, prefix, ready):
    store = RuntimePasswordStore(path)
    store.get("not-created")
    ready.wait(timeout=10)
    for index in range(3):
        store.set(f"{prefix}-{index}", "synthetic-password")


def test_existing_readers_observe_other_writers_and_revocation(tmp_path, monkeypatch):
    monkeypatch.setenv("GMS_SECRET_KEY", Fernet.generate_key().decode())
    path = tmp_path / "passwords.json"
    first, second = RuntimePasswordStore(path), RuntimePasswordStore(path)
    assert second.get("job-a") == ""
    first.set("job-a", "synthetic-password")
    assert second.get("job-a") == "synthetic-password"
    second.set("job-b", "another-synthetic-password")
    assert first.get("job-b") == "another-synthetic-password"
    second.pop("job-a")
    assert first.get("job-a") == ""
    assert RuntimePasswordStore(path).get("job-b") == "another-synthetic-password"


def test_concurrent_processes_preserve_every_job(tmp_path, monkeypatch):
    monkeypatch.setenv("GMS_SECRET_KEY", Fernet.generate_key().decode())
    path = tmp_path / "passwords.json"
    context = multiprocessing.get_context("spawn")
    ready = context.Barrier(2)
    workers = [context.Process(target=_write_passwords, args=(path, prefix, ready))
               for prefix in ("one", "two")]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
    store = RuntimePasswordStore(path)
    for prefix in ("one", "two"):
        for index in range(3):
            assert store.get(f"{prefix}-{index}") == "synthetic-password"
    assert "synthetic-password" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
