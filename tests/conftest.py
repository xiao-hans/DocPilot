"""测试基座：在导入应用前把配置指向一次性目录，避免污染开发库与真实上传目录。"""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
# 先读 .env 再决定测试库，使 TEST_DATABASE_URL 可以放进本地 .env 而不必每次导出环境变量。
load_dotenv(BASE_DIR / ".env")

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="docpilot-pytest-"))

# 设置 TEST_DATABASE_URL 时整轮用例改跑 PostgreSQL；未设置时继续用一次性 SQLite 文件。
_PG_TEST_URL = os.getenv("TEST_DATABASE_URL", "").strip()
_TARGET_DATABASE_URL = _PG_TEST_URL or f"sqlite:///{(_TMP_ROOT / 'test.sqlite3').as_posix()}"

os.environ.update(
    {
        "APP_ENV": "development",
        "AUTH_COOKIE_SECURE": "false",
        "BOOTSTRAP_TOKEN": "pytest-deployment-token",
        "DATABASE_URL": _TARGET_DATABASE_URL,
        "DATA_DIR": str(_TMP_ROOT),
        "EXTRACTOR_MODE": "mock",
        "OUTPUT_DIR": str(_TMP_ROOT / "outputs"),
        "UPLOAD_DIR": str(_TMP_ROOT / "uploads"),
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete, text  # noqa: E402

from app.contexts.processing.domain import JobAttempt
from app.contexts.automation.domain import EventRule, NotificationRequest, RuleRun
from app.contexts.review.domain import DocumentText, ExtractionResult, ResultRevision
from app.contexts.search.domain import SearchEntry
from app.contexts.templates.domain import Template, TemplateVersion
from app.contexts.webhooks.domain import WebhookDelivery, WebhookEndpoint
from app.db import (  # noqa: E402
    SessionLocal,
    assert_disposable_database,
    engine,
    initialize_development_schema,
)
from app.main import app  # noqa: E402
from app.models.access import AuditEvent, Invitation  # noqa: E402
from app.models.account import AuthSession, BootstrapState, Membership, Tenant, User  # noqa: E402
from app.models.document import Document, DocumentLabel, DocumentVersion  # noqa: E402
from app.models.outbox import DomainEventRecord  # noqa: E402
from app.models.persistent_job import PersistentJob  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.services.job_manager import get_job_manager  # noqa: E402

if _PG_TEST_URL:
    # 重建 schema 会清空该库，因此先做一次性库命名校验：
    # 子串含 test 的判断会放过 `latest` 这类正常库名，不可用于破坏性操作。
    assert_disposable_database(_PG_TEST_URL)
    with engine.begin() as _connection:
        _connection.execute(text("DROP SCHEMA public CASCADE"))
        _connection.execute(text("CREATE SCHEMA public"))

# 迁移只依赖 alembic，不依赖 TestClient 生命周期，便于不启动 lifespan 的用例直接访问数据库。
initialize_development_schema()

BOOTSTRAP_TOKEN = "pytest-deployment-token"
PASSWORD = "test-password-1234"

# 先删子表再删父表，满足 SQLite 外键约束。
_TABLES_IN_DELETE_ORDER = (
    WebhookDelivery.__table__,
    WebhookEndpoint.__table__,
    RuleRun.__table__,
    NotificationRequest.__table__,
    SearchEntry.__table__,
    EventRule.__table__,
    ResultRevision.__table__,
    ExtractionResult.__table__,
    DocumentText.__table__,
    DocumentLabel.__table__,
    DomainEventRecord.__table__,
    JobAttempt.__table__,
    AuditEvent.__table__,
    Invitation.__table__,
    DocumentVersion.__table__,
    Document.__table__,
    PersistentJob.__table__,
    TemplateVersion.__table__,
    Template.__table__,
    AuthSession.__table__,
    Membership.__table__,
    BootstrapState.__table__,
    User.__table__,
    Tenant.__table__,
)


@pytest.fixture(autouse=True)
def clean_database():
    """每个用例从空库开始：bootstrap 一次性入口与用户唯一索引才不会互相污染。"""
    with engine.begin() as connection:
        for table in _TABLES_IN_DELETE_ORDER:
            connection.execute(delete(table))
    yield


@pytest.fixture(autouse=True)
def pinned_run_form(request, monkeypatch: pytest.MonkeyPatch) -> None:
    """用例的队列形态由标记决定，不跟着调用者的 shell 走。

    `BROKER_URL` 一旦被导出（比如同一条 shell 里刚跑完 `-m broker` 又去跑 `-m s3`），
    上传端点就把任务投进队列而不是进程内执行；没有 Worker 消费时任务停在 `queued`，
    于是与队列无关的门禁会因为环境里多一个变量而红——排查方向完全指错地方。
    真实 broker 用例（`-m broker`）自己把开关打开，`queue_mode` 这类夹具在函数体内
    再设一次，仍会覆盖这里的默认值。
    """
    if request.node.get_closest_marker("broker"):
        return
    monkeypatch.setattr("app.api.workspace_jobs.QUEUE_ENABLED", False)
    monkeypatch.setattr("app.contexts.processing.service.QUEUE_ENABLED", False)


@pytest.fixture(autouse=True)
def clean_job_manager():
    """同步清理进程级任务缓存，避免数据库清库后残留任务污染后续用例。"""
    manager = get_job_manager()
    with manager._lock:
        manager._jobs.clear()
        manager._processing.clear()
        manager._staged.clear()
    yield
    with manager._lock:
        manager._jobs.clear()
        manager._processing.clear()
        manager._staged.clear()


def _login(
    email: str,
    tenant_id: str | None,
    user_id: str,
    *,
    clients: list[TestClient],
) -> dict:
    """为已存在的账号开一个独立 cookie jar 并登录。"""
    logged_in = TestClient(app)
    clients.append(logged_in)
    response = logged_in.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {
        "client": logged_in,
        "email": email,
        "csrf": response.json()["csrf_token"],
        "tenant_id": tenant_id,
        "user_id": user_id,
    }


@pytest.fixture()
def client():
    """TestClient 生命周期会执行待处理迁移，并同步跑完后台任务。"""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把租户任务 API 的上传/输出目录指向用例私有目录。

    上传目录改的是 `app.config`：写入端与解析端都按当前配置取根目录，只 patch 其中一侧
    会让对象写到 tmp、读回真实目录。
    """
    monkeypatch.setattr("app.config.UPLOAD_DIR", tmp_path)
    monkeypatch.setattr("app.api.workspace_jobs.OUTPUT_DIR", tmp_path)
    return tmp_path


@pytest.fixture()
def owner_workspace(client) -> dict:
    """已完成首次初始化的租户成员：平台管理员与其唯一的活跃工作空间。"""
    response = client.post(
        "/api/v1/auth/bootstrap",
        headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
        json={"email": "owner@example.com", "password": PASSWORD, "tenant_name": "甲方空间"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return {
        "client": client,
        "email": "owner@example.com",
        "csrf": body["csrf_token"],
        "tenant_id": body["tenant_id"],
        "user_id": body["user_id"],
    }


@pytest.fixture()
def identity(request: pytest.FixtureRequest):
    """直接落库创建成员身份并登录，返回其独立客户端（每份身份各有一个 cookie jar）。"""
    clients: list[TestClient] = []

    def close_clients() -> None:
        for test_client in clients:
            test_client.close()

    request.addfinalizer(close_clients)

    def _create(email: str, tenant_name: str, role: str = "member") -> dict:
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            user = User(
                id=uuid.uuid4().hex,
                email=email,
                password_hash=hash_password(PASSWORD),
                is_active=True,
                created_at=now,
            )
            tenant = Tenant(
                id=uuid.uuid4().hex, name=tenant_name, status="active", created_at=now
            )
            db.add_all(
                [
                    user,
                    tenant,
                    Membership(
                        id=uuid.uuid4().hex,
                        tenant_id=tenant.id,
                        user_id=user.id,
                        role=role,
                        status="active",
                        created_at=now,
                    ),
                ]
            )
            db.commit()

        return _login(
            email, tenant.id, user.id, clients=clients
        )

    return _create


@pytest.fixture()
def member_of(request: pytest.FixtureRequest):
    """把新用户加入既有租户并登录，用于按角色参数化权限矩阵。"""
    clients: list[TestClient] = []

    def close_clients() -> None:
        for test_client in clients:
            test_client.close()

    request.addfinalizer(close_clients)

    def _add(email: str, tenant_id: str, role: str = "member") -> dict:
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            user = User(
                id=uuid.uuid4().hex,
                email=email,
                password_hash=hash_password(PASSWORD),
                is_active=True,
                created_at=now,
            )
            db.add(user)
            db.add(
                Membership(
                    id=uuid.uuid4().hex,
                    tenant_id=tenant_id,
                    user_id=user.id,
                    role=role,
                    status="active",
                    created_at=now,
                )
            )
            db.commit()
            user_id = user.id
        return _login(email, tenant_id, user_id, clients=clients)

    return _add


@pytest.fixture()
def platform_admin_without_membership(request: pytest.FixtureRequest):
    """平台管理员但没有加入任何租户。"""
    clients: list[TestClient] = []
    request.addfinalizer(lambda: [client.close() for client in clients])

    def _create(email: str = "ops@example.com") -> dict:
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            user = User(
                id=uuid.uuid4().hex,
                email=email,
                password_hash=hash_password(PASSWORD),
                is_active=True,
                is_platform_admin=True,
                created_at=now,
            )
            db.add(user)
            db.commit()
            user_id = user.id
        return _login(email, None, user_id, clients=clients)

    return _create


@pytest.fixture()
def other_workspace(identity) -> dict:
    """另一个客户的租户，用于跨租户负向断言。"""
    return identity("other@example.com", "丙方空间")


@pytest.fixture()
def add_membership():
    """把已存在的用户加入另一个租户，用于验证跨租户授权互不影响。"""

    def _add(user_id: str, tenant_id: str, role: str = "member") -> None:
        with SessionLocal() as db:
            db.add(
                Membership(
                    id=uuid.uuid4().hex,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    role=role,
                    status="active",
                    created_at=datetime.now(timezone.utc),
                )
            )
            db.commit()

    return _add


@pytest.fixture()
def make_invoice_pdf(tmp_path: Path):
    """生成含发票文本的 PDF，供 mock 提取器命中默认字段。"""

    def _make(invoice_no: str = "01100200011100045239") -> Path:
        import pymupdf

        path = tmp_path / f"invoice-{invoice_no[-4:]}.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text(
            (72, 72),
            "增值税普通发票\n"
            f"发票号码: {invoice_no}\n"
            "开票日期: 2025-01-12\n"
            "销售方: 恒信达科技有限公司\n"
            "购买方: 测试购买方有限公司\n"
            "价税合计(大写): 壹仟贰佰捌拾元伍角整\n"
            "价税合计(小写): ¥1280.50元\n"
            "税额: ¥76.83元\n",
        )
        doc.save(path)
        doc.close()
        return path

    return _make
