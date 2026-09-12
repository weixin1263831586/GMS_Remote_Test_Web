"""gms_agent — GMS Agent Runtime Python SDK.

Shared by the MCP server (protocol adapter) and callable directly; the CLI
can migrate to it incrementally without changing its public interface.
"""
from .client import GmsAgentSdk, GmsApiError, GmsClient


__all__ = ["GmsAgentSdk", "GmsApiError", "GmsClient"]
__version__ = "0.21.0"
