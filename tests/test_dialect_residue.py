"""阶段 G4 收口用例：结果权威源、NUL 入库边界、时间戳格式与聚合键序（docs/17 §6）。

这些缺陷的共同点是"只在某一种方言或某一个读取时刻才显形"，因此每条用例都写成
可跨方言重复的形式：本机 SQLite 与 `TEST_DATABASE_URL` 指向的 PostgreSQL 必须给出同样的结论。
"""

from __future__ import annotations

import ast
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import openpyxl
import pytest
from sqlalchemy import select

from app.contexts.review import repository
from app.contexts.review import service as review
from app.contexts.review.domain import ExtractionResult
from app.db import SessionLocal
from app.models.document import DocumentVersion
from app.models.schema import INVOICE_TEMPLATE
from app.services.extraction.base import ExtractResult, ExtractorBase
from app.services.job_manager import get_job_manager
from app.services.timestamps import iso_utc

FIELDS = json.dumps(INVOICE_TEMPLATE)
ISO_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _upload(owner_workspace: dict, pdf: Path) -> str:
    response = owner_workspace["client"].post(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))],
        data={"fields": FIELDS},
    )
    assert response.status_code == 201, response.text
    return response.json()["job_id"]


def _result_values(owner_workspace: dict, job_id: str) -> dict:
    rows = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{job_id}/result"
    ).json()["result"]
    assert len(rows) == 1
    return rows[0]["values"]


