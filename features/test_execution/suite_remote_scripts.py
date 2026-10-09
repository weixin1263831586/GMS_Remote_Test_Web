"""Suite 远程脚本常量（从 suites_api.py 拆出，纯数据无逻辑）。"""

_SUITE_SCRIPT_PREAMBLE = r"""
import json, os, sys
root = os.path.realpath(sys.argv[1])
target = os.path.realpath(sys.argv[2])
def emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
if target != root and not target.startswith(root + os.sep):
    emit({"success": False, "error": "Illegal path"})
    sys.exit(0)
"""

SUITE_FILE_LIST_SCRIPT = _SUITE_SCRIPT_PREAMBLE + r"""
if not os.path.isdir(target):
    emit({"success": False, "error": "Directory not found"})
    sys.exit(0)
items = []
for name in sorted(os.listdir(target), key=lambda n: n.lower()):
    full_path = os.path.join(target, name)
    try:
        real_path = os.path.realpath(full_path)
        if real_path != root and not real_path.startswith(root + os.sep):
            continue
        st = os.stat(full_path)
        is_dir = os.path.isdir(full_path)
        rel = os.path.relpath(full_path, root)
        items.append({"name": name, "path": "" if rel == "." else rel, "type": "directory" if is_dir else "file", "size": 0 if is_dir else st.st_size, "modified": int(st.st_mtime), "is_apk": (not is_dir) and name.lower().endswith(".apk"), "is_jar": (not is_dir) and name.lower().endswith(".jar")})
    except OSError:
        continue
items.sort(key=lambda item: (item["type"] != "directory", item["name"].lower()))
emit({"success": True, "path": "" if target == root else os.path.relpath(target, root), "root": root, "items": items})
"""

SUITE_FILE_INFO_SCRIPT = _SUITE_SCRIPT_PREAMBLE + r"""
if not os.path.isfile(target):
    emit({"success": False, "error": "File not found"})
    sys.exit(0)
st = os.stat(target)
name_lower = target.lower()
emit({"success": True, "real_path": target, "name": os.path.basename(target), "size": st.st_size, "modified": int(st.st_mtime), "is_apk": name_lower.endswith(".apk"), "is_jar": name_lower.endswith(".jar")})
"""

# 把一个目录打包成远程临时 zip，返回 zip 路径与文件夹名。供「下载文件夹」用：
# 浏览器无法在一次响应里下载保持目录结构的多个文件，统一打包成 zip 流式回传，
# 解压后顶层即为被下载的文件夹名。
_ZIP_OPERATION_HELPERS = r"""
import contextlib, fcntl, re, shutil, signal, stat, tempfile, time
base = os.path.join(tempfile.gettempdir(), "gms-suite-downloads-" + str(os.getuid()))
os.makedirs(base, mode=0o700, exist_ok=True)
base_stat = os.lstat(base)
if not stat.S_ISDIR(base_stat.st_mode) or base_stat.st_uid != os.getuid():
    raise RuntimeError("Unsafe archive temporary directory")
os.chmod(base, 0o700)
operation = sys.argv[3]
if not re.fullmatch(r"[0-9a-f]{32}", operation):
    raise ValueError("Invalid archive operation")
work = os.path.join(base, operation)

def lock_file(directory):
    return os.fdopen(os.open(os.path.join(directory, "owner.lock"),
                            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a+b")

def reap_stale(age):
    cutoff = time.time() - age
    for entry in os.scandir(base):
        if not re.fullmatch(r"[0-9a-f]{32}", entry.name) or not entry.is_dir(follow_symlinks=False):
            continue
        if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
            continue
        try:
            with lock_file(entry.path) as owner:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                shutil.rmtree(entry.path)
        except (OSError, BlockingIOError):
            pass
    # Migrate leftovers made by the previous mkstemp-based implementation.
    for entry in os.scandir(tempfile.gettempdir()):
        if not re.fullmatch(r"suite_dl_[A-Za-z0-9_-]+\.zip", entry.name):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_mtime < cutoff:
                os.unlink(entry.path)
        except OSError:
            pass

def cancel_operation():
    os.makedirs(work, mode=0o700, exist_ok=True)
    with open(os.path.join(work, "cancelled"), "w"):
        pass
    with lock_file(work) as owner:
        pidfd = None
        try:
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # A pidfd binds cancellation to this process, avoiding PID
                # reuse. Older kernels rely on the script's own alarm and
                # cancellation marker instead of signalling an ambiguous PID.
                try:
                    with open(os.path.join(work, "pid.json")) as metadata:
                        info = json.load(metadata)
                    pidfd = os.pidfd_open(info["pid"])
                    with open("/proc/" + str(info["pid"]) + "/stat") as proc:
                        start = proc.read().rsplit(")", 1)[1].split()[19]
                    if start != info["start"]:
                        os.close(pidfd)
                        pidfd = None
                    else:
                        signal.pidfd_send_signal(pidfd, signal.SIGTERM)
                except (OSError, AttributeError, ValueError, KeyError):
                    if pidfd is not None:
                        os.close(pidfd)
                        pidfd = None
                deadline = time.monotonic() + 5
                killed = False
                while True:
                    try:
                        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            if pidfd is not None and not killed:
                                with contextlib.suppress(OSError):
                                    signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                                killed = True
                                deadline = time.monotonic() + 1
                            else:
                                return False
                        time.sleep(0.05)
            # Keep an early cancellation tombstone until stale collection if
            # the SSH command has not started yet. A delayed command sees it.
            if os.path.isfile(os.path.join(work, "pid.json")):
                shutil.rmtree(work, ignore_errors=True)
            return True
        finally:
            if pidfd is not None:
                os.close(pidfd)
"""

