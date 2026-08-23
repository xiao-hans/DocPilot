"""FastAPI 入口：路由挂载 + 静态页面 + 启动初始化。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api import routes
from app.config import UPLOAD_DIR


@asynccontextmanager
async def lifespan(app: FastAPI):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="DocPilot", lifespan=lifespan)
app.include_router(routes.router)

_STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(_STATIC_DIR / "index.html", media_type="text/html")


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
