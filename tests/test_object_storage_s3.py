"""真实 S3 端点上的对象存储用例（docs/17 §5、TC-DATA-002 的 S3 分支）。

需要本机 Compose 的 SeaweedFS：在 `.env` 配好 `S3_ENDPOINT_URL` 与 `S3_BUCKET` 后运行；
未配置则整组跳过，并按覆盖缺口记录（跳过不等于通过）。SeaweedFS 只是本机模拟器，
这些用例证明"适配器与 S3 语义相符"，不构成生产存储选型。
每条用例使用独立对象前缀，结束时报出并清掉自己写入的对象。
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from app.config import S3_ACCESS_KEY, S3_BUCKET, S3_ENDPOINT_URL, S3_REGION, S3_SECRET_KEY
from app.db import SessionLocal
from app.models.document import DocumentVersion
from app.models.schema import INVOICE_TEMPLATE
from app.services.object_storage import LocalStorage, S3Storage

pytestmark = pytest.mark.s3

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"

@pytest.fixture(autouse=True)
def require_real_object_store() -> None:
    """缺端点时在"用例已被选中之后"才跳过。

    与 `test_queue_broker.py` 同一个理由：模块级 skip 发生在收集期，`-m "not s3"` 还没筛掉
    本模块就已经记了一条 skipped，把"跳过不等于通过"的账目弄脏。
    """
    if not (S3_ENDPOINT_URL and S3_BUCKET):
        pytest.skip("未设置 S3_ENDPOINT_URL / S3_BUCKET：真实 S3 用例未执行（覆盖缺口，不算通过）")


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bucket(prefix: str) -> S3Storage:
    storage = S3Storage(
        bucket=S3_BUCKET,
        endpoint_url=S3_ENDPOINT_URL,
        region=S3_REGION,
        access_key=S3_ACCESS_KEY,
        secret_key=S3_SECRET_KEY,
        prefix=prefix,
    )
    try:
        storage._client.head_bucket(Bucket=S3_BUCKET)
    except ClientError:
        storage._client.create_bucket(Bucket=S3_BUCKET)
    return storage


def _drain(storage: S3Storage) -> None:
    for key in list(storage.list_keys()):
        storage.delete(key)


@pytest.fixture()
def s3(request) -> Iterator[S3Storage]:
    storage = _bucket(f"docpilot-it/{uuid.uuid4().hex[:12]}")
    yield storage
    # 用例自己的前缀，结束即清空，不给模拟器留测试垃圾。
    _drain(storage)


@pytest.fixture()
def s3_mode(request, monkeypatch) -> S3Storage:
    """把应用整体切到 S3 后端，并给本次用例一个独立对象前缀。"""
    prefix = f"docpilot-it/{uuid.uuid4().hex[:12]}"
    monkeypatch.setattr("app.config.STORAGE_BACKEND", "s3")
    monkeypatch.setattr("app.config.S3_KEY_PREFIX", prefix)
    storage = _bucket(prefix)
    request.addfinalizer(lambda: _drain(storage))
    return storage


@pytest.fixture()
def to_s3(request, monkeypatch):
    """在用例执行中途才切到 S3 后端：回填用例必须先以 local 写出"存量数据"。"""

    def enable() -> S3Storage:
        prefix = f"docpilot-it/{uuid.uuid4().hex[:12]}"
        monkeypatch.setattr("app.config.STORAGE_BACKEND", "s3")
        monkeypatch.setattr("app.config.S3_KEY_PREFIX", prefix)
        storage = _bucket(prefix)
        request.addfinalizer(lambda: _drain(storage))
        return storage

    return enable


def _upload(owner_workspace, make_invoice_pdf) -> str:
    pdf = make_invoice_pdf()
    response = owner_workspace["client"].post(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))],
        data={"fields": json.dumps(INVOICE_TEMPLATE)},
    )
    assert response.status_code == 201, response.text
    return response.json()["job_id"]


def test_s3_roundtrip_keeps_content_type_and_size(s3):
    s3.put("tenant/job/a.pdf", io.BytesIO(b"%PDF-1.4"), content_type="application/pdf")

    head = s3._client.head_object(Bucket=S3_BUCKET, Key=f"{s3._prefix}/tenant/job/a.pdf")
    assert head["ContentType"] == "application/pdf"
    assert s3.stat("tenant/job/a.pdf").size == 8
    assert s3.read_bytes("tenant/job/a.pdf") == b"%PDF-1.4"
    assert list(s3.list_keys("tenant/job")) == ["tenant/job/a.pdf"]


def test_s3_missing_object_is_absent_not_an_error(s3):
    assert s3.stat("tenant/job/none.pdf").exists is False
    assert s3.delete("tenant/job/none.pdf") is False
    with pytest.raises(FileNotFoundError):
        s3.open_read("tenant/job/none.pdf")


def test_s3_put_overwrites_and_read_back_matches_digest(s3):
    s3.put("tenant/job/a.pdf", io.BytesIO(b"first"), content_type="application/pdf")
    payload = b"second-longer"
    s3.put("tenant/job/a.pdf", io.BytesIO(payload), content_type="application/pdf")

    assert hashlib.sha256(s3.read_bytes("tenant/job/a.pdf")).hexdigest() == hashlib.sha256(
        payload
    ).hexdigest()
    assert len(list(s3.list_keys())) == 1


def test_s3_local_path_materialises_a_temp_file_and_removes_it(s3):
    s3.put("tenant/job/a.pdf", io.BytesIO(b"content"), content_type="application/pdf")

    with s3.local_path("tenant/job/a.pdf") as path:
        assert path.suffix == ".pdf"
        assert path.read_bytes() == b"content"
        temp_dir = path.parent
    # 解析器拿到的是临时副本，退出后必须消失，否则会累积没被任何人认领的文档明文。
    assert not temp_dir.exists()


def test_s3_delete_prefix_matches_whole_segments(s3):
    for key in ("tenant/job-1/a.pdf", "tenant/job-2/b.pdf", "tenant/job-10/c.pdf"):
        s3.put(key, io.BytesIO(b"x"), content_type="application/pdf")

    assert s3.delete_prefix("tenant/job-1") == 1
    assert sorted(s3.list_keys()) == ["tenant/job-10/c.pdf", "tenant/job-2/b.pdf"]


def test_s3_rejects_keys_outside_the_namespace(s3):
    for key in ("../escape.pdf", "/abs.pdf", "tenant/../job/a.pdf"):
        with pytest.raises(ValueError):
            s3.put(key, io.BytesIO(b"x"))
        with pytest.raises(ValueError):
            s3.stat(key)


def test_uploaded_job_reads_back_through_the_s3_adapter(
    owner_workspace, s3_mode, make_invoice_pdf
):
    """写入与解析都只经过去过对象存储时，任务仍能跑到成功：证明 Worker 不依赖本机路径。"""
    job_id = _upload(owner_workspace, make_invoice_pdf)
    with SessionLocal() as db:
        version = db.query(DocumentVersion).filter_by(job_id=job_id).one()
        assert s3_mode.stat(version.storage_key).size == version.size_bytes

    detail = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{job_id}"
    ).json()
    assert detail["status"] == "succeeded"
    assert detail["result"][0]["values"]["开票日期"] == "2025-01-12"
    assert list(s3_mode.list_keys("nonexistent-prefix")) == []


def test_backfill_copies_verifies_and_is_idempotent(
    owner_workspace, tmp_path, monkeypatch, make_invoice_pdf, to_s3, capsys
):
    upload_root = tmp_path / "legacy-uploads"
    monkeypatch.setattr("app.config.UPLOAD_DIR", upload_root)
    job_id = _upload(owner_workspace, make_invoice_pdf)  # 默认 local 后端：对象先落在本机目录
    tenant_id = owner_workspace["tenant_id"]
    with SessionLocal() as db:
        version = db.query(DocumentVersion).filter_by(job_id=job_id).one()
        key, checksum = version.storage_key, version.checksum
    legacy = LocalStorage(upload_root)
    assert legacy.stat(key).exists

    s3_mode = to_s3()
    capsys.readouterr()
    assert _load_script("storage_backfill_to_object_store").main() == 0
    assert "新复制=1" in capsys.readouterr().out
    assert hashlib.sha256(s3_mode.read_bytes(key)).hexdigest() == checksum

    capsys.readouterr()
    assert _load_script("storage_backfill_to_object_store").main() == 0
    out = capsys.readouterr().out
    assert "新复制=0" in out and "已在校验通过=1" in out

    # 负向：判据必须是校验和，不能是"对象存在"。
    s3_mode.put(key, io.BytesIO(b"tampered"), content_type="application/pdf")
    capsys.readouterr()
    assert _load_script("storage_backfill_to_object_store").main() == 1
    assert "目标已存在但校验不符" in capsys.readouterr().out

    s3_mode.put(key, io.BytesIO(legacy.read_bytes(key)), content_type="application/pdf")
    capsys.readouterr()
    assert _load_script("storage_orphan_audit").main() == 0
    assert f"{tenant_id}/{job_id}" not in capsys.readouterr().out
