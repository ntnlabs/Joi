"""Admin endpoints for Joi API."""

import logging
import os
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("joi.api.admin")

# Create router - will be included in main app
router = APIRouter(prefix="/admin", tags=["admin"])

# Dependencies (set via set_dependencies after import)
_memory = None
_policy_manager = None
_config_push_client = None
_hmac_rotator = None


def set_dependencies(memory, policy_manager, config_push_client, hmac_rotator):
    """Set dependencies after import to avoid circular imports."""
    global _memory, _policy_manager, _config_push_client, _hmac_rotator
    _memory = memory
    _policy_manager = policy_manager
    _config_push_client = config_push_client
    _hmac_rotator = hmac_rotator


def _is_local_request(request: Request) -> bool:
    """Check if request is from the local machine.

    Accepts 127.0.0.1 and, when the API binds to a specific IP (e.g. Nebula
    address 10.42.0.10), also accepts that IP — a connection from the bind
    address to itself is local.

    Note: Previously trusted all 10.x.x.x (Nebula network), but that's too
    permissive - any host on the network could access admin endpoints.
    For remote admin access, use HMAC-authenticated endpoints instead.

    IMPORTANT: This assumes no reverse proxy in front of Joi. If a reverse proxy
    is added, client.host will be the proxy's IP (127.0.0.1), bypassing this check.
    In that case, X-Forwarded-For handling would be needed (with proxy stripping/overwriting).
    """
    client_ip = request.client.host if request.client else ""
    if client_ip == "127.0.0.1":
        return True
    bind_host = os.getenv("JOI_BIND_HOST", "")
    return bool(bind_host and client_ip == bind_host)


@router.get("/config/status")
def admin_config_status(request: Request):
    """Get config sync status."""
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are local-only")

    if not _config_push_client:
        return {"status": "error", "error": "config_push_not_enabled"}

    return {
        "status": "ok",
        "data": _config_push_client.get_status(),
    }


@router.post("/config/push")
def admin_config_push(request: Request):
    """Force push current config to mesh."""
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are local-only")

    if not _config_push_client:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": "config_push_not_enabled"},
        )

    success, result = _config_push_client.push_config(force=True)
    if success:
        return {"status": "ok", "data": {"mesh_config_hash": result}}

    return JSONResponse(
        status_code=500,
        content={"status": "error", "error": result},
    )


@router.post("/hmac/rotate")
def admin_hmac_rotate(request: Request):
    """
    Manually trigger HMAC key rotation.

    Query params:
        grace: Set to "false" for immediate rotation (no grace period). Default: true.
    """
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are local-only")

    if not _hmac_rotator:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": "hmac_rotation_not_enabled"},
        )

    # Check for grace period option
    use_grace = request.query_params.get("grace", "true").lower() != "false"

    success, result = _hmac_rotator.rotate(use_grace_period=use_grace)
    if success:
        return {
            "status": "ok",
            "message": "HMAC rotation complete",
            "grace_period": use_grace,
        }

    return JSONResponse(
        status_code=500,
        content={"status": "error", "error": result},
    )


@router.get("/hmac/status")
def admin_hmac_status(request: Request):
    """Get HMAC rotation status."""
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are local-only")

    if not _hmac_rotator:
        return {"status": "error", "error": "hmac_rotation_not_enabled"}

    last_rotation = _hmac_rotator.get_last_rotation_time()
    return {
        "status": "ok",
        "data": {
            "last_rotation_time": last_rotation,
            "last_rotation_ago_hours": (time.time() - last_rotation) / 3600 if last_rotation else None,
            "rotation_due": _hmac_rotator.should_rotate(),
        },
    }


@router.get("/security/status")
def admin_security_status(request: Request):
    """Get security settings status."""
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are local-only")

    return {
        "status": "ok",
        "data": _policy_manager.get_security(),
    }


@router.post("/security/privacy-mode")
def admin_set_privacy_mode(request: Request):
    """
    Enable or disable privacy mode.

    Query params:
        enabled: "true" or "false"
    """
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are local-only")

    enabled = request.query_params.get("enabled", "").lower() == "true"
    _policy_manager.set_privacy_mode(enabled)

    # Push to mesh
    if _config_push_client:
        success, result = _config_push_client.push_config(force=True)
        if not success:
            logger.warning("Failed to push privacy mode change to mesh", extra={"error": result})

    return {
        "status": "ok",
        "privacy_mode": enabled,
    }


