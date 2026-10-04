"""Compose 与镜像的形状守卫。

这一轮容器化踩到的两个坑都属于"错了也照样能改文件"那一类，所以放在这里当用例而不是靠人眼：

- `docker-compose.yml` 里写了第二个顶层 `services:`，YAML 解析器默认**静默接受重复键**，
  于是文件看着正常、`docker compose config` 才报；
- `Dockerfile` 的 HEALTHCHECK 指到一个不存在的端点，容器会永远 unhealthy，而没有任何地方
  告诉你原因。

两条都不需要起 Docker，纯文本判定，因此不会因环境缺位而跳过。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

BASE = Path(__file__).resolve().parent.parent
COMPOSE_FILE = BASE / "docker-compose.yml"
DOCKERFILE = BASE / "Dockerfile"
DOCKERIGNORE = BASE / ".dockerignore"

INFRA_SERVICES = {"postgres", "rabbitmq", "object-store"}
APP_SERVICES = {"migrate", "api", "dispatcher", "worker"}


class DuplicateKey(Exception):
    pass


def load_no_duplicates(text: str):
    """带重复键检查的加载：Compose 的 YAML 子集里，重复键一定是事故而不是意图。"""

    class Loader(yaml.SafeLoader):
        pass

    def construct_mapping(loader, node, deep=False):
        seen: dict[object, int] = {}
        for key_node, _ in node.value:
            # `<<: *anchor` 是 Compose 用来展开公共片段的合并键，出现多次是正常写法。
            if key_node.tag.endswith("merge"):
                continue
            key = loader.construct_object(key_node, deep=True)
            line = key_node.start_mark.line + 1
            if key in seen:
                raise DuplicateKey(
                    f"重复的键 {key!r}：先出现在第 {seen[key]} 行，又出现在第 {line} 行"
                )
            seen[key] = line
        return yaml.SafeLoader.construct_mapping(loader, node, deep)  # type: ignore[attr-defined]

    Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    return yaml.load(text, Loader=Loader)


def test_duplicate_key_guard_itself_rejects():
    """守卫本身要有负向用例：不然它只是个装饰。"""
    with pytest.raises(DuplicateKey):
        load_no_duplicates("services:\n  a: 1\nservices:\n  b: 2\n")


def test_compose_file_has_no_duplicate_keys():
    load_no_duplicates(COMPOSE_FILE.read_text(encoding="utf-8"))


def test_compose_services_and_profiles():
    model = load_no_duplicates(COMPOSE_FILE.read_text(encoding="utf-8"))
    services = model["services"]
    assert INFRA_SERVICES <= set(services), sorted(services)
    assert APP_SERVICES <= set(services), sorted(services)

    # 默认 `docker compose up` 必须仍然只有基础设施——G1 以来的开发基线，容器化不该动它。
    for name in INFRA_SERVICES:
        assert "profiles" not in services[name], f"{name} 被挂上 profile 会让默认 up 变形态"
    for name in APP_SERVICES:
        assert services[name].get("profiles") == ["app"], f"{name} 必须只在 app profile 下启动"

    # migrate 是一次性的，常驻三个是服务；这条区分决定了 depends_on 的等待条件怎么写。
    assert services["migrate"].get("restart") in ("no", None)
    assert "condition" in str(services["api"]["depends_on"]), "api 必须等 migrate 成功而不是等它存在"


def test_app_env_reads_container_side_addresses_only():
    model = load_no_duplicates(COMPOSE_FILE.read_text(encoding="utf-8"))
    app_env = model["x-app-env"]
    # 容器里的 DATABASE_URL 必须由 APP_DATABASE_URL 供给：主机侧脚本用的是 127.0.0.1 上
    # 发布的端口，那套地址在容器网络里连不到依赖，两者不能共用一个键。
    assert app_env["DATABASE_URL"] == "${APP_DATABASE_URL:-}"
    assert app_env["BROKER_URL"] == "${APP_BROKER_URL:-}"
    # 对象端点也必须走容器侧专用键：主机侧 .env 里那个同名键填的是 127.0.0.1，
    # 同名复用会让容器里的应用去连自己。
    assert app_env["S3_ENDPOINT_URL"] == "${APP_S3_ENDPOINT_URL:-http://object-store:8333}"
    for key, value in app_env.items():
        assert ":?}" not in value, (
            f"x-app-env.{key} 用了 ${{VAR:?}}：顶层扩展字段在解析整份文件时就会求值，"
            "没启用 app profile 的 `docker compose up` 也会被它拖垮"
        )


def test_dockerfile_healthcheck_points_at_a_real_endpoint():
    from app.main import app

    declared = set(_healthcheck_paths(DOCKERFILE.read_text(encoding="utf-8")))
    assert declared, "Dockerfile 没有 HEALTHCHECK：容器只报进程在，不报能不能服务请求"
    # 这版 FastAPI 的 `app.routes` 用 _IncludedRouter 包着，拿不到扁平路径；OpenAPI 的 paths
    # 是同一份注册表的展开，够回答"这个端点存在吗"。
    routes = set(app.openapi().get("paths", {}))
    missing = sorted(declared - routes)
    assert not missing, f"Dockerfile HEALTHCHECK 指向不存在的端点：{missing}（容器会一直 unhealthy）"


def _healthcheck_paths(text: str) -> list[str]:
    return re.findall(r"https?://[^/]+(/[\w\-/]*)", text)


def test_healthcheck_guard_rejects_a_bogus_path():
    """上一条用例的负向证明：指到不存在的端点时必须能被挑出来。"""
    from app.main import app

    routes = set(app.openapi().get("paths", {}))
    declared = set(_healthcheck_paths('CMD ["python", "-c", "urlopen(\'http://127.0.0.1:8000/nopez\')"]'))
    assert declared - routes == {"/nopez"}


def test_dockerignore_keeps_host_only_and_secret_content_out():
    excluded = {
        line.strip().rstrip("/")
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    # 凭据与宿主机专属二进制进了镜像就出不来，而且是跨机传播。
    for required in (".env", ".venv", ".git"):
        assert required in excluded, f".dockerignore 必须排除 {required}"


def test_dockerfile_copies_only_what_dockerignore_keeps():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    ignored = {
        line.strip().rstrip("/")
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    copied: list[str] = []
    for line in dockerfile.splitlines():
        if line.strip().upper().startswith("COPY "):
            parts = line.split()[1:]
            copied.extend(p for p in parts if not p.startswith("--from"))
    for source in copied:
        assert source not in ignored, f"Dockerfile COPY {source}，但 .dockerignore 把它排除了——构建会失败"
