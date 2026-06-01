"""Helpers for discovering and normalizing local test suites."""

import logging
import os
import re
from typing import Any, Dict, List, Optional

from core.common_utils import CommonUtils
from core.config import config_manager

logger = logging.getLogger(__name__)

TRADEFED_BINARY_MAP = {
    'cts': 'cts-tradefed',
    'gsi': 'cts-tradefed',
    'gts': 'gts-tradefed',
    'sts': 'sts-tradefed',
    'vts': 'vts-tradefed',
    'xts': 'xts-tradefed'
}

SPECIAL_TEST_TYPES = {
    'cts-v-host-tradefed': 'cts-v',
    'apts-tradefed': 'apts',
    'gts-root-tradefed': 'gts-root',
}

TRADEFED_BINARY_REVERSE_MAP = {v: k for k, v in TRADEFED_BINARY_MAP.items()}
SUITE_TYPE_PATTERN = re.compile(r'/android-([a-z]+)')
TRADEFED_BINARY_LIST = list(set(TRADEFED_BINARY_MAP.values()))
TEST_TYPE_DETECTION_PRIORITY = ['vts', 'gts', 'sts', 'cts']


def get_test_type_from_binary(binary_name: str) -> str:
    """Return the suite type for a tradefed launcher name."""
    if result := SPECIAL_TEST_TYPES.get(binary_name):
        return result
    if result := TRADEFED_BINARY_REVERSE_MAP.get(binary_name):
        return result
    return binary_name.replace('-tradefed', '')


def detect_test_type_from_suite_path(suite_path: str) -> Optional[str]:
    """Detect suite type from a suite tools path."""
    if not suite_path:
        return None

    suite_match = SUITE_TYPE_PATTERN.search(suite_path.lower())
    if suite_match:
        detected_type = suite_match.group(1)
        if detected_type in TRADEFED_BINARY_MAP or detected_type in SPECIAL_TEST_TYPES:
            return detected_type
    return None


def detect_test_type_from_dir_path(dir_path: str) -> Optional[str]:
    """Detect suite type from a result or retry directory path."""
    if not dir_path:
        return None

    dir_lower = dir_path.lower()
    for test_type in TEST_TYPE_DETECTION_PRIORITY:
        if test_type in dir_lower:
            if test_type == 'cts' and 'gts' in dir_lower:
                continue
            return test_type
    return None


def get_default_suites_path(config: Dict[str, Any]) -> str:
    """Get default suites path from config or environment."""
    ubuntu_user = config_manager.get_ubuntu_user(config)
    return config.get('suites_path', f"/home/{ubuntu_user}/GMS-Suite")


def is_config_host_local(config: Dict[str, Any]) -> bool:
    """Return whether the configured Ubuntu host is local."""
    return CommonUtils.is_local_host(config_manager.get_ubuntu_host(config))


def get_effective_local_server(client_id: str, requested_local_server: str = "") -> str:
    """Resolve the callback host for the current client."""
    if requested_local_server:
        return requested_local_server

    dynamic_config = config_manager.get_runtime_config()
    dynamic_local_server = dynamic_config.get('local_server')
    if dynamic_local_server:
        return dynamic_local_server

    return client_id


def build_suite_info(full_path: str) -> Optional[Dict[str, str]]:
    """Build suite info from a tradefed binary path."""
    full_path = full_path.strip()
    if not full_path:
        return None

    parts = full_path.split('/')
    tradefed_name = parts[-1]
    test_type = get_test_type_from_binary(tradefed_name)
    tools_dir = '/'.join(parts[:-1])

    if test_type == 'cts-v':
        for i, part in enumerate(parts):
            if part == 'android-cts-verifier':
                tools_dir = '/'.join(parts[:i + 1])
                break

    version_dir = next(
        (
            p for p in parts
            if p.startswith('android-')
            and (
                test_type in p
                or (test_type == 'gsi' and 'cts' in p)
                or (test_type == 'gts-root' and 'gts' in p)
            )
        ),
        ""
    )
    if test_type == 'gsi' and version_dir:
        test_type = 'cts'
    if test_type == 'gts-root' and version_dir:
        test_type = 'gts'

    return {
        'test_type': test_type,
        'version': version_dir,
        'tools_path': tools_dir,
        'full_path': full_path,
        'binary': tradefed_name
    }


def ensure_tradefed_executable(full_path: str) -> bool:
    """Ensure extracted tradefed launchers are executable."""
    if os.access(full_path, os.X_OK):
        return True
    try:
        current_mode = os.stat(full_path).st_mode
        os.chmod(full_path, current_mode | 0o111)
        return os.access(full_path, os.X_OK)
    except Exception as e:
        logger.warning(f"[TestSuites] Failed to chmod tradefed launcher {full_path}: {e}")
        return False


def list_local_test_suites(base_path: str) -> List[Dict[str, str]]:
    """Discover locally available test suites below base_path."""
    suites = []
    if not os.path.isdir(base_path):
        logger.info(f"[TestSuites] Local base path not found: {base_path}")
        return suites

    max_depth = 8
    base_depth = base_path.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(base_path):
        if root.rstrip(os.sep).count(os.sep) - base_depth >= max_depth:
            dirs[:] = []
        for file_name in files:
            if not file_name.endswith('-tradefed'):
                continue
            full_path = os.path.join(root, file_name)
            if not ensure_tradefed_executable(full_path):
                continue
            suite = build_suite_info(full_path)
            if suite:
                suites.append(suite)

    suites.sort(key=lambda item: (item['test_type'], item['version'], item['full_path']))
    return suites
