#!/usr/bin/env python3
"""
MCP API Keys Server — Secure access to free-api-hunter credentials.

Uses official MCP SDK (FastMCP) with stdio transport.

Wave 2 (KRV): this server is now a *facade* over the KRV Registry — a SQLite
database that is the single source of truth for live validation results,
instructions and static metadata. The MCP server no longer invents metadata;
it reflects the Registry (with providers.json only as a fallback when a
provider has no Registry row yet).

Security model (security fix):
  * API keys are returned MASKED by default. The real secret is only returned
    when the caller explicitly passes `unmask=True`.
  * `masked_key` is always present (safe for logs).
  * Every get_key call is written to an append-only audit log.
"""

import json
import os
import time
import sqlite3
import fcntl
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any
from mcp.server.fastmcp import FastMCP

# --- Configuration ---
# KRV Registry: single source of truth (SQLite).
REGISTRY_DB = Path(
    os.environ.get(
        "KRV_REGISTRY_DB",
        "/root/LabDoctorM/projects/free-api-hunter/data/free-api-hunter.db",
    )
)
AUDIT_LOG = Path(
    os.environ.get(
        "KRV_AUDIT_LOG",
        "/root/LabDoctorM/projects/free-api-hunter/data/apikeys_audit.log",
    )
)
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
    if not key:
        return None
    if len(key) <= show_head + show_tail:
        return key[:show_head] + "..." + key[-show_tail:] if len(key) > show_head else key
    return key[:show_head] + "*" * (len(key) - show_head - show_tail) + key[-show_tail:]


def _mask_key_id(key_id: str) -> str:
    """Mask a Registry key_id (provider/filename) so the vault filename is hidden."""
    if not key_id:
        return "***"
    parts = key_id.split("/")
    if len(parts) >= 2:
        provider = parts[0]
        fname = parts[-1]
        return provider + "/" + _mask_key(fname, show_head=2, show_tail=2)
    return _mask_key(key_id, show_head=2, show_tail=2)


def _read_key_file(provider: str) -> Optional[str]:
    """Read API key from vault directory. Returns (key_text, filename) or (None, None)."""
    key_file = VAULT_ROOT / provider.lower() / "api.key"
    fname = "api.key"
    if not key_file.exists():
        # Try alternative names
        for alt_name in ["api.keys", "api_key_primary.key", "api_key.key"]:
            alt_file = VAULT_ROOT / provider.lower() / alt_name
            if alt_file.exists():
                key_file = alt_file
                fname = alt_name
                break

    if key_file.exists():
        try:
            text = key_file.read_text().strip()
            if len(text.encode()) > MAX_FILE_SIZE:
                return None, None
            return text, fname
        except Exception:
            return None, None
    return None, None


def _load_providers() -> Dict[str, Any]:
    """Load providers.json data."""
    try:
        if PROVIDERS_JSON.exists():
            return json.loads(PROVIDERS_JSON.read_text())
    except Exception:
        pass
    return {"providers": [], "provider_pages": []}


def _provider_in_providers_json(provider_lower: str) -> Optional[Dict[str, Any]]:
    """Return a provider entry from providers.json (matched case-insensitively) or None."""
    data = _load_providers()
    for p in data.get("providers", []):
        if p.get("name", "").lower() == provider_lower:
            return p
    return None


