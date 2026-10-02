"""FastAPI 入口：路由挂载 + 静态页面 + 启动初始化。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth, routes, tenants, workspace_jobs
from app.config import APP_ENV, BASE_DIR, DATABASE_URL, UPLOAD_DIR
from app.db import initialize_development_schema
from app.services.job_manager import get_job_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    if APP_ENV != "development" and DATABASE_URL.startswith("sqlite"):
        raise RuntimeError("非开发环境必须配置 PostgreSQL DATABASE_URL")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if APP_ENV == "development":
        initialize_development_schema()
    manager = get_job_manager()
    for job_id in manager.pending_job_ids():
        asyncio.create_task(asyncio.to_thread(manager.process, job_id))
    yield


app = FastAPI(title="DocPilot", lifespan=lifespan)
app.include_router(routes.router)
app.include_router(auth.router)
app.include_router(tenants.router)
app.include_router(workspace_jobs.router)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_FRONTEND_DIST = BASE_DIR / "frontend" / "dist"

if (_FRONTEND_DIST / "assets").is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=_FRONTEND_DIST / "assets"),
        name="frontend-assets",
    )


@app.get("/", include_in_schema=False)
async def index():
    dist_index = _FRONTEND_DIST / "index.html"
    if dist_index.is_file():
        return FileResponse(dist_index, media_type="text/html")
    if APP_ENV == "production":
        raise HTTPException(status_code=503, detail="前端尚未构建")
    return FileResponse(_STATIC_DIR / "index.html", media_type="text/html")


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
