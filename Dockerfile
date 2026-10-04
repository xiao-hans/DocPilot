FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/docpilot

# 先装依赖再拷代码：改一行应用代码不该重装一遍 pandas。
# apt 只补 onnxruntime 的运行时库（libgomp），不装编译链——本轮依赖全有 linux wheel，
# 装不上就该以构建失败暴露，而不是静默拉长。
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY alembic.ini ./
COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts

# 非 root。命名卷在首次挂载时会继承镜像里这三个空目录的属主，所以先建再 chown。
RUN useradd --create-home --uid 10001 docpilot \
    && mkdir -p data uploads outputs \
    && chown -R docpilot:docpilot /srv/docpilot
USER docpilot

VOLUME ["/srv/docpilot/data"]

EXPOSE 8000

# slim 镜像没有 curl；探针本身必须是"这个进程能不能服务请求"，不是容器还在不在。
HEALTHCHECK --interval=10s --timeout=3s --start-period=25s --retries=12 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
