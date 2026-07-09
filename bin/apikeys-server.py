#!/usr/bin/env python3
"""
MCP API Keys Server — Secure access to free-api-hunter credentials.

Uses official MCP SDK (FastMCP) with stdio transport.
Provides read-only access to API keys and provider documentation.
"""

import json
import os
import time
import hashlib
from pathlib import Path
from typing import Optional, List, Dict, Any
from mcp.server.fastmcp import FastMCP

# --- Configuration ---
VAULT_ROOT = Path("/root/LabDoctorM/vault/free-api-hunter")
PROVIDERS_JSON = Path("/root/LabDoctorM/projects/free-api-hunter/data/providers.json")
SOURCES_JSON = Path("/root/LabDoctorM/projects/free-api-hunter/configs/sources.json")
MAX_FILE_SIZE = 10 * 1024  # 10KB max for key files

mcp = FastMCP(
    "apikeys-server",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8086")),
)


def _mask_key(key: str, show_head: int = 4, show_tail: int = 4) -> str:
    """Mask API key showing only first and last N characters."""
    if len(key) <= show_head + show_tail:
        return key[:show_head] + "..." + key[-show_tail:] if len(key) > show_head else key
    return key[:show_head] + "*" * (len(key) - show_head - show_tail) + key[-show_tail:]


def _read_key_file(provider: str) -> Optional[str]:
    """Read API key from vault directory."""
    key_file = VAULT_ROOT / provider.lower() / "api.key"
    if not key_file.exists():
        # Try alternative names
        for alt_name in ["api.keys", "api_key_primary.key", "api_key.key"]:
            alt_file = VAULT_ROOT / provider.lower() / alt_name
            if alt_file.exists():
                key_file = alt_file
                break

    if key_file.exists():
        try:
            return key_file.read_text().strip()
        except Exception:
            return None
    return None


def _load_providers() -> Dict[str, Any]:
    """Load providers.json data."""
    try:
        if PROVIDERS_JSON.exists():
            return json.loads(PROVIDERS_JSON.read_text())
    except Exception:
        pass
    return {"providers": [], "provider_pages": []}


# --- Tools ---

@mcp.tool()
def get_key(provider: str) -> Dict[str, Any]:
    """
    Get API key and metadata for a provider.

    Returns:
        api_key: The actual API key (be careful with this!)
        masked_key: Masked version for logging (first/last 4 chars)
        base_url: API endpoint
        auth_type: "bearer" or "query_param"
        models: List of available models
        use_cases: List of use cases (chat, embeddings, images, etc.)
        note: Special instructions or limitations
        account_id: Required for Cloudflare (account ID in URL)
        health_endpoint: Endpoint for health checks
        status: Provider status (verified, confirmed, rate_limited, blocked)

    Security:
        - Only read access, never writes keys
        - Keys are masked in logs
        - Provider must be in allowlist
    """
    ALLOWED_PROVIDERS = {
        "cerebras", "cloudflare", "gemini", "manus", "cohere", "mistral",
        "elevenlabs", "pollinations", "ocr-space"
    }

    provider_lower = provider.lower()
    if provider_lower not in ALLOWED_PROVIDERS:
        return {
            "error": f"Provider '{provider}' not in allowlist",
            "allowed": list(ALLOWED_PROVIDERS),
            "api_key": None,
            "masked_key": None
        }

    # Load provider info
    data = _load_providers()
    provider_info = None
    for p in data.get("providers", []):
        if p.get("name", "").lower() == provider_lower:
            provider_info = p
            break

    # Get key from vault
    api_key = _read_key_file(provider_lower)

    # Build response
    result = {
        "provider": provider,
        "api_key": api_key,
        "masked_key": _mask_key(api_key) if api_key else None,
        "status": provider_info.get("status", "unknown") if provider_info else "no_info",
    }

    if provider_info:
        result["base_url"] = provider_info.get("url")
        result["api_key_url"] = provider_info.get("api_key_url")
        result["auth_type"] = "bearer" if provider_lower not in ["gemini"] else "query_param"
        result["models"] = provider_info.get("models", [])
        result["limits"] = provider_info.get("limits", {})
        result["note"] = provider_info.get("notes", "")

        # Special cases
        if provider_lower == "cloudflare":
            result["account_id"] = "REQUIRED: Get from cf_accounts.json"
        if provider_lower == "gemini":
            result["auth_type"] = "query_param"

    # Provider-specific details
    if provider_lower == "pollinations":
        result["base_url"] = "https://gen.pollinations.ai/v1"
        result["models"] = [
            "openai", "openai-fast", "gpt-5.4-mini", "qwen-coder",
            "mistral-small-3.2", "mistral", "deepseek", "gemma",
            "grok", "grok-large", "kimi", "llama", "llama-scout",
            "mistral-large", "qwen-vision", "step-3.5-flash"
        ]
    elif provider_lower == "cerebras":
        result["base_url"] = "https://api.cerebras.ai/v1"
        result["models"] = ["gpt-oss-120b", "zai-glm-4.7"]
    elif provider_lower == "mistral":
        result["base_url"] = "https://api.mistral.ai/v1"
        result["models"] = ["mistral-medium-3.5", "mistral-small-4", "mistral-large-3"]
    elif provider_lower == "cohere":
        result["base_url"] = "https://api.cohere.com/v1"
        result["auth_type"] = "bearer"
    elif provider_lower == "elevenlabs":
        result["base_url"] = "https://api.elevenlabs.io/v1"
        result["auth_type"] = "bearer"
    elif provider_lower == "ocr-space":
        result["base_url"] = "https://api.ocr.space/1.0"
        result["auth_type"] = "query_param"
        result["models"] = ["Engine 1 (fast)", "Engine 2 (balanced)", "Engine 3 (accurate)"]

    return result


