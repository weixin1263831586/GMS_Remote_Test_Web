"""The deployed worker loads model credentials before feature imports."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_systemd_worker_loads_environment_before_importing_analysis(tmp_path):
    unit = (ROOT / 'deploy/systemd/gms-redmine-daily-brief-worker.service').read_text()
    command = shlex.split(next(line.split('=', 1)[1] for line in unit.splitlines()
                              if line.startswith('ExecStart=')))
    assert command[1:] == ['-m', 'bootstrap.run_daily_brief_worker']
    for relative in (
        'bootstrap/run_daily_brief_worker.py', 'bootstrap/env_loader.py',
        'foundation/config_paths.py', 'foundation/runtime_settings.py', 'foundation/private_config.py',
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    feature = tmp_path / 'features/redmine/daily_brief_worker.py'
    feature.parent.mkdir(parents=True)
    feature.write_text('''import os

seen_on_import = os.getenv("GMS_DEPLOYMENT_TEST_MODEL_KEY")

def main():
    assert seen_on_import == "synthetic-model-key"
    return 0
''')
    config = tmp_path / 'configs/secrets/environment.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'GMS_DEPLOYMENT_TEST_MODEL_KEY': 'synthetic-model-key'}))
    env = {**os.environ, 'PYTHONPATH': str(tmp_path), 'HOME': str(tmp_path)}
    for key in ('GMS_SKIP_RUNTIME_ENV', 'GMS_DATA_ROOT', 'GMS_DEPLOYMENT_TEST_MODEL_KEY'):
        env.pop(key, None)
    result = subprocess.run([sys.executable, *command[1:]], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
