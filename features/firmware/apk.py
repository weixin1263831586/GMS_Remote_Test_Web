"""APK 分析辅助函数 - 任务管理、符号索引、JADX反编译"""

import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from typing import Any

from foundation.uploads import safe_upload_target_path

from . import runtime
from .responses import ApiResponse


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


def _create_apk_task(task_id, apk_path, filename, owner_id: str):
    """创建APK分析任务并限制总数"""
    os.makedirs(runtime.apk_upload_dir, exist_ok=True)
    with runtime.global_state.apk_analysis_tasks_lock:
        if len(runtime.global_state.apk_analysis_tasks) >= runtime.apk_max_tasks:
            removable = [
                item
                for item in runtime.global_state.apk_analysis_tasks.items()
                if item[1].get('status') != 'analyzing'
            ]
            if not removable:
                raise ValueError('APK analysis capacity is full; retry later')
            oldest = min(removable, key=lambda t: t[1].get('timestamp', 0))
            old_dir = _safe_join(runtime.apk_upload_dir, oldest[0])
            shutil.rmtree(old_dir, ignore_errors=True)
            del runtime.global_state.apk_analysis_tasks[oldest[0]]
            if runtime.apk_task_store is not None:
                runtime.apk_task_store.delete(oldest[0])
        runtime.global_state.apk_analysis_tasks[task_id] = {
            'status': 'uploaded', 'progress': 0,
            'apk_path': apk_path, 'output_dir': None,
            'filename': filename, 'timestamp': time.time(), 'error': None,
            'owner_id': owner_id,
        }
        _persist_apk_task_locked(task_id)


def _persist_apk_task_locked(task_id: str) -> None:
    if runtime.apk_task_store is None:
        return
    task = runtime.global_state.apk_analysis_tasks.get(task_id)
    if task is not None:
        runtime.apk_task_store.upsert(task_id, task)


def _persist_apk_task(task_id: str) -> None:
    with runtime.global_state.apk_analysis_tasks_lock:
        _persist_apk_task_locked(task_id)


def _get_apk_upload_lock(task_id: str) -> asyncio.Lock:
    with runtime.global_state.apk_upload_locks_lock:
        return runtime.global_state.apk_upload_locks.setdefault(task_id, asyncio.Lock())


def _safe_join(base_dir: str, *parts: str) -> str:
    """Join paths and ensure the result stays under base_dir."""
    return safe_upload_target_path(base_dir, os.path.join(*parts) if parts else '.', allow_nested=True)


def _normalize_apk_filename(filename: str | None) -> str:
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


def _normalize_apk_task_id(upload_id: str | None) -> str:
    """Use UUID task directories only; never trust path-like upload IDs."""
    if not upload_id:
        return str(uuid.uuid4())
    try:
        return str(uuid.UUID(str(upload_id)))
    except (TypeError, ValueError) as exc:
        raise ValueError("非法上传ID") from exc


def _cleanup_files(paths: list[str]):
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"Failed to remove temporary APK file {path}: {e}")


def _get_apk_task(
    task_id: str,
    require_completed: bool = True,
    owner_id: str | None = None,
):
    """获取APK分析任务，返回 (task, error_response)"""
    try:
        task_id = _normalize_apk_task_id(task_id)
    except ValueError as e:
        return None, ApiResponse.error(str(e), status_code=400)

    with runtime.global_state.apk_analysis_tasks_lock:
        task = runtime.global_state.apk_analysis_tasks.get(task_id)
        if task is None and runtime.apk_task_store is not None:
            task = runtime.apk_task_store.get(task_id)
            if task is not None:
                runtime.global_state.apk_analysis_tasks[task_id] = task
    if not task:
        return None, ApiResponse.error("任务不存在", status_code=404)
    if owner_id is not None and task.get('owner_id') != owner_id:
        return None, ApiResponse.error("任务不存在", status_code=404)
    if require_completed and task['status'] != 'completed':
        return None, ApiResponse.error("分析尚未完成", status_code=400)
    return task, None


def _read_manifest_xml(task):
    """读取并返回 AndroidManifest.xml 的原始内容"""
    manifest_path = os.path.join(task.get('output_dir', ''), 'resources', 'AndroidManifest.xml')
    if not os.path.exists(manifest_path):
        return None, "AndroidManifest.xml 未找到"
    with open(manifest_path, encoding='utf-8') as f:
        return f.read(), None


def _get_apk_sources_dir(task) -> str:
    return _safe_join(task.get('output_dir', ''), 'sources')


def _add_apk_symbol(symbols: dict[str, list[dict[str, Any]]], name: str, kind: str, path: str, line: int, column: int):
    if not name or name in JAVA_CONTROL_WORDS:
        return
    symbols.setdefault(name, []).append({
        'name': name, 'kind': kind, 'path': path, 'line': line, 'column': column,
    })


def _index_java_source_file(sources_dir: str, file_path: str, symbols: dict[str, list[dict[str, Any]]]):
    if os.path.getsize(file_path) > APK_SYMBOL_INDEX_MAX_FILE_SIZE:
        return

    rel_path = os.path.relpath(file_path, sources_dir)
    try:
        with open(file_path, encoding='utf-8', errors='replace') as f:
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