@mcp.tool()
def list_providers() -> Dict[str, Any]:
    """
    List all available providers with their status.

    Returns:
        providers: List of {name, status, models_count, note}
    """
    data = _load_providers()
    providers = []

    for p in data.get("providers", []):
        providers.append({
            "name": p.get("name"),
            "status": p.get("status", "unknown"),
            "models_count": len(p.get("models", [])) if p.get("models") else 0,
            "note": p.get("notes", "")[:100]
        })

    return {
        "total": len(providers),
        "providers": providers
    }


@mcp.tool()
def get_provider_docs(provider: str) -> Dict[str, Any]:
    """
    Get detailed documentation for a provider.

    Returns:
        provider: Provider name
        docs: Markdown documentation
        quickstart: Code example
        common_errors: List of common errors and solutions
    """
    provider_lower = provider.lower()
    data = _load_providers()

    # Find provider info
    provider_info = None
    for p in data.get("providers", []):
        if p.get("name", "").lower() == provider_lower:
            provider_info = p
            break

    docs = f"# {provider}\n\n"

    if provider_info:
        docs += f"## Status: {provider_info.get('status', 'unknown')}\n\n"
        docs += f"**URL:** {provider_info.get('url', 'N/A')}\n\n"
        if provider_info.get('models'):
            docs += f"**Models:**\n"
            for m in provider_info['models']:
                docs += f"- `{m}`\n"
            docs += "\n"
        if provider_info.get('limits'):
            docs += f"**Limits:**\n"
            for k, v in provider_info['limits'].items():
                docs += f"- {k}: {v}\n"
            docs += "\n"
        if provider_info.get('notes'):
            docs += f"**Notes:**\n{provider_info['notes']}\n\n"

    # Provider-specific docs
    if provider_lower == "cloudflare":
        docs += "## Setup\n"
        docs += "1. Get account ID from `cf_accounts.json`\n"
        docs += "2. Use endpoint: `/client/v4/accounts/{account_id}/ai/run/{model}`\n"
    elif provider_lower == "gemini":
        docs += "## Setup\n"
        docs += "1. Get API key from AI Studio\n"
        docs += "2. Use query param auth: `?key=YOUR_KEY`\n"
    elif provider_lower == "pollinations":
        docs += "## Setup\n"
        docs += "1. API key is optional for free tier\n"
        docs += "2. OpenAI-compatible API\n"
        docs += "3. Image generation: `/v1/images/generations`\n"

    quickstart = f"# Example usage for {provider}\n# TODO: Add specific example"
    common_errors = ["TODO: Add common errors"]

    return {
        "provider": provider,
        "docs": docs,
        "quickstart": quickstart,
        "common_errors": common_errors
    }


@mcp.tool()
def check_health(provider: str) -> Dict[str, Any]:
    """
    Check if a provider's API is accessible.

    Returns:
        provider: Provider name
        healthy: bool
        latency_ms: Response time
        error: Error message if unhealthy
    """
    import urllib.request
    import urllib.error

    provider_lower = provider.lower()

    # Provider endpoints
    endpoints = {
        "cerebras": "https://api.cerebras.ai/v1/models",
        "cloudflare": "https://api.cloudflare.com/client/v4/user",
        "gemini": "https://generativelanguage.googleapis.com/v1/models",
        "cohere": "https://api.cohere.com/v1/models",
        "mistral": "https://api.mistral.ai/v1/models",
        "elevenlabs": "https://api.elevenlabs.io/v1/speech-synthesis",
        "pollinations": "https://gen.pollinations.ai/v1/models",
        "ocr-space": "https://api.ocr.space/test"
    }

    endpoint = endpoints.get(provider_lower)
    if not endpoint:
        return {
            "provider": provider,
            "healthy": False,
            "latency_ms": 0,
            "error": f"No health check endpoint for {provider}"
        }

    try:
        start = time.time()
        req = urllib.request.Request(endpoint, method="GET")
        req.add_header("User-Agent", "MCP-apikeys-server/1.0")
        resp = urllib.request.urlopen(req, timeout=10)
        latency = (time.time() - start) * 1000
        return {
            "provider": provider,
            "healthy": True,
            "latency_ms": round(latency, 0),
            "status_code": resp.status
        }
    except Exception as e:
        return {
            "provider": provider,
            "healthy": False,
            "latency_ms": 0,
            "error": str(e)[:200]
        }


# --- Resources ---

@mcp.resource("providers://list")
def list_providers_resource() -> str:
    """List all providers as JSON string."""
    return json.dumps(list_providers(), indent=2)


@mcp.resource("provider://{name}")
def provider_resource(name: str) -> str:
    """Get provider info as JSON string."""
    return json.dumps(get_key(name), indent=2)


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transport == "http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
