from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from bidtriage import __version__
from bidtriage.web.routers import actions, admin, api, calendar, digests, health, opportunities

templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))


def create_app() -> FastAPI:
    app = FastAPI(
        title="bidtriage",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.include_router(health.router)
    app.include_router(opportunities.router)
    app.include_router(actions.router)
    app.include_router(calendar.router)
    app.include_router(digests.router)
    app.include_router(admin.router)
    app.include_router(api.router)
    return app


app = create_app()
