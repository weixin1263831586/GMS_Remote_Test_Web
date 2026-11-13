#!/usr/bin/env python3
"""一次性迁移：宿主 ~/.kkagent/config.toml → configs/local/config.json ``ai_models``。

模型目录统一后（ADR 0034），kkagent 临时配置由 ``ai_models`` 物化生成；
本脚本把宿主 kkagent 配置里的 ``[models]``/``[providers]`` 转换为
``ai_models`` 片段，供运维并入 ``configs/local/config.json``：

- 输出只写 stdout JSON（api_key 建议转 ``api_key_env``，脚本不打印明文）；
- ``--dry-run`` 只打印将生成的片段（默认行为），``--write`` 直接并入
  现有 config.json 的 ``ai_models``（保留既有 provider/设置）；
- 永不修改 ``~/.kkagent/config.toml``。

用法（tools/scripts 脚本经 _common.py 解析仓库根）::

    python3 tools/scripts/kkagent/import_kkagent_models.py            # dry-run
    python3 tools/scripts/kkagent/import_kkagent_models.py --write
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import find_repo_root


_REPO_ROOT = find_repo_root()

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib


def _host_config() -> Path:
    return Path(
        os.getenv("GMS_ASSISTANT_KKAGENT_CONFIG", "").strip()
        or Path.home() / ".kkagent" / "config.toml"
    ).expanduser()


def convert(config: dict) -> dict:
    """宿主 kkagent 配置 → ai_models 片段。

    provider 的 ``api_key`` 明文转 ``api_key_env``（``GMS_KKAGENT_KEY_<NAME>``），
    避免把宿主密钥复制进 config.json。
    """
    providers = config.get("providers") or {}
    models = config.get("models") or {}
    default_model = str(config.get("default_model") or "")

    api_format_by_provider: dict[str, str] = {}
    for name, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        ptype = str(provider.get("type") or "").lower()
        api_format_by_provider[name] = ptype if ptype in {"openai", "anthropic"} else "openai"

    primary_provider = ""
    ai_providers: dict[str, dict] = {}
    for alias, model in models.items():
        if not isinstance(model, dict):
            continue
        provider_name = str(model.get("provider") or "")
        provider_cfg = providers.get(provider_name) if isinstance(providers.get(provider_name), dict) else {}
        base_url = str(provider_cfg.get("base_url") or "")
        if not base_url:
            continue
        entry: dict = {
            "name": str(model.get("display_name") or alias),
            "enabled": True,
            "base_url": base_url,
            "model": str(model.get("model") or alias),
            "api_format": api_format_by_provider.get(provider_name, "openai"),
        }
        api_key = str(provider_cfg.get("api_key") or "")
        if provider_cfg.get("api_key_env"):
            entry["api_key_env"] = str(provider_cfg["api_key_env"])
        elif api_key:
            entry["api_key_env"] = f"GMS_KKAGENT_KEY_{provider_name.upper().replace('-', '_')}"
        if model.get("max_context_size"):
            entry["max_context_size"] = int(model["max_context_size"])
        if model.get("max_output_size"):
            entry["max_tokens"] = int(model["max_output_size"])
        if model.get("capabilities"):
            entry["capabilities"] = list(model["capabilities"])
        if model.get("default_effort"):
            entry["default_effort"] = str(model["default_effort"])
        # 同一 provider 多个 alias：首个 alias 决定该 provider 的 model 与
        # primary；其余 alias 作为独立 provider（名字加后缀）以便逐一选择。
        if provider_name in ai_providers:
            provider_name = f"{provider_name}_{alias.replace('/', '_').replace('.', '_')}"
        ai_providers.setdefault(provider_name, entry)
        if alias == default_model and not primary_provider:
            primary_provider = provider_name

    return {
        "enabled": bool(ai_providers),
        "primary_provider": primary_provider or (next(iter(ai_providers), "")),
        "providers": ai_providers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="把片段并入 configs/local/config.json（默认 dry-run）")
    args = parser.parse_args()

    find_repo_root()
    host = _host_config()
    if not host.is_file():
        print(json.dumps({"error": f"宿主 kkagent 配置不存在: {host}"}))
        return 1
    config = tomllib.loads(host.read_text(encoding="utf-8"))
    fragment = convert(config)

    if not args.write:
        print(json.dumps({
            "mode": "dry-run",
            "source": str(host),
            "note": "api_key 已转 api_key_env（GMS_KKAGENT_KEY_<PROVIDER>），请为对应变量导出原密钥",
            "ai_models_fragment": fragment,
        }, ensure_ascii=False, indent=2))
        return 0

    target = _REPO_ROOT / "configs" / "local" / "config.json"
    if target.is_file():
        existing = json.loads(target.read_text(encoding="utf-8"))
    else:
        existing = {}
    merged = dict(existing.get("ai_models") or {})
    merged_providers = dict(merged.get("providers") or {})
    for name, provider in fragment["providers"].items():
        merged_providers.setdefault(name, provider)
    merged["providers"] = merged_providers
    merged.setdefault("enabled", fragment["enabled"])
    merged.setdefault("primary_provider", fragment["primary_provider"])
    existing["ai_models"] = merged
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"mode": "write", "target": str(target),
                      "providers_added": sorted(fragment["providers"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
