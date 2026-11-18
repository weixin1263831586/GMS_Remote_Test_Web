"""Locate the bundled tesseract OCR engine shipped with the project.

The project bundles tesseract binaries + shared libraries + chi_sim/eng traineddata
so that image OCR works in environments where apt/sudo is unavailable.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path


def _project_root() -> Path:
    # 从模块路径定位项目根目录，不依赖进程工作目录。
    return Path(__file__).resolve().parents[2]


def _tesseract_dir() -> Path:
    return _project_root() / "tools" / "tesseract"


@functools.lru_cache(maxsize=1)
def bundled_tesseract_cmd() -> str | None:
    """Return the path to the bundled tesseract binary, or None if missing.

    Cached: the binary location is fixed for the process lifetime, so the
    ``.exists()`` probe runs only once; later calls reuse the result.
    """
    cmd = _tesseract_dir() / "tesseract"
    return str(cmd) if cmd.exists() else None


def bundled_tesseract_env() -> dict[str, str]:
    """Return env vars needed for the bundled tesseract to run.

    Adds the bundled lib directory to LD_LIBRARY_PATH and sets TESSDATA_PREFIX.
    """
    root = _tesseract_dir()
    env = dict(os.environ)
    lib_path = str(root)
    for key in ("LD_LIBRARY_PATH", "PATH"):
        paths = env[key].split(os.pathsep) if env.get(key) else []
        env[key] = os.pathsep.join([lib_path, *(path for path in paths if path != lib_path)])
    env["TESSDATA_PREFIX"] = str(root / "tessdata")
    return env


# Cache only binary availability; repair environment variables on every call.
_configured: bool | None = None


def configure_bundled_tesseract() -> bool:
    """If a bundled tesseract exists, update process env so subprocess can find it.

    Idempotent: ``_configured`` 只记忆二进制探测结果（路径进程内固定）；
    而 LD_LIBRARY_PATH/TESSDATA_PREFIX 这类全局副作用**不能**随之 memoize
    ——测试或宿主代码里的 ``patch.dict(os.environ, clear=True)`` 恢复时会把
    这里写入的键一并抹掉，之后所有 tesseract 子进程都会 127
    （libtesseract.so.4 找不到）。因此每次调用都做 O(1) 检查并按需重设。
    """
    global _configured
    if _configured is None:
        _configured = bundled_tesseract_cmd() is not None
    if not _configured:
        return False
    env = bundled_tesseract_env()
    # Each variable may have been changed independently by the host. Repair
    # all required values without copying unrelated environment variables.
    for key in ("LD_LIBRARY_PATH", "PATH", "TESSDATA_PREFIX"):
        os.environ[key] = env[key]
    return True


@functools.lru_cache(maxsize=1)
def usable_tesseract_cmd() -> str | None:
    """Use a runnable bundled engine; fall back to distro Tesseract.

    The bundled Tesseract 4 binary may depend on unavailable old libtiff or
    giflib sonames on newer Linux distributions. Existence alone is not proof
    it can load. The system engine is preferred over disabling OCR entirely.
    """
    bundled = bundled_tesseract_cmd()
    if bundled:
        try:
            result = subprocess.run(
                [bundled, "--version"],
                env=bundled_tesseract_env(),
                capture_output=True, timeout=5, check=False,
            )
            if result.returncode == 0:
                return bundled
        except (OSError, subprocess.TimeoutExpired):
            pass
    return shutil.which("tesseract")


def configure_usable_tesseract() -> str | None:
    """Select a functional OCR binary and avoid mixing shared library stacks.

    A prior bundled OCR call can have prepended tools/tesseract to the process
    library search path. System Tesseract must not then import the bundled
    libtesseract/liblept, which may depend on removed libtiff.so.5.
    """
    cmd = usable_tesseract_cmd()
    if not cmd:
        return None
    if cmd == bundled_tesseract_cmd():
        configure_bundled_tesseract()
    else:
        root = str(_tesseract_dir())
        for key in ("LD_LIBRARY_PATH", "PATH"):
            if key in os.environ:
                values = [path for path in os.environ[key].split(os.pathsep)
                          if path != root]
                if values:
                    os.environ[key] = os.pathsep.join(values)
                else:
                    os.environ.pop(key, None)
        if os.environ.get("TESSDATA_PREFIX") == str(_tesseract_dir() / "tessdata"):
            os.environ.pop("TESSDATA_PREFIX", None)
    return cmd
