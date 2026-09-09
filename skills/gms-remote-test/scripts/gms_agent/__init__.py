"""gms_agent — GMS Agent Runtime Python SDK (10.txt §五, Phase 2).

Shared by the MCP server (protocol adapter) and callable directly; the CLI
can migrate to it incrementally without changing its public interface.
"""
from .client import GmsAgentSdk, GmsApiError, GmsClient


__all__ = ["GmsAgentSdk", "GmsApiError", "GmsClient"]
__version__ = "0.13.0"
