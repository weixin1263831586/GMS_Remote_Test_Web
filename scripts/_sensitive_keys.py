"""Shared sensitive-key lists for release packaging and source secret scans.

``SENSITIVE_KEYS``/``SENSITIVE_SUFFIXES`` 是发布脱敏
（sanitize_release_config.py）、发布树校验（verify_release_tree.py）与
源码密钥扫描（check_source_secrets.py）共用的唯一清单；新增敏感键只改
这里，避免多处复制后漂移。
"""

SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "encrypted_password",
    "password",
    "private_key",
    "rest_password",
    "secret",
    "token",
    "ubuntu_pswd",
    "vnc_password",
}

SENSITIVE_SUFFIXES = ("_api_key", "_password", "_pswd", "_secret", "_token")