def _registry_lookup(provider_lower: str) -> Optional[Dict[str, Any]]:
    """
    Look up a provider in the KRV Registry (SQLite "keys" table).

    Returns the best row as a dict, or None if the provider is absent.
    Among multiple rows for the same provider, prefer live_status='valid',
    otherwise the first row (ORDER BY rowid).
    """
    if not REGISTRY_DB.exists():
        return None
    try:
        conn = sqlite3.connect(str(REGISTRY_DB))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT provider, key_id, vault_path, registry_status, live_status,
                   last_validated, models, auth_type, base_url, instructions,
                   added_by, added_at
            FROM "keys"
            WHERE provider = ? OR key_id LIKE ? || '/%'
            ORDER BY CASE WHEN live_status = 'valid' THEN 0 ELSE 1 END, rowid
            LIMIT 1
            """,
            (provider_lower, provider_lower),
        )
        row = cur.fetchone()
        conn.close()
        if row is None:
            return None
        return dict(row)
    except Exception:
        # Registry must never break key access; fall back to providers.json.
        return None


def _write_audit(provider: str, key_id_masked: str, caller: str = "unknown") -> None:
    """Append an audit line: ISO-timestamp | provider | key_id(masked) | caller."""
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        line = f"{ts} | {provider} | {key_id_masked} | {caller}\n"
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception:
        # Audit must never break key access.
        pass


# Allowlist of providers that are safe to expose even without a Registry row.
ALLOWED_PROVIDERS = {
    "cerebras", "cloudflare", "gemini", "manus", "cohere", "mistral",
    "elevenlabs", "pollinations", "ocr-space",
}


# --- Tools ---

@mcp.tool()
def get_key(provider: str, unmask: bool = False) -> Dict[str, Any]:
    """
    Get API key and metadata for a provider.

    This is a facade over the KRV Registry (SQLite). The Registry is the single
    source of truth for live_status, instructions and static metadata.

    Args:
        provider: Provider name (case-insensitive).
        unmask: If True, return the REAL api_key. By default api_key is masked
                (security fix — keys were previously returned in clear text).

    Returns:
        provider: Provider name as requested.
        api_key: MASKED by default; real secret only when unmask=True.
        masked_key: Always the masked secret (safe for logs).
        live_status: Real validation result from the Registry
                     (valid|expired|rate_limited|unknown).
        status: "verified" if live_status=='valid', else registry_status.
        registry_status: Static status from the Registry / providers.json.
        last_validated: ISO timestamp of last live validation (Registry).
        models: Model list (Registry JSON, fallback providers.json).
        auth_type: "bearer" | "query_param" (Registry, fallback providers.json).
        base_url: API endpoint (Registry, fallback providers.json).
        instructions: Human-readable instructions from the Registry.
        key_id: Registry key id (provider/filename) when available.

    Security:
        - Read-only; never writes keys.
        - Keys are masked by default; real key only on explicit unmask=True.
        - Provider must be in the allowlist OR present in the Registry OR in
          providers.json (no arbitrary provider input).
        - Every call is appended to an audit log.
    """
    provider_lower = provider.lower()

    # --- Security gate: allowlist OR curated (Registry) OR providers.json ---
    reg = _registry_lookup(provider_lower)
    pj = _provider_in_providers_json(provider_lower)
    allowed = (
        provider_lower in ALLOWED_PROVIDERS
        or reg is not None
        or pj is not None
    )
    if not allowed:
        _write_audit(provider, _mask_key_id(f"{provider_lower}/***"))
        return {
            "error": f"Provider '{provider}' not in allowlist and not present in Registry or providers.json",
            "allowed_examples": list(ALLOWED_PROVIDERS),
            "api_key": None,
            "masked_key": None,
        }

    # --- Read the real key from vault (for masking / unmasking) ---
    real_key, key_fname = _read_key_file(provider_lower)

    # --- Source metadata: Registry first, providers.json as fallback ---
    if reg is not None:
        registry_status = reg.get("registry_status") or (pj or {}).get("status", "unknown")
        live_status = reg.get("live_status") or "unknown"
        last_validated = reg.get("last_validated")
        try:
            reg_models = json.loads(reg.get("models") or "[]")
        except Exception:
            reg_models = []
        if not isinstance(reg_models, list):
            reg_models = []
        auth_type = reg.get("auth_type") or "unknown"
        base_url = reg.get("base_url") or ""
        instructions = reg.get("instructions") or ""
        key_id = reg.get("key_id")
    else:
        registry_status = (pj or {}).get("status", "unknown")
        live_status = "unknown"
        last_validated = None
        reg_models = []
        auth_type = "unknown"
        base_url = ""
        instructions = ""
        key_id = None

    # Fallbacks into providers.json when Registry row is incomplete.
    pj_models = (pj or {}).get("models") or []
    if isinstance(pj_models, list):
        models = reg_models if reg_models else pj_models
    else:
        models = reg_models

    if not base_url and pj:
        base_url = pj.get("url", "")

    if auth_type in ("", "unknown"):
        # providers.json has no auth_type field; derive a sane default.
        if provider_lower in ("gemini", "ocr-space"):
            auth_type = "query_param"
        else:
            auth_type = "bearer"

    # status: live_status valid -> verified, else registry_status.
    status = "verified" if live_status == "valid" else registry_status

    # --- Audit log ---
    audit_key_id = key_id or f"{provider_lower}/{key_fname or '***'}"
    _write_audit(provider, _mask_key_id(audit_key_id))

    masked_key = _mask_key(real_key) if real_key else None
    api_key = real_key if unmask else masked_key

    return {
        "provider": provider,
        "api_key": api_key,
        "masked_key": masked_key,
        "key_id": key_id,
        "live_status": live_status,
        "status": status,
        "registry_status": registry_status,
        "last_validated": last_validated,
        "models": models,
        "auth_type": auth_type,
        "base_url": base_url,
        "instructions": instructions,
    }


@mcp.tool()
def list_providers(with_live: bool = True) -> Dict[str, Any]:
    """
    List all available providers with their status.

    Args:
        with_live: If True, enrich each provider with live_status from the
                   KRV Registry when a row exists (minimal Registry integration).

    Returns:
        providers: List of {name, status, live_status, models_count, note}
    """
    data = _load_providers()
    providers = []

    for p in data.get("providers", []):
        name = p.get("name")
        name_l = (name or "").lower()
        entry = {
            "name": name,
            "status": p.get("status", "unknown"),
            "models_count": len(p.get("models", [])) if p.get("models") else 0,
            "note": (p.get("notes", "") or "")[:100],
        }
        if with_live:
            reg = _registry_lookup(name_l)
            if reg is not None:
                entry["live_status"] = reg.get("live_status") or "unknown"
                entry["status"] = (
                    "verified" if (reg.get("live_status") or "") == "valid"
                    else reg.get("registry_status") or p.get("status", "unknown")
                )
        providers.append(entry)

    return {
        "total": len(providers),
        "providers": providers,
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

    # Registry enrichment (instructions are now sourced from the Registry).
    reg = _registry_lookup(provider_lower)
    registry_instructions = (reg or {}).get("instructions", "") if reg else ""

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

    if registry_instructions:
        docs += f"## Registry Instructions\n{registry_instructions}\n\n"

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
        "common_errors": common_errors,
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

    # Prefer the Registry base_url; fall back to static map.
    reg = _registry_lookup(provider_lower)
    if reg and reg.get("base_url"):
        endpoint = str(reg["base_url"]).rstrip("/") + "/models"
    else:
        endpoints = {
            "cerebras": "https://api.cerebras.ai/v1/models",
            "cloudflare": "https://api.cloudflare.com/client/v4/user",
            "gemini": "https://generativelanguage.googleapis.com/v1/models",
            "cohere": "https://api.cohere.com/v1/models",
            "mistral": "https://api.mistral.ai/v1/models",
            "elevenlabs": "https://api.elevenlabs.io/v1/speech-synthesis",
            "pollinations": "https://gen.pollinations.ai/v1/models",
            "ocr-space": "https://api.ocr.space/test",
        }
        endpoint = endpoints.get(provider_lower)
        if not endpoint:
            # Fall back to providers.json url if present.
            pj = _provider_in_providers_json(provider_lower)
            if pj and pj.get("url"):
                endpoint = str(pj["url"]).rstrip("/") + "/models"

    if not endpoint:
        return {
            "provider": provider,
            "healthy": False,
            "latency_ms": 0,
            "error": f"No health check endpoint for {provider}",
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
            "status_code": resp.status,
        }
    except Exception as e:
        return {
            "provider": provider,
            "healthy": False,
            "latency_ms": 0,
            "error": str(e)[:200],
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