def _build_apk_symbol_index(task_id: str, task: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    with runtime.global_state.apk_analysis_tasks_lock:
        cached = runtime.global_state.apk_analysis_tasks.get(task_id, {}).get('symbol_index')
        if cached is not None:
            return cached

    sources_dir = _get_apk_sources_dir(task)
    symbols: dict[str, list[dict[str, Any]]] = {}
    if not os.path.isdir(sources_dir):
        return symbols

    for root, _, files in os.walk(sources_dir):
        for filename in files:
            if filename.endswith('.java'):
                _index_java_source_file(sources_dir, os.path.join(root, filename), symbols)

    with runtime.global_state.apk_analysis_tasks_lock:
        if task_id in runtime.global_state.apk_analysis_tasks:
            runtime.global_state.apk_analysis_tasks[task_id]['symbol_index'] = symbols
    return symbols


def _score_apk_symbol_candidate(candidate: dict[str, Any], current_path: str, current_line: int) -> tuple[int, int]:
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
        with runtime.global_state.apk_analysis_tasks_lock:
            if task_id in runtime.global_state.apk_analysis_tasks:
                runtime.global_state.apk_analysis_tasks[task_id]['status'] = 'analyzing'
                runtime.global_state.apk_analysis_tasks[task_id]['progress'] = 10
                runtime.global_state.apk_analysis_tasks[task_id]['error'] = None
                _persist_apk_task_locked(task_id)

        jadx_threads = min(max(os.cpu_count() or 2, 2), 8)
        cmd = [
            runtime.jadx_path,
            '-d', output_dir,
            '-j', str(jadx_threads),
            '-m', 'simple',
            '--log-level', 'error',
            '--no-debug-info',
            '--comments-level', 'none',
            '-Pdex-input.verify-checksum=no',
            apk_path
        ]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=runtime.jadx_timeout
            )
        except asyncio.TimeoutError as exc:
            process.terminate()
            await process.wait()
            raise subprocess.TimeoutExpired(
                cmd,
                runtime.jadx_timeout,
            ) from exc
        except asyncio.CancelledError:
            process.terminate()
            await process.wait()
            with runtime.global_state.apk_analysis_tasks_lock:
                if task_id in runtime.global_state.apk_analysis_tasks:
                    runtime.global_state.apk_analysis_tasks[task_id].update({
                        'status': 'uploaded',
                        'progress': 0,
                        'error': 'Analysis interrupted by Controller shutdown',
                    })
                    _persist_apk_task_locked(task_id)
            raise

        if process.returncode != 0:
            with runtime.global_state.apk_analysis_tasks_lock:
                if task_id in runtime.global_state.apk_analysis_tasks:
                    runtime.global_state.apk_analysis_tasks[task_id]['status'] = 'error'
                    decoded_error = stderr.decode('utf-8', errors='replace')
                    runtime.global_state.apk_analysis_tasks[task_id]['error'] = decoded_error[-500:] if decoded_error else 'jadx 反编译失败'
                    _persist_apk_task_locked(task_id)
            return

        with runtime.global_state.apk_analysis_tasks_lock:
            if task_id in runtime.global_state.apk_analysis_tasks:
                runtime.global_state.apk_analysis_tasks[task_id]['status'] = 'completed'
                runtime.global_state.apk_analysis_tasks[task_id]['progress'] = 100
                runtime.global_state.apk_analysis_tasks[task_id]['output_dir'] = output_dir
                _persist_apk_task_locked(task_id)
    except subprocess.TimeoutExpired:
        with runtime.global_state.apk_analysis_tasks_lock:
            if task_id in runtime.global_state.apk_analysis_tasks:
                runtime.global_state.apk_analysis_tasks[task_id]['status'] = 'error'
                runtime.global_state.apk_analysis_tasks[task_id]['error'] = 'jadx 反编译超时（超过600秒）'
                _persist_apk_task_locked(task_id)
    except Exception as e:
        with runtime.global_state.apk_analysis_tasks_lock:
            if task_id in runtime.global_state.apk_analysis_tasks:
                runtime.global_state.apk_analysis_tasks[task_id]['status'] = 'error'
                runtime.global_state.apk_analysis_tasks[task_id]['error'] = str(e)
                _persist_apk_task_locked(task_id)


def recover_apk_analysis_tasks() -> list[asyncio.Task]:
    """Resume interrupted JADX tasks after the Controller has acquired its lock."""

    recovered: list[asyncio.Task] = []
    with runtime.global_state.apk_analysis_tasks_lock:
        candidates = [
            (task_id, dict(task))
            for task_id, task in runtime.global_state.apk_analysis_tasks.items()
            if task.get('status') == 'analyzing'
        ]
    for task_id, task in candidates:
        apk_path = str(task.get('apk_path') or '')
        output_dir = str(task.get('output_dir') or '')
        try:
            task_root = os.path.realpath(
                _safe_join(runtime.apk_upload_dir, _normalize_apk_task_id(task_id))
            )
            resolved_apk = os.path.realpath(apk_path)
            safe_output = _safe_join(
                runtime.apk_upload_dir,
                task_id,
                'jadx_output',
            )
        except ValueError:
            task_root = ''
            resolved_apk = ''
            safe_output = ''
        if (
            not os.path.isfile(resolved_apk)
            or not resolved_apk.startswith(task_root + os.sep)
            or not resolved_apk.lower().endswith(('.apk', '.jar'))
            or os.path.realpath(output_dir) != os.path.realpath(safe_output)
        ):
            with runtime.global_state.apk_analysis_tasks_lock:
                current = runtime.global_state.apk_analysis_tasks[task_id]
                current.update({
                    'status': 'error',
                    'error': 'Interrupted analysis files failed integrity validation',
                })
                _persist_apk_task_locked(task_id)
            continue
        shutil.rmtree(safe_output, ignore_errors=True)
        task_handle = asyncio.create_task(
            _run_jadx_analysis(task_id, apk_path, safe_output)
        )
        runtime.global_state.background_tasks.add(task_handle)
        task_handle.add_done_callback(runtime.global_state.background_tasks.discard)
        recovered.append(task_handle)
    return recovered
