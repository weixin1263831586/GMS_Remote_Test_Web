"""APK 分析辅助函数 - 任务管理、符号索引、JADX反编译"""

import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple


from core.api_response import ApiResponse
from core.settings import APK_MAX_TASKS, APK_UPLOAD_DIR, JADX_PATH, JADX_TIMEOUT
from core.state import global_state
from core.upload_utils import safe_upload_target_path

logger = logging.getLogger(__name__)

ANDROID_NS = 'http://schemas.android.com/apk/res/android'
JAVA_IDENTIFIER_RE = r'[A-Za-z_$][A-Za-z0-9_$]*'
JAVA_CLASS_DEF_RE = re.compile(
    rf'\b(?:public|protected|private|static|final|abstract|\s)*(class|interface|enum)\s+({JAVA_IDENTIFIER_RE})\b')
JAVA_METHOD_DEF_RE = re.compile(
    rf'\b(?:public|protected|private|static|final|synchronized|native|abstract|strictfp|\s)+[\w$<>\[\].?,\s]+\s+({JAVA_IDENTIFIER_RE})\s*\([^;]*\)\s*(?:throws\b[^{{;]*)?(?:\{{|$)')
JAVA_FIELD_DEF_RE = re.compile(
    rf'\b(?:public|protected|private|static|final|volatile|transient|\s)+[\w$<>\[\].?,\s]+\s+({JAVA_IDENTIFIER_RE})\s*(?:=|;|,)')
JAVA_LOCAL_DEF_RE = re.compile(
    rf'\b(?:final\s+)?(?:[A-Za-z_$][\w$<>.\[\]?]*)(?:\s*<[^;=()]+>)?(?:\[\])?\s+({JAVA_IDENTIFIER_RE})\s*(?:=|;|,)')
JAVA_CONTROL_WORDS = {'if', 'for', 'while', 'switch', 'catch', 'return', 'throw', 'new', 'case', 'do', 'else', 'try', 'finally'}
APK_SYMBOL_INDEX_MAX_FILE_SIZE = 2 * 1024 * 1024


def _create_apk_task(task_id, apk_path, filename):
    """创建APK分析任务并限制总数"""
    os.makedirs(APK_UPLOAD_DIR, exist_ok=True)
    with global_state.apk_analysis_tasks_lock:
        if len(global_state.apk_analysis_tasks) >= APK_MAX_TASKS:
            oldest = min(global_state.apk_analysis_tasks.items(), key=lambda t: t[1].get('timestamp', 0))
            old_dir = os.path.join(APK_UPLOAD_DIR, oldest[0])
            shutil.rmtree(old_dir, ignore_errors=True)
            del global_state.apk_analysis_tasks[oldest[0]]
        global_state.apk_analysis_tasks[task_id] = {
            'status': 'uploaded', 'progress': 0,
            'apk_path': apk_path, 'output_dir': None,
            'filename': filename, 'timestamp': time.time(), 'error': None
        }


def _get_apk_upload_lock(task_id: str) -> asyncio.Lock:
    with global_state.apk_upload_locks_lock:
        return global_state.apk_upload_locks.setdefault(task_id, asyncio.Lock())


def _safe_join(base_dir: str, *parts: str) -> str:
    """Join paths and ensure the result stays under base_dir."""
    return safe_upload_target_path(base_dir, os.path.join(*parts) if parts else '.', allow_nested=True)


def _normalize_apk_filename(filename: Optional[str]) -> str:
    """Normalize upload filenames to a safe APK/JAR basename."""
    raw_name = (filename or '').replace('\\', '/')
    basename = os.path.basename(raw_name).strip()
    if not basename or basename in ('.', '..'):
        raise ValueError("文件名无效")
    if not (basename.lower().endswith('.apk') or basename.lower().endswith('.jar')):
        raise ValueError("仅支持 .apk 和 .jar 文件")

    stem, ext = os.path.splitext(basename)
    stem = re.sub(r'[^A-Za-z0-9._ -]+', '_', stem).strip(' ._') or 'app'
    return f"{stem}{ext.lower()}"


def _normalize_apk_task_id(upload_id: Optional[str]) -> str:
    """Use UUID task directories only; never trust path-like upload IDs."""
    if not upload_id:
        return str(uuid.uuid4())
    try:
        return str(uuid.UUID(str(upload_id)))
    except (TypeError, ValueError):
        raise ValueError("非法上传ID")


def _cleanup_files(paths: List[str]):
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"Failed to remove temporary APK file {path}: {e}")


def _get_apk_task(task_id: str, require_completed: bool = True):
    """获取APK分析任务，返回 (task, error_response)"""
    with global_state.apk_analysis_tasks_lock:
        task = global_state.apk_analysis_tasks.get(task_id)
    if not task:
        return None, ApiResponse.error("任务不存在", status_code=404)
    if require_completed and task['status'] != 'completed':
        return None, ApiResponse.error("分析尚未完成", status_code=400)
    return task, None


def _read_manifest_xml(task):
    """读取并返回 AndroidManifest.xml 的原始内容"""
    manifest_path = os.path.join(task.get('output_dir', ''), 'resources', 'AndroidManifest.xml')
    if not os.path.exists(manifest_path):
        return None, "AndroidManifest.xml 未找到"
    with open(manifest_path, 'r', encoding='utf-8') as f:
        return f.read(), None


def _get_apk_sources_dir(task) -> str:
    return _safe_join(task.get('output_dir', ''), 'sources')


