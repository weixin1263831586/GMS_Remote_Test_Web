"""Exercise the Linux samples used by both heartbeat producers."""

from pathlib import Path

from foundation import host_metrics


def test_host_metrics_separate_cpu_usage_from_load_and_use_data_filesystem(monkeypatch, tmp_path):
    cpu_samples = iter(("cpu 20 0 10 65 5 0 0 0\n", "cpu 30 0 20 135 15 0 0 0\n"))
    disk_paths = []

    def read_proc(path):
        if path == Path("/proc/stat"):
            return next(cpu_samples)
        return "MemTotal: 4194304 kB\nMemAvailable: 1048576 kB\n"

    def read_disk(path):
        disk_paths.append(path)
        return {"disk_total_gb": 100.0, "disk_free_gb": 25.0, "disk_percent": 75}

    monkeypatch.setattr(host_metrics, "_last_cpu_sample", None)
    monkeypatch.setattr(Path, "read_text", read_proc)
    monkeypatch.setattr(host_metrics.os, "getloadavg", lambda: (7.0, 0.0, 0.0))
    monkeypatch.setattr(host_metrics, "disk_metrics", read_disk)

    data_root = tmp_path / "data"
    data_root.mkdir()
    assert host_metrics.collect_host_metrics(data_root)["cpu_percent"] == 0.0
    assert host_metrics.collect_host_metrics(data_root / "pending") == {
        "cpu_percent": 20.0,
        "memory_percent": 75.0,
        "memory_total_gb": 4.0,
        "memory_available_gb": 1.0,
        "load_1m": 7.0,
        "disk_total_gb": 100.0,
        "disk_free_gb": 25.0,
        "disk_percent": 75,
    }
    assert disk_paths == [data_root, data_root]
