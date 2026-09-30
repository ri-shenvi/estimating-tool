from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from bidtriage.web.deps import db

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
def readyz(response: Response, session: Session = Depends(db)) -> dict[str, str]:
    try:
        session.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        response.status_code = 503
        return {"status": "db_unreachable", "detail": str(e)[:200]}
    return {"status": "ready"}