SUITE_DIR_CLEANUP_SCRIPT = "import json, os, sys\n" + _ZIP_OPERATION_HELPERS + r"""
try:
    success = cancel_operation()
except FileNotFoundError:
    success = True
sys.stdout.write(json.dumps({"success": success}))
"""

SUITE_DIR_ZIP_SCRIPT = _SUITE_SCRIPT_PREAMBLE + _ZIP_OPERATION_HELPERS + r"""
import zipfile
if not os.path.isdir(target):
    emit({"success": False, "error": "Directory not found"})
    sys.exit(0)
max_bytes, max_files, min_free, stale_age, timeout = map(int, sys.argv[4:9])
reap_stale(stale_age)
os.makedirs(work, mode=0o700, exist_ok=True)
owner = lock_file(work)
fcntl.flock(owner, fcntl.LOCK_EX)
with open("/proc/self/stat") as proc:
    start = proc.read().rsplit(")", 1)[1].split()[19]
with open(os.path.join(work, "pid.json"), "w") as metadata:
    json.dump({"pid": os.getpid(), "start": start}, metadata)
zip_path = os.path.join(work, "archive.zip")
completed = False

class LimitExceeded(Exception):
    pass

class DiskUnavailable(Exception):
    pass

def abort(_signal, _frame):
    raise TimeoutError("Remote archive operation interrupted or timed out")

def check_cancelled():
    if os.path.exists(os.path.join(work, "cancelled")):
        raise TimeoutError("Remote archive operation cancelled")

def ensure_disk(extra=0):
    if shutil.disk_usage(base).free < min_free + extra:
        raise DiskUnavailable("Insufficient temporary disk space for directory archive")

def files_inside():
    for current, dirs, files in os.walk(target):
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(current, name))]
        for name in files:
            check_cancelled()
            full_path = os.path.join(current, name)
            real_full = os.path.realpath(full_path)
            if real_full != root and not real_full.startswith(root + os.sep):
                continue
            if os.path.isfile(real_full):
                yield full_path, os.path.relpath(full_path, target)

signal.signal(signal.SIGALRM, abort)
signal.signal(signal.SIGTERM, abort)
signal.signal(signal.SIGHUP, abort)
signal.alarm(max(1, timeout))
try:
    count = total = 0
    for full_path, arc in files_inside():
        count += 1
        total += os.path.getsize(full_path)
        if count > max_files or total > max_bytes:
            raise LimitExceeded("Directory exceeds archive file-count or byte limit")
    ensure_disk()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        count = total = central_size = 0
        for full_path, arc in files_inside():
            count += 1
            if count > max_files:
                raise LimitExceeded("Directory exceeds archive file-count limit")
            entry = zipfile.ZipInfo.from_file(full_path, arc)
            entry.compress_type = zipfile.ZIP_DEFLATED
            central_size += 100 + len(arc.encode("utf-8"))
            with open(full_path, "rb") as source, zipf.open(entry, "w", force_zip64=True) as dest:
                while True:
                    check_cancelled()
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise LimitExceeded("Directory exceeds archive byte limit")
                    ensure_disk(len(chunk))
                    dest.write(chunk)
                    if zipf.fp.tell() > max_bytes:
                        raise LimitExceeded("Archive exceeds output byte limit")
            ensure_disk(central_size)
    st = os.stat(zip_path)
    if st.st_size > max_bytes:
        raise LimitExceeded("Archive exceeds output byte limit")
    emit({"success": True, "zip_path": zip_path, "name": os.path.basename(target), "size": st.st_size})
    completed = True
except Exception as e:
    code = ("DEPENDENCY_TIMEOUT" if isinstance(e, TimeoutError) else
            "INVALID_SEMANTICS" if isinstance(e, LimitExceeded) else
            "DEPENDENCY_UNAVAILABLE" if isinstance(e, DiskUnavailable) else "UPSTREAM_FAILURE")
    emit({"success": False, "error": str(e), "code": code})
finally:
    signal.alarm(0)
    if not completed:
        shutil.rmtree(work, ignore_errors=True)
    owner.close()
"""

SUITE_FILE_SEARCH_SCRIPT = _SUITE_SCRIPT_PREAMBLE + r"""
query = sys.argv[3].lower()
limit = int(sys.argv[4])
if not os.path.isdir(target):
    emit({"success": False, "error": "Directory not found"})
    sys.exit(0)
items = []
for current, dirs, files in os.walk(target):
    dirs[:] = [d for d in dirs if not d.startswith('.')]
    for name in sorted(dirs, key=str.lower):
        if query and query not in name.lower():
            continue
        full_path = os.path.join(current, name)
        rel = os.path.relpath(full_path, root)
        items.append({"name": name, "path": "" if rel == "." else rel, "type": "directory", "size": 0, "modified": int(os.path.getmtime(full_path))})
        if len(items) >= limit:
            emit({"success": True, "items": items})
            sys.exit(0)
    for name in sorted(files, key=str.lower):
        if query and query not in name.lower():
            continue
        full_path = os.path.join(current, name)
        try:
            st = os.stat(full_path)
        except OSError:
            continue
        rel = os.path.relpath(full_path, root)
        lower = name.lower()
        items.append({"name": name, "path": rel, "type": "file", "size": st.st_size, "modified": int(st.st_mtime), "is_apk": lower.endswith(".apk"), "is_jar": lower.endswith(".jar")})
        if len(items) >= limit:
            emit({"success": True, "items": items})
            sys.exit(0)
emit({"success": True, "items": items})
"""