@router.post("/security/kill-switch")
def admin_set_kill_switch(request: Request):
    """
    Activate or deactivate kill switch.

    When active, mesh will not forward messages to Joi.
    Use in emergencies to immediately stop message processing.

    Query params:
        active: "true" or "false"
    """
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are local-only")

    active = request.query_params.get("active", "").lower() == "true"
    _policy_manager.set_kill_switch(active)

    # Push to mesh immediately
    if _config_push_client:
        success, result = _config_push_client.push_config(force=True)
        if success:
            logger.info("Kill switch pushed to mesh", extra={"active": active, "action": "kill_switch"})
        else:
            logger.error("CRITICAL: Failed to push kill switch to mesh", extra={"error": result, "active": active})
            return JSONResponse(
                status_code=500,
                content={"status": "error", "error": f"push_failed: {result}", "kill_switch": active},
            )

    return {
        "status": "ok",
        "kill_switch": active,
    }


@router.get("/fts/status")
def admin_fts_status(request: Request):
    """
    Get FTS (Full-Text Search) index integrity status.

    Returns counts for each FTS table and whether they're in sync with main tables.
    """
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are local-only")

    integrity = _memory.check_fts_integrity()
    all_ok = all(status.get("ok", False) for status in integrity.values())

    return {
        "status": "ok" if all_ok else "degraded",
        "indexes": integrity,
    }


@router.post("/fts/rebuild")
def admin_fts_rebuild(request: Request):
    """
    Rebuild FTS indexes.

    Query params:
        index: Specific index to rebuild (user_facts_fts, summaries_fts, knowledge_fts)
               If not specified, rebuilds all indexes.
    """
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are local-only")

    index_name = request.query_params.get("index")

    if index_name:
        # Rebuild specific index
        success, message = _memory.rebuild_fts_index(index_name)
        if not success:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "error": message},
            )
        return {"status": "ok", "message": message}
    else:
        # Rebuild all
        results = _memory.rebuild_all_fts_indexes()
        all_ok = all(r["success"] for r in results.values())
        return {
            "status": "ok" if all_ok else "partial",
            "results": results,
        }


@router.get("/routing/status")
def admin_routing_status(request: Request):
    """Get routing configuration status."""
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are local-only")

    routing = _policy_manager.get_routing()
    return {
        "status": "ok",
        "data": {
            "enabled": routing.get("enabled", False),
            "default_backend": routing.get("default_backend", "joi"),
            "backends": list(routing.get("backends", {}).keys()),
            "rules_count": len(routing.get("rules", []))
        }
    }


@router.post("/routing/toggle")
def admin_routing_toggle(request: Request):
    """
    Toggle routing enabled/disabled.

    Query params:
        enabled: "true" or "false"
    """
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are local-only")

    enabled = request.query_params.get("enabled", "").lower() == "true"
    _policy_manager.set_routing_enabled(enabled)

    # Push config to mesh
    if _config_push_client:
        success, result = _config_push_client.push_config(force=True)
        if not success:
            logger.warning("Failed to push routing change to mesh", extra={"error": result})

    return {
        "status": "ok",
        "data": {"routing_enabled": enabled}
    }


@router.get("/rag/scopes")
def admin_rag_scopes(request: Request):
    """List all RAG scopes and their chunk counts (debug endpoint)."""
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are local-only")

    sources = _memory.get_knowledge_sources()
    # Group by scope
    scopes = {}
    for s in sources:
        scope = s["scope"]
        if scope not in scopes:
            scopes[scope] = {"sources": 0, "chunks": 0}
        scopes[scope]["sources"] += 1
        scopes[scope]["chunks"] += s["chunk_count"]

    return {
        "status": "ok",
        "scopes": scopes,
        "total_sources": len(sources),
        "total_chunks": sum(s["chunks"] for s in scopes.values()),
    }


@router.get("/rag/search")
def admin_rag_search(request: Request, q: str, scope: Optional[str] = None):
    """Test RAG search with optional scope filter (debug endpoint)."""
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are local-only")

    scopes = [scope] if scope else None
    chunks = _memory.search_knowledge(q, limit=10, scopes=scopes)

    return {
        "status": "ok",
        "query": q,
        "scope_filter": scope,
        "results": [
            {
                "source": c["source"],
                "title": c["title"],
                "scope": c["scope"],
                "content_preview": c["content"][:200] if c["content"] else "",
            }
            for c in chunks
        ],
    }
