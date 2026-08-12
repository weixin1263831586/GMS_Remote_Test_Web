import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.users import config_api


class AiConfigApiTests(unittest.TestCase):
    @staticmethod
    def _config():
        return {
            "enabled": True,
            "primary_provider": "remote",
            "providers": {
                "glm_local": {
                    "name": "Local GLM",
                    "enabled": True,
                    "api_key": "",
                    "model": "glm-local",
                    "base_url": "http://172.16.1.20:3000",
                    "api_format": "openai",
                },
                "remote": {
                    "name": "Remote",
                    "enabled": True,
                    "api_key": "secret-token",
                    "model": "remote-model",
                    "base_url": "https://example.invalid",
                    "api_format": "anthropic",
                },
            },
        }

    def test_config_status_does_not_claim_enabled_provider_is_online(self):
        with (
            patch.object(
                config_api,
                "config_manager",
                SimpleNamespace(get_ai_config=lambda: self._config()),
            ),
            patch.dict("os.environ", {"GMS_LOCAL_AI_API_KEY": ""}, clear=False),
        ):
            response = asyncio.run(
                config_api.get_ai_config(SimpleNamespace(), probe=False, provider="")
            )

        payload = json.loads(response.body)
        status = payload["data"]["status"]
        providers = {item["provider"]: item for item in status["providers"]}
        self.assertEqual(status["local_provider"], "glm_local")
        self.assertEqual(providers["glm_local"]["state"], "credential_missing")
        self.assertIsNone(providers["glm_local"]["available"])
        self.assertNotEqual(
            payload["data"]["providers"]["remote"]["api_key"], "secret-token"
        )

    def test_explicit_probe_updates_only_selected_provider_status(self):
        probed = {
            "provider": "glm_local",
            "name": "Local GLM",
            "model": "glm-local",
            "enabled": True,
            "local": True,
            "configured": True,
            "credential_configured": True,
            "state": "available",
            "available": True,
            "checked": True,
            "latency_ms": 42,
        }
        with (
            patch.object(
                config_api,
                "config_manager",
                SimpleNamespace(get_ai_config=lambda: self._config()),
            ),
            patch(
                "features.assistant.universal_ai.UniversalAIAnalyzer.probe_provider",
                return_value=probed,
            ) as probe,
        ):
            response = asyncio.run(
                config_api.get_ai_config(
                    SimpleNamespace(), probe=True, provider="glm_local"
                )
            )

        payload = json.loads(response.body)
        providers = {
            item["provider"]: item
            for item in payload["data"]["status"]["providers"]
        }
        self.assertTrue(providers["glm_local"]["available"])
        self.assertIsNone(providers["remote"]["available"])
        probe.assert_called_once_with("glm_local")


if __name__ == "__main__":
    unittest.main()