def _add_apk_symbol(symbols: Dict[str, List[Dict[str, Any]]], name: str, kind: str, path: str, line: int, column: int):
    if not name or name in JAVA_CONTROL_WORDS:
        return
    symbols.setdefault(name, []).append({
        'name': name, 'kind': kind, 'path': path, 'line': line, 'column': column,
    })


def _index_java_source_file(sources_dir: str, file_path: str, symbols: Dict[str, List[Dict[str, Any]]]):
    if os.path.getsize(file_path) > APK_SYMBOL_INDEX_MAX_FILE_SIZE:
        return

    rel_path = os.path.relpath(file_path, sources_dir)
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            for line_no, line in enumerate(f, start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith('//') or stripped.startswith('*') or stripped.startswith('/*'):
                    continue

                for match in JAVA_CLASS_DEF_RE.finditer(line):
                    _add_apk_symbol(symbols, match.group(2), match.group(1), rel_path, line_no, match.start(2) + 1)

                method_match = JAVA_METHOD_DEF_RE.search(line)
                if method_match:
                    name = method_match.group(1)
                    if name not in JAVA_CONTROL_WORDS:
                        _add_apk_symbol(symbols, name, 'method', rel_path, line_no, method_match.start(1) + 1)
                    continue

                for match in JAVA_FIELD_DEF_RE.finditer(line):
                    _add_apk_symbol(symbols, match.group(1), 'field', rel_path, line_no, match.start(1) + 1)

                for match in JAVA_LOCAL_DEF_RE.finditer(line):
                    name = match.group(1)
                    prefix = line[:match.start(1)].strip()
                    if '(' in prefix and ')' not in prefix:
                        continue
                    _add_apk_symbol(symbols, name, 'local', rel_path, line_no, match.start(1) + 1)
    except UnicodeError:
        logger.warning(f"Failed to decode Java source for APK symbol index: {file_path}")


def _build_apk_symbol_index(task_id: str, task: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    with global_state.apk_analysis_tasks_lock:
        cached = global_state.apk_analysis_tasks.get(task_id, {}).get('symbol_index')
        if cached is not None:
            return cached

    sources_dir = _get_apk_sources_dir(task)
    symbols: Dict[str, List[Dict[str, Any]]] = {}
    if not os.path.isdir(sources_dir):
        return symbols

    for root, _, files in os.walk(sources_dir):
        for filename in files:
            if filename.endswith('.java'):
                _index_java_source_file(sources_dir, os.path.join(root, filename), symbols)

    with global_state.apk_analysis_tasks_lock:
        if task_id in global_state.apk_analysis_tasks:
            global_state.apk_analysis_tasks[task_id]['symbol_index'] = symbols
    return symbols


def _score_apk_symbol_candidate(candidate: Dict[str, Any], current_path: str, current_line: int) -> Tuple[int, int]:
    score = 0
    if candidate.get('path') == current_path:
        score += 100
        if current_line:
            score -= abs(candidate.get('line', 0) - current_line)
    if candidate.get('kind') in ('method', 'field', 'class', 'interface', 'enum'):
        score += 20
    return score, -candidate.get('line', 0)


async def _run_jadx_analysis(task_id: str, apk_path: str, output_dir: str):
    """后台运行 jadx 反编译"""
    try:
        with global_state.apk_analysis_tasks_lock:
            if task_id in global_state.apk_analysis_tasks:
                global_state.apk_analysis_tasks[task_id]['status'] = 'analyzing'
                global_state.apk_analysis_tasks[task_id]['progress'] = 10
                global_state.apk_analysis_tasks[task_id]['error'] = None

        jadx_threads = min(max(os.cpu_count() or 2, 2), 8)
        cmd = [
            JADX_PATH,
            '-d', output_dir,
            '-j', str(jadx_threads),
            '-m', 'simple',
            '--log-level', 'error',
            '--no-debug-info',
            '--comments-level', 'none',
            '-Pdex-input.verify-checksum=no',
            apk_path
        ]
        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, timeout=JADX_TIMEOUT
        )

        if result.returncode != 0:
            with global_state.apk_analysis_tasks_lock:
                if task_id in global_state.apk_analysis_tasks:
                    global_state.apk_analysis_tasks[task_id]['status'] = 'error'
                    global_state.apk_analysis_tasks[task_id]['error'] = result.stderr[-500:] if result.stderr else 'jadx 反编译失败'
            return

        with global_state.apk_analysis_tasks_lock:
            if task_id in global_state.apk_analysis_tasks:
                global_state.apk_analysis_tasks[task_id]['status'] = 'completed'
                global_state.apk_analysis_tasks[task_id]['progress'] = 100
                global_state.apk_analysis_tasks[task_id]['output_dir'] = output_dir
    except subprocess.TimeoutExpired:
        with global_state.apk_analysis_tasks_lock:
            if task_id in global_state.apk_analysis_tasks:
                global_state.apk_analysis_tasks[task_id]['status'] = 'error'
                global_state.apk_analysis_tasks[task_id]['error'] = 'jadx 反编译超时（超过600秒）'
    except Exception as e:
        with global_state.apk_analysis_tasks_lock:
            if task_id in global_state.apk_analysis_tasks:
                global_state.apk_analysis_tasks[task_id]['status'] = 'error'
                global_state.apk_analysis_tasks[task_id]['error'] = str(e)
