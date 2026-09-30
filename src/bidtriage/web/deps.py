"""Request dependencies: DB session and current user.

Authentication: production uses Entra ID OIDC (SPEC-09 F1). This scaffold ships a dev-mode
identity resolved from the `X-Dev-User` header or the first active user, and refuses to run
that path when BIDTRIAGE_ENV is not `dev`. Wire the OIDC middleware before any non-dev deploy.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from bidtriage.core.config import Settings, get_settings
from bidtriage.core.db import get_session_factory
from bidtriage.core.models import User


def db() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@dataclass
class CurrentUser:
    id: str
    name: str
    email: str
    role: str


def current_user(
    request: Request,
    session: Session = Depends(db),
    settings: Settings = Depends(get_settings),
    x_dev_user: str | None = Header(default=None),
) -> CurrentUser:
    if not settings.is_dev:
        # OIDC session cookie handling goes here; until wired, refuse rather than pretend.
        raise HTTPException(status_code=401, detail="authentication not configured")
    stmt = select(User).where(User.active.is_(True))
    if x_dev_user:
        stmt = stmt.where(User.email == x_dev_user)
    user = session.scalars(stmt.order_by(User.email)).first()
    if user is None:
        return CurrentUser(id="dev", name="Dev User", email="dev@example.com", role="admin")
    return CurrentUser(id=user.id, name=user.name, email=user.email, role=user.role)


def require_role(*roles: str):
    def _check(user: CurrentUser = Depends(current_user)) -> CurrentUser:
        if user.role not in roles:
            raise HTTPException(
                status_code=403, detail=f"requires role in {roles}; ask the chief estimator"
            )
        return user

    return _check