def test_result_and_excel_read_the_revised_values_not_the_snapshot(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-DATA-002 之外 G4 的核心：改过的字段不能在任何读路径上退回机器值。"""
    job_id = _upload(owner_workspace, make_invoice_pdf())
    machine = _result_values(owner_workspace, job_id)
    listed = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/results?job_id={job_id}"
    ).json()["results"]
    patched = owner_workspace["client"].patch(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/results/{listed[0]['id']}",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        json={
            "values": {**listed[0]["values"], "销售方名称": "已核对公司"},
            "expected_revision": 1,
        },
    )
    assert patched.status_code == 200, patched.text

    revised = _result_values(owner_workspace, job_id)
    assert revised["销售方名称"] == "已核对公司"
    assert revised["发票号码"] == machine["发票号码"]

    sheet = openpyxl.load_workbook(
        io.BytesIO(
            owner_workspace["client"].get(
                f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{job_id}/excel"
            ).content
        )
    ).active
    header = [cell.value for cell in sheet[1]]
    row = [cell.value for cell in sheet[2]]
    assert row[header.index("销售方名称")] == "已核对公司"

    with SessionLocal() as db:
        snapshot = db.execute(
            select(ExtractionResult).where(ExtractionResult.job_id == job_id)
        ).scalar_one()
        assert snapshot.state == "revised" and snapshot.revision_no == 2


class _DirtyExtractor(ExtractorBase):
    """模拟带 NUL 的解析/模型输出：真实 OCR 与外部模型都会给出这种字符串。"""

    def extract(self, text, fields, metadata=None) -> ExtractResult:
        return ExtractResult(
            values={field.name: f"值\x00{field.name}" for field in fields},
            warnings=["解析含\x00的告警"],
        )


def test_machine_values_and_warnings_lose_nul_at_the_write_boundary(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 经工厂替换（维护轮 M17 起流水线每次执行时解析工厂，所以这里对生产路径生效）；
    # 显式注入仍优先，所以 `_pipeline.extractor` 在注入了实现时会盖过工厂。
    monkeypatch.setattr("app.services.pipeline.get_extractor", lambda mode: _DirtyExtractor())
    monkeypatch.setattr(get_job_manager()._pipeline, "_injected_extractor", None)
    job_id = _upload(owner_workspace, make_invoice_pdf())

    values = _result_values(owner_workspace, job_id)
    assert all("\x00" not in value for value in values.values())
    assert values["发票号码"] == "值\x00发票号码".replace("\x00", "")

    with SessionLocal() as db:
        row = db.execute(
            select(ExtractionResult).where(ExtractionResult.job_id == job_id)
        ).scalar_one()
        assert "\x00" not in json.dumps(row.values, ensure_ascii=False)
        assert row.warnings == ["解析含\x00的告警".replace("\x00", "")]


def test_extracted_text_loses_nul_and_keeps_the_recorded_char_count(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    job_id = _upload(owner_workspace, make_invoice_pdf())
    with SessionLocal() as db:
        version = db.query(DocumentVersion).filter_by(job_id=job_id).one()
        result = db.execute(
            select(ExtractionResult).where(ExtractionResult.job_id == job_id)
        ).scalar_one()
        review.record_machine_result(
            db,
            tenant_id=owner_workspace["tenant_id"],
            job_id=job_id,
            filename=version.original_filename,
            text="发票\x00号码\n第二行",
            fields=result.fields,
            values={"发票号码": "5239\x00"},
            warnings=["脏\x00告警"],
        )
        db.commit()
        stored = repository.get_text(
            db, owner_workspace["tenant_id"], version.id
        )
        assert stored.text == "发票号码\n第二行"
        assert stored.char_count == len(stored.text)
        reloaded = db.get(ExtractionResult, result.id)
        assert reloaded.values["发票号码"] == "5239"


def _iso_strings(node, path="$"):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _iso_strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _iso_strings(value, f"{path}[{index}]")
    elif isinstance(node, str) and ISO_PREFIX.match(node):
        yield path, node


def test_every_response_timestamp_has_one_format_across_endpoints(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """同一份代码在 SQLite 与 PostgreSQL 上必须给出同样的对外时间戳形状。"""
    job_id = _upload(owner_workspace, make_invoice_pdf())
    tenant = owner_workspace["tenant_id"]
    client = owner_workspace["client"]
    listed = client.get(f"/api/v1/tenants/{tenant}/results?job_id={job_id}").json()

    responses = {
        "jobs": client.get(f"/api/v1/tenants/{tenant}/jobs").json(),
        "job": client.get(f"/api/v1/tenants/{tenant}/jobs/{job_id}").json(),
        "result": client.get(f"/api/v1/tenants/{tenant}/jobs/{job_id}/result").json(),
        "results": listed,
        "result_detail": client.get(
            f"/api/v1/tenants/{tenant}/results/{listed['results'][0]['id']}"
        ).json(),
        "revisions": client.get(
            f"/api/v1/tenants/{tenant}/results/{listed['results'][0]['id']}/revisions"
        ).json(),
        "audit": client.get(f"/api/v1/tenants/{tenant}/audit-events").json(),
        "documents": client.get(f"/api/v1/tenants/{tenant}/documents").json(),
        "templates": client.get(f"/api/v1/tenants/{tenant}/templates").json(),
        "rules": client.get(f"/api/v1/tenants/{tenant}/automation/rules").json(),
        "notifications": client.get(
            f"/api/v1/tenants/{tenant}/automation/notifications"
        ).json(),
        "usage": client.get(f"/api/v1/tenants/{tenant}/usage").json(),
        "members": client.get(f"/api/v1/tenants/{tenant}/members").json(),
        "invitations": client.get(f"/api/v1/tenants/{tenant}/invitations").json(),
    }

    checked = 0
    for name, body in responses.items():
        for path, value in _iso_strings(body):
            checked += 1
            assert value.endswith("Z"), f"{name}{path} 返回 {value!r}，不是统一的 ...Z 形状"
    assert checked > 10, f"扫到的时间戳太少（{checked}），用例本身没有覆盖到东西"


def test_no_hand_written_timestamp_serialisation_left_in_the_app() -> None:
    """把"响应时间戳只经 `iso_utc`"变成结构性约束，避免新端点又漏一种方言形状。

    唯一豁免：`timestamps.py` 自身，以及把 `date`（不是时刻）格式化成 `YYYY-MM-DD` 的提取层。
    """
    root = Path(__file__).resolve().parent.parent / "app"
    allowed = {"services/timestamps.py", "services/extraction/base.py"}
    offenders: list[str] = []
    for source in sorted(root.rglob("*.py")):
        relative = source.relative_to(root).as_posix()
        if relative in allowed or "__pycache__" in source.parts:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "isoformat"
            ):
                offenders.append(f"{relative}:{node.lineno}")
    assert offenders == [], f"仍在手写时间戳序列化（应改用 app.services.timestamps.iso_utc）：{offenders}"


def test_iso_utc_normalises_naive_aware_and_missing_values() -> None:
    naive = datetime(2026, 10, 3, 7, 20, 0, 123456)
    aware = naive.replace(tzinfo=timezone.utc)

    assert iso_utc(naive) == iso_utc(aware) == "2026-10-03T07:20:00.123456Z"
    assert iso_utc(None) is None


def test_operations_aggregates_keep_a_stable_key_order(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """契约：运营视图里直接来自聚合的 dict 键序稳定（顺序由 SQL 排序提供）。

    本用例单独看并不能证明"少了 ORDER BY 就会失败"——PG 在这个数据集上恰好走了索引扫描。
    排序是否真的下推到数据库，由 `test_aggregate_queries_ask_the_database_to_order_them` 把关。
    """
    for index in range(3):
        _upload(owner_workspace, make_invoice_pdf(f"011002000111000452{index:02d}"))
    documents = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/documents"
    ).json()["items"]
    assert len(documents) == 3

    for document, status in zip(documents[:2], ("purged", "purge_scheduled"), strict=True):
        patched = owner_workspace["client"].patch(
            f"/api/v1/tenants/{owner_workspace['tenant_id']}/documents/{document['id']}",
            headers={"X-CSRF-Token": owner_workspace["csrf"]},
            json={"retention_status": status},
        )
        assert patched.status_code == 200, patched.text

    retention = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/usage"
    ).json()["documents"]["retention"]
    assert retention == {"active": 1, "purge_scheduled": 1, "purged": 1}
    assert list(retention) == ["active", "purge_scheduled", "purged"]


class _StatementRecorder:
    """只记录语句、不碰数据库的假 Session：用于断言排序确实下推到了 SQL。"""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement):
        self.statements.append(str(statement))

        class _Empty:
            @staticmethod
            def all():
                return []

        return _Empty()


def test_aggregate_queries_ask_the_database_to_order_them() -> None:
    from app.contexts.operations import service as operations
    from app.models.document import Document
    from app.models.persistent_job import PersistentJob

    recorder = _StatementRecorder()
    operations._grouped(recorder, Document.retention_status, [Document.tenant_id == "tenant"])
    operations._by_tenant(
        recorder,
        PersistentJob.status,
        PersistentJob.tenant_id,
        [PersistentJob.tenant_id == "tenant"],
    )

    assert len(recorder.statements) == 2
    for statement in recorder.statements:
        assert "ORDER BY" in statement.upper(), statement
