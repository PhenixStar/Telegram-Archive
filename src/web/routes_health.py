"""Health check endpoints for liveness, readiness, and diagnostics."""

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import dependencies as deps

router = APIRouter()
_start_time = time.monotonic()


@router.get("/api/health")
async def health_liveness():
    """Liveness probe — always returns 200 if the app is running."""
    return {"status": "ok"}


@router.get("/api/health/ready")
async def health_readiness():
    """Readiness probe — checks DB connectivity, returns 503 if degraded."""
    db_ok = False
    if deps.db:
        try:
            db_ok = await deps.db.db_manager.health_check()
        except Exception:
            pass
    if not db_ok:
        return JSONResponse({"status": "degraded", "db": False}, status_code=503)
    return {"status": "ok", "db": True}


@router.get("/api/health/detailed")
async def health_detailed(request: Request):
    """Full diagnostic — DB, workers, FTS, listener, connections, uptime."""
    db_ok = False
    if deps.db:
        try:
            db_ok = await deps.db.db_manager.health_check()
        except Exception:
            pass

    # Worker status from app.state
    workers = {}
    for name in ("ocr_worker", "embedding_worker", "transcription_worker", "translation_worker"):
        w = getattr(request.app.state, name, None)
        workers[name.replace("_worker", "")] = {
            "running": getattr(w, "_running", False) if w else False,
            "enabled": w is not None,
        }

    # FTS status
    fts = "unknown"
    if deps.db:
        try:
            fts = await deps.db.get_fts_status()
        except Exception:
            pass

    # Listener status
    listener = "unknown"
    try:
        if deps.listener_mgr:
            listener = deps.listener_mgr.status
    except Exception:
        pass

    # Active WebSocket connections
    connections = len(deps.manager.active_connections) if deps.manager else 0
    status = "ok" if db_ok else "degraded"

    return {
        "status": status,
        "db": db_ok,
        "workers": workers,
        "fts": fts,
        "listener": listener,
        "connections": connections,
        "uptime_seconds": round(time.monotonic() - _start_time, 1),
    }
