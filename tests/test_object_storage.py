"""对象存储本地实现、错误映射与孤儿审计脚本用例（docs/17 §5、TC-DATA-002）。

S3 实现另见 `test_object_storage_s3.py`（需要本机 SeaweedFS 端点）。
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from app.platform.db import SessionLocal
from app.models.document import DocumentVersion
from app.models.schema import INVOICE_TEMPLATE
from app.adapters.storage.object_storage import (
    LocalStorage,
    ObjectInfo,
    ObjectStorage,
    S3Storage,
    StorageError,
    build_object_storage,
)

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def storage(tmp_path) -> LocalStorage:
    return LocalStorage(tmp_path / "uploads")


@pytest.fixture()
def upload_root(tmp_path, monkeypatch) -> Path:
    """只放上传对象的存储根：`make_invoice_pdf` 会在别处生成样例文件，混在一起会让
    孤儿审计把样例当对象。
    """
    root = tmp_path / "objects"
    monkeypatch.setattr("app.platform.config.UPLOAD_DIR", root)
    return root


def _put(storage: ObjectStorage, key: str, payload: bytes) -> None:
    storage.put(key, io.BytesIO(payload), content_type="application/pdf")


def _s3_with(client) -> S3Storage:
    """绕过 boto3 装配一个只换 client 的 S3 实例，用于离线验证错误映射。"""
    storage = S3Storage.__new__(S3Storage)
    storage._bucket = "bucket"
    storage._prefix = "docpilot"
    storage._client = client
    return storage


class _ClientErrorRaiser:
    def __init__(self, code: str) -> None:
        self._code = code

    def _raise(self, operation: str):
        raise ClientError({"Error": {"Code": self._code}}, operation)

    def head_object(self, **_kwargs):
        self._raise("HeadObject")

    def get_object(self, **_kwargs):
        self._raise("GetObject")


def test_local_storage_roundtrip_reports_size_and_exists(storage):
    _put(storage, "tenant/job/a.pdf", b"%PDF-1.4")

    assert storage.stat("tenant/job/a.pdf") == ObjectInfo(exists=True, size=8)
    assert storage.read_bytes("tenant/job/a.pdf") == b"%PDF-1.4"
    assert storage.delete("tenant/job/a.pdf") is True
    assert storage.delete("tenant/job/a.pdf") is False
    assert storage.stat("tenant/job/a.pdf").exists is False


def test_local_storage_rejects_keys_outside_the_root(storage):
    for key in ("../outside.pdf", "/abs.pdf", "C:/abs.pdf", "tenant/../job/a.pdf"):
        with pytest.raises(ValueError):
            _put(storage, key, b"x")
        with pytest.raises(ValueError):
            storage.stat(key)
        with pytest.raises(ValueError):
            storage.delete(key)
        with pytest.raises(ValueError):
            storage.delete_prefix(key)
    assert list(storage.list_keys()) == []


def test_local_storage_overwrites_same_key(storage):
    _put(storage, "tenant/job/a.pdf", b"first")
    _put(storage, "tenant/job/a.pdf", b"second-longer")

    assert storage.read_bytes("tenant/job/a.pdf") == b"second-longer"
    assert storage.stat("tenant/job/a.pdf").size == 13


def test_local_path_yields_the_real_file_without_copying(storage, tmp_path):
    _put(storage, "tenant/job/a.pdf", b"content")

    with storage.local_path("tenant/job/a.pdf") as path:
        assert path == (tmp_path / "uploads" / "tenant" / "job" / "a.pdf").resolve()
        assert path.read_bytes() == b"content"


def test_local_path_raises_for_missing_object(storage):
    with pytest.raises(FileNotFoundError):
        with storage.local_path("tenant/job/missing.pdf"):
            pass


def test_list_keys_returns_relative_posix_keys(storage):
    _put(storage, "tenant/job/a.pdf", b"a")
    _put(storage, "tenant/job/nested/b.pdf", b"b")
    _put(storage, "other/job/c.pdf", b"c")

    assert sorted(storage.list_keys("tenant/job")) == [
        "tenant/job/a.pdf",
        "tenant/job/nested/b.pdf",
    ]
    assert len(list(storage.list_keys())) == 3


def test_delete_prefix_removes_objects_and_the_empty_directory_scaffolding(storage, tmp_path):
    _put(storage, "tenant/job/a.pdf", b"a")
    _put(storage, "tenant/job/nested/b.pdf", b"b")

    assert storage.delete_prefix("tenant/job") == 2
    assert list(storage.list_keys()) == []
    # 补偿之后不该留下空目录：否则每失败一次就多一层没人认领的脚手架。
    assert (tmp_path / "uploads").exists()
    assert list((tmp_path / "uploads").glob("*")) == []


def test_delete_prefix_matches_whole_key_segments(storage):
    _put(storage, "tenant/job-1/a.pdf", b"a")
    _put(storage, "tenant/job-2/b.pdf", b"b")
    _put(storage, "tenant/job-10/c.pdf", b"c")

    assert storage.delete_prefix("tenant/job-1") == 1
    assert sorted(storage.list_keys()) == ["tenant/job-10/c.pdf", "tenant/job-2/b.pdf"]


def test_delete_prefix_is_noop_for_missing_prefix(storage):
    _put(storage, "tenant/job/a.pdf", b"a")

    assert storage.delete_prefix("tenant/other") == 0
    assert storage.delete_prefix("tenant/job/a.pdf") == 1
    assert storage.delete_prefix("tenant/job/a.pdf") == 0


def test_local_cleanup_errors_propagate_instead_of_claiming_success(tmp_path, monkeypatch):
    storage = LocalStorage(tmp_path)
    _put(storage, "tenant/job/a.pdf", b"a")
    calls: list[str] = []

    def busy_rmtree(path, *args, **kwargs):
        calls.append(str(path))
        raise PermissionError("device busy")

    monkeypatch.setattr("app.adapters.storage.object_storage.shutil.rmtree", busy_rmtree)

    with pytest.raises(PermissionError):
        storage.delete_prefix("tenant/job")
    assert calls == [str(tmp_path / "tenant" / "job")]
    # 清理没成功时对象必须还在，报告与审计才有机会发现它，而不是被假装删掉了。
    assert storage.stat("tenant/job/a.pdf").exists


def test_s3_treats_missing_objects_as_absent_and_other_failures_as_errors():
    assert _s3_with(_ClientErrorRaiser("404")).stat("tenant/job/a.pdf").exists is False
    with pytest.raises(FileNotFoundError):
        _s3_with(_ClientErrorRaiser("NoSuchKey")).open_read("tenant/job/a.pdf")
    with pytest.raises(StorageError):
        _s3_with(_ClientErrorRaiser("AccessDenied")).stat("tenant/job/a.pdf")


def test_build_object_storage_follows_current_config(tmp_path, monkeypatch):
    monkeypatch.setattr("app.platform.config.STORAGE_BACKEND", "local")
    monkeypatch.setattr("app.platform.config.UPLOAD_DIR", tmp_path)

    storage = build_object_storage()
    assert isinstance(storage, LocalStorage)
    storage.put("tenant/job/a.pdf", io.BytesIO(b"a"))
    assert (tmp_path / "tenant" / "job" / "a.pdf").is_file()

    monkeypatch.setattr("app.platform.config.STORAGE_BACKEND", "dfs")
    with pytest.raises(RuntimeError, match="STORAGE_BACKEND"):
        build_object_storage()


def test_build_object_storage_requires_endpoint_for_s3(monkeypatch):
    monkeypatch.setattr("app.platform.config.STORAGE_BACKEND", "s3")
    monkeypatch.setattr("app.platform.config.S3_ENDPOINT_URL", "")
    monkeypatch.setattr("app.platform.config.S3_BUCKET", "")

    with pytest.raises(RuntimeError, match="S3_ENDPOINT_URL"):
        build_object_storage()


def _upload(owner_workspace, make_invoice_pdf, count: int = 1) -> str:
    files = [
        ("files", (f"invoice-{index}.pdf", make_invoice_pdf(f"011002000111000452{index:02d}").read_bytes(), "application/pdf"))
        for index in range(count)
    ]
    response = owner_workspace["client"].post(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        files=files,
        data={"fields": json.dumps(INVOICE_TEMPLATE)},
    )
    assert response.status_code == 201, response.text
    return response.json()["job_id"]


def test_upload_writes_every_object_through_the_adapter(
    owner_workspace, upload_root, make_invoice_pdf, monkeypatch
):
    seen: list[str] = []
    original_put = LocalStorage.put

    def record_put(self, key, source, *, content_type=None):
        seen.append(key)
        return original_put(self, key, source, content_type=content_type)

    monkeypatch.setattr(LocalStorage, "put", record_put)
    job_id = _upload(owner_workspace, make_invoice_pdf, count=2)

    prefix = f"{owner_workspace['tenant_id']}/{job_id}/"
    assert len(seen) == 2
    assert all(key.startswith(prefix) and "/" not in key[len(prefix) :] for key in seen)
    assert sorted(Path(key).suffix for key in seen) == [".pdf", ".pdf"]
    assert all((upload_root / key).is_file() for key in seen)


def test_orphan_audit_passes_when_objects_and_records_agree(
    owner_workspace, upload_root, make_invoice_pdf, capsys
):
    _upload(owner_workspace, make_invoice_pdf, count=2)
    capsys.readouterr()

    assert _load_script("storage_orphan_audit").main() == 0
    out = capsys.readouterr().out
    assert "对象数=2" in out and "记录数=2" in out and "一致" in out


def test_orphan_audit_reports_objects_without_records_without_leaking_keys(
    owner_workspace, upload_root, make_invoice_pdf, capsys
):
    job_id = _upload(owner_workspace, make_invoice_pdf)
    with SessionLocal() as db:
        recorded = db.query(DocumentVersion).filter_by(job_id=job_id).one().storage_key
    secret = f"{owner_workspace['tenant_id']}/{job_id}/deadbeefdeadbeefdeadbeefdeadbeef.pdf"
    _put(build_object_storage(), secret, b"orphan")
    capsys.readouterr()

    assert _load_script("storage_orphan_audit").main() == 1
    out = capsys.readouterr().out
    assert "  孤儿对象 " in out and "  断链记录" not in out
    assert f"job_id={job_id}" in out
    # 对象键是文件定位符，不进报告（docs/17 §7），只留内部 ID 与摘要。
    assert secret not in out and recorded not in out


def test_orphan_audit_reports_records_whose_object_is_gone(
    owner_workspace, upload_root, make_invoice_pdf, capsys
):
    job_id = _upload(owner_workspace, make_invoice_pdf)
    with SessionLocal() as db:
        recorded = db.query(DocumentVersion).filter_by(job_id=job_id).one().storage_key
    build_object_storage().delete(recorded)
    capsys.readouterr()

    assert _load_script("storage_orphan_audit").main() == 1
    out = capsys.readouterr().out
    assert "  断链记录" in out and "  孤儿对象" not in out and recorded not in out


def test_backfill_refuses_when_source_and_target_are_the_same_place(upload_root):
    script = _load_script("storage_backfill_to_object_store")

    with pytest.raises(SystemExit) as exit_info:
        script.main()
    assert "STORAGE_BACKEND" in str(exit_info.value)
