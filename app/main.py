"""FastAPI 入口：路由挂载 + 静态页面 + 启动初始化。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth, members, tenants, workspace_jobs
from app.config import APP_ENV, BASE_DIR, DATABASE_URL, QUEUE_ENABLED, UPLOAD_DIR
from app.contexts.automation import api as automation_api
from app.contexts.documents import api as documents_api
from app.contexts.operations import api as operations_api
from app.contexts.processing import service as processing
from app.contexts.quota import api as quota_api
from app.contexts.review import api as review_api
from app.contexts.search import api as search_api
from app.contexts.templates import api as templates_api
from app.contexts.webhooks import api as webhooks_api
from app.db import SessionLocal, initialize_development_schema
from app.errors import AccessError
from app.request_context import RequestIdMiddleware, configure_logging
from app.services.job_manager import get_job_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    if APP_ENV != "development" and DATABASE_URL.startswith("sqlite"):
        raise RuntimeError("非开发环境必须配置 PostgreSQL DATABASE_URL")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if APP_ENV == "development":
        initialize_development_schema()
    manager = get_job_manager()
    with SessionLocal() as db:
        processing.reclaim_timeouts(db)
    if not QUEUE_ENABLED:
        # 队列模式下重启待办由派发器与 Worker 接管，API 不再进程内执行。
        for job_id in manager.pending_job_ids():
            asyncio.create_task(asyncio.to_thread(manager.process, job_id))
    yield


app = FastAPI(title="DocPilot", lifespan=lifespan)
# 关联 ID 中间件先于路由挂载（NFR-005）：它在每个请求上建立上下文并在响应头回填，
# 下游的审计写入与日志过滤器都从该上下文取 ID。
app.add_middleware(RequestIdMiddleware)
app.include_router(auth.router)
app.include_router(tenants.router)
app.include_router(workspace_jobs.router)
app.include_router(members.router)
app.include_router(members.invitations_router)
app.include_router(templates_api.router)
app.include_router(review_api.router)
app.include_router(documents_api.router)
app.include_router(operations_api.system_router)
app.include_router(operations_api.router)
app.include_router(operations_api.platform_router)
app.include_router(search_api.router)
app.include_router(automation_api.router)
app.include_router(webhooks_api.router)
app.include_router(quota_api.router)


@app.exception_handler(AccessError)
async def access_error_handler(_request: Request, exc: AccessError) -> JSONResponse:
    """服务层的业务拒绝统一翻译成 HTTP 响应，保持状态码语义单一来源。"""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

_FRONTEND_DIST = BASE_DIR / "frontend" / "dist"

if (_FRONTEND_DIST / "assets").is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=_FRONTEND_DIST / "assets"),
        name="frontend-assets",
    )


@app.get("/", include_in_schema=False)
async def index():
    """只服务已构建的前端。

    这里原来还有一层"开发环境退回旧演示页"的分支，而演示页调的正是本次退役的
    `/api/jobs`；留下它就等于留一个点开即 404 的界面。开发环境改由 Vite (5173) 提供 UI，
    API 根路径在没有构建产物时统一 503 并说明怎么办。
    """
    dist_index = _FRONTEND_DIST / "index.html"
    if dist_index.is_file():
        return FileResponse(dist_index, media_type="text/html")
    raise HTTPException(
        status_code=503,
        detail="前端尚未构建：跑 `npm --prefix frontend run build`，或用 `npm run dev` 访问 5173",
    )


if __name__ == "__main__":
    # API 进程也走同一份日志配置：uvicorn 自带的 access 日志没有关联 ID，
    # 而我们自己的 logger 需要那个过滤器，否则 `%(request_id)s` 会打成异常。
    configure_logging()
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
