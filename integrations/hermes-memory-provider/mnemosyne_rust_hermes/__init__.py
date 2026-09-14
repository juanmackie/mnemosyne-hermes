from mnemosyne_rust_hermes.config import default_config, ProviderConfig
from mnemosyne_rust_hermes.provider import MnemosyneRustProvider, CheckpointError, PROVIDER_ID
from mnemosyne_rust_hermes.mcp_client import StdioJsonRpcClient, McpDisconnected
from mnemosyne_rust_hermes.contexts import SKIP_CONTEXTS

__all__ = [
    "default_config",
    "ProviderConfig",
    "MnemosyneRustProvider",
    "CheckpointError",
    "PROVIDER_ID",
    "StdioJsonRpcClient",
    "McpDisconnected",
    "SKIP_CONTEXTS",
]


def register(ctx):
    provider = MnemosyneRustProvider()
    ctx.register_memory_provider(provider)
    return provider
