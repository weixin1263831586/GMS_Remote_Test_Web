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
SUITE_DIR_ZIP_SCRIPT = _SUITE_SCRIPT_PREAMBLE + r"""
import tempfile, zipfile
if not os.path.isdir(target):
    emit({"success": False, "error": "Directory not found"})
    sys.exit(0)
fd, zip_path = tempfile.mkstemp(suffix=".zip", prefix="suite_dl_")
os.close(fd)
try:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for current, dirs, files in os.walk(target):
            for name in files:
                full_path = os.path.join(current, name)
                real_full = os.path.realpath(full_path)
                if real_full != root and not real_full.startswith(root + os.sep):
                    continue
                arc = os.path.relpath(full_path, target)
                zipf.write(full_path, arc)
    st = os.stat(zip_path)
    emit({"success": True, "zip_path": zip_path, "name": os.path.basename(target), "size": st.st_size})
except Exception as e:
    try:
        os.remove(zip_path)
    except OSError:
        pass
    emit({"success": False, "error": str(e)})
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
