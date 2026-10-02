"""租户内检索：文件名、原文与字段值命中，索引随修订重建（TC-FUNC-031..033、TC-SEC-016）。"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import func, select

from app.contexts.search.domain import SearchEntry
from app.db import SessionLocal
from app.models.schema import INVOICE_TEMPLATE

FIELDS = json.dumps(INVOICE_TEMPLATE)
SERIAL = "01100200011100045239"


def _upload(workspace: dict, pdf: Path, name: str | None = None) -> str:
    response = workspace["client"].post(
        f"/api/v1/tenants/{workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": workspace["csrf"]},
        files=[("files", (name or pdf.name, pdf.read_bytes(), "application/pdf"))],
        data={"fields": FIELDS},
    )
    assert response.status_code == 201, response.text
    return response.json()["job_id"]


def _search(workspace: dict, **params) -> dict:
    query = "&".join(f"{key}={value}" for key, value in params.items())
    url = f"/api/v1/tenants/{workspace['tenant_id']}/search"
    response = workspace["client"].get(f"{url}?{query}" if query else url)
    assert response.status_code == 200, response.text
    return response.json()


def test_search_hits_filename_text_and_field_values(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-FUNC-031：一个查询词能从文件名、原文与字段值三类来源命中同一文档。"""
    _upload(owner_workspace, make_invoice_pdf(SERIAL), name=f"发票{SERIAL}.pdf")

    hits = _search(owner_workspace, q=SERIAL)["hits"]
    kinds = {hit["kind"] for hit in hits}
    assert {"filename", "text", "field"} <= kinds, hits
    assert all(SERIAL in hit["snippet"] for hit in hits), hits
    assert len({hit["document_id"] for hit in hits}) == 1

    field_hits = [hit for hit in hits if hit["kind"] == "field"]
    assert all(hit["field_name"] for hit in field_hits)

    dumped = json.dumps(_search(owner_workspace, q=SERIAL), ensure_ascii=False)
    assert str(sandbox) not in dumped and "storage_key" not in dumped


def test_kind_filter_narrows_hits(owner_workspace: dict, sandbox: Path, make_invoice_pdf) -> None:
    _upload(owner_workspace, make_invoice_pdf(SERIAL), name=f"发票{SERIAL}.pdf")
    assert {hit["kind"] for hit in _search(owner_workspace, q=SERIAL, kind="text")["hits"]} == {"text"}
    assert {hit["kind"] for hit in _search(owner_workspace, q=SERIAL, kind="filename")["hits"]} == {"filename"}
    assert _search(owner_workspace, q=SERIAL, kind="filename")["total"] == 1
    assert _search(owner_workspace, q="不存在的关键词XYZ", kind="all")["hits"] == []


def test_revision_rebuilds_index_without_growth(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-FUNC-032：改值后旧值不再命中、新值命中，且索引行数不增长。"""
    job_id = _upload(owner_workspace, make_invoice_pdf(SERIAL))
    detail = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/results?job_id={job_id}"
    ).json()["results"][0]
    old_serial = detail["values"]["发票号码"]
    assert _search(owner_workspace, q=old_serial, kind="field")["total"] == 1

    with SessionLocal() as db:
        rows = db.query(SearchEntry).count()

    new_serial = "99990000111122223333"
    revised = _patch_result(owner_workspace, detail, "发票号码", new_serial)
    assert revised.status_code == 200, revised.text

    # 旧值不再命中该字段条目，新值命中；索引行数不变（覆盖写而非追加）
    assert _search(owner_workspace, q=old_serial, kind="field")["total"] == 0
    assert _search(owner_workspace, q=new_serial, kind="field")["total"] == 1
    with SessionLocal() as db:
        assert db.query(SearchEntry).count() == rows


def _patch_result(workspace: dict, detail: dict, field: str, value: str):
    values = {**detail["values"], field: value}
    return workspace["client"].patch(
        f"/api/v1/tenants/{workspace['tenant_id']}/results/{detail['id']}",
        headers={"X-CSRF-Token": workspace["csrf"]},
        json={"values": values, "expected_revision": detail["revision_no"]},
    )


def test_query_validation_and_wildcards_are_literal(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-FUNC-033：长度校验生效，LIKE 通配符按字面匹配而不是命中全表。"""
    _upload(owner_workspace, make_invoice_pdf(SERIAL))
    base = f"/api/v1/tenants/{owner_workspace['tenant_id']}/search"

    assert owner_workspace["client"].get(f"{base}?q=a").status_code == 422
    assert owner_workspace["client"].get(f"{base}?q={'x' * 61}").status_code == 422
    assert owner_workspace["client"].get(f"{base}?q={SERIAL}&kind=bogus").status_code == 422

    for wildcard in ("%x", "_x", chr(92) + "x", "%_%"):
        body = owner_workspace["client"].get(base, params={"q": wildcard})
        assert body.status_code == 200, (wildcard, body.text)
        assert body.json()["hits"] == [], wildcard
    # 空白查询词去掉后长度不足，同样被拒
    assert owner_workspace["client"].get(f"{base}?q=%20%20").status_code == 422


def test_search_never_leaks_across_tenants(
    owner_workspace: dict, identity, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-SEC-016：他租户检索不到本租户独有内容，也无法按 ID 读取命中。"""
    _upload(owner_workspace, make_invoice_pdf(SERIAL))
    outsider = identity("outsider-search@example.com", "外部空间", "owner")

    assert outsider["client"].get(f"/api/v1/tenants/{outsider['tenant_id']}/search?q={SERIAL}").json() == {
        "total": 0,
        "hits": [],
    }
    mine = _search(owner_workspace, q=SERIAL)["hits"][0]["document_id"]
    outsider_documents = outsider["client"].get(
        f"/api/v1/tenants/{outsider['tenant_id']}/documents"
    ).json()
    assert all(item["id"] != mine for item in outsider_documents["items"])

    anonymous = outsider["client"]
    anonymous.cookies.clear()
    assert anonymous.get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/search?q={SERIAL}"
    ).status_code == 401

    with SessionLocal() as db:
        # 索引行确实只落在属主租户
        assert db.scalar(select(func.count(SearchEntry.id)).where(SearchEntry.tenant_id == owner_workspace["tenant_id"])) > 0
        assert db.scalar(
            select(func.count(SearchEntry.id)).where(SearchEntry.tenant_id == outsider["tenant_id"])
        ) == 0
