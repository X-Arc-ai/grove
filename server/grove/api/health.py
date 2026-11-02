"""GET /healthz: the liveness probe of the box. No sign-in, no world, no data.

D13's health views are /v1/health/person and /v1/health/operator (B.3); this is not one of them.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from grove.store.probe import store_reachable

router = APIRouter()


@router.get("/healthz")
def healthz(request: Request) -> JSONResponse:
    store_ok = store_reachable(request.app.state.driver)
    body = {
        "status": "ok" if store_ok else "degraded",
        "store": "ok" if store_ok else "unreachable",
        "build": request.app.state.settings.build_sha,
    }
    return JSONResponse(
        body, status_code=200 if store_ok else 503, headers={"Cache-Control": "no-store"}
    )
