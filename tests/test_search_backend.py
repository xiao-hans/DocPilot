"""检索后端可替换性契约测试（NFR-004，`docs/22 §6`）。

`TC-FUNC-060` 已确认另外三类适配器有接缝，**只有搜索后端没有**——检索直接查
`search_entries` 表，没有可注入的接口。本文件补上那一类的判据。

与 `tests/test_adapter_contracts.py` 同一个纪律：替换点必须在**生产路径**上生效，
而不是只在构造函数层。M2 实测过一个反例（`DocumentPipeline` 构造时绑定 dispatcher，
换工厂函数对它无效），所以这里显式验"经工厂替换后 HTTP `/search` 走的是假实现"。

不改产品行为：默认后端与抽取前逐字一致，`tests/test_search.py` 是首要判据。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.platform import config
from app.contexts.search import backend as backend_module
from app.contexts.search.backend import SearchBackend, SqlSearchBackend, get_search_backend
from app.contexts.search.domain import SearchEntry
from app.platform.db import SessionLocal
from app.models.schema import INVOICE_TEMPLATE

FIELDS = json.dumps(INVOICE_TEMPLATE)
SERIAL = "01100200011100045239"


class _RecordingBackend(SearchBackend):
    """记录被调用时的入参，并把查询交给空结果。用于证明"经契约调用"。"""

    def __init__(self) -> None:
        self.reindex_calls: list[dict[str, Any]] = []
        self.query_calls: list[dict[str, Any]] = []

    def reindex(self, db, **kwargs) -> None:  # type: ignore[override]
        self.reindex_calls.append(kwargs)

    def query(self, db, **kwargs):  # type: ignore[override]
        self.query_calls.append(kwargs)
        return 0, []


@pytest.fixture()
def recording_backend(monkeypatch: pytest.MonkeyPatch) -> _RecordingBackend:
    recorder = _RecordingBackend()
    monkeypatch.setattr(backend_module, "get_search_backend", lambda: recorder)
    # service 里是 `from ... import get_search_backend`，名字已绑进 service 命名空间，
    # 只换 backend 模块不够——这正是 M2 记过的"绑定时机"陷阱。
    from app.contexts.search import service as search_service

    monkeypatch.setattr(search_service, "get_search_backend", lambda: recorder)
    return recorder


def _upload(workspace: dict, pdf: Path) -> str:
    response = workspace["client"].post(
        f"/api/v1/tenants/{workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": workspace["csrf"]},
        files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))],
        data={"fields": FIELDS},
    )
    assert response.status_code == 201, response.text
    return response.json()["job_id"]


# ---------------------------------------------------------------- 工厂

def test_default_backend_is_sql():
    assert isinstance(get_search_backend(), SqlSearchBackend)
    assert isinstance(get_search_backend(), SearchBackend)


def test_unknown_backend_raises_instead_of_silently_falling_back(monkeypatch):
    """配置写错要立刻失败。

    静默退回默认实现会让人以为换后端生效了，实际还在查原来的表——
    与 `STORAGE_BACKEND`、`EXTRACTOR_MODE` 同一套纪律。
    """
    monkeypatch.setattr(config, "SEARCH_BACKEND", "elastic", raising=False)
    monkeypatch.setattr(backend_module, "_backend", None, raising=False)
    monkeypatch.setattr(backend_module, "_backend_name", None, raising=False)
    with pytest.raises(RuntimeError, match="未知的 SEARCH_BACKEND"):
        get_search_backend()


def test_factory_reflects_config_changes(monkeypatch):
    """每次调用读配置：换配置后立刻换实现，不像流水线那样构造时绑定。"""
    monkeypatch.setattr(backend_module, "_backend", None, raising=False)
    monkeypatch.setattr(backend_module, "_backend_name", None, raising=False)
    monkeypatch.setattr(config, "SEARCH_BACKEND", "sql", raising=False)
    first = get_search_backend()
    monkeypatch.setattr(config, "SEARCH_BACKEND", "bogus", raising=False)
    with pytest.raises(RuntimeError):
        get_search_backend()
    monkeypatch.setattr(config, "SEARCH_BACKEND", "sql", raising=False)
    assert isinstance(get_search_backend(), SqlSearchBackend)
    assert first is not None


# ---------------------------------------------------------------- 生产路径

def test_query_goes_through_the_backend_on_the_http_path(
    recording_backend, owner_workspace, sandbox, make_invoice_pdf
):
    """经工厂替换后，HTTP `/search` 必须走假实现。

    这条是核心判据：契约的价值在于**生产路径**可替换。只测"工厂返回了假实现"
    证明不了 service 真的用它——M2 的教训。
    """
    _upload(owner_workspace, make_invoice_pdf())
    body = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/search?q={SERIAL}"
    ).json()
    assert body == {"total": 0, "hits": []}
    assert len(recording_backend.query_calls) == 1
    call = recording_backend.query_calls[0]
    assert call["tenant_id"] == owner_workspace["tenant_id"]
    # 假实现拿到的必须是**已转义**的模式（契约边界：转义属 service）
    assert call["pattern"].startswith("%") and call["pattern"].endswith("%")


def test_reindex_goes_through_the_backend_on_the_upload_path(
    recording_backend, owner_workspace, sandbox, make_invoice_pdf
):
    """写入侧同样经契约：上传→处理→复核写结果都会触发重建索引。"""
    _upload(owner_workspace, make_invoice_pdf())
    assert recording_backend.reindex_calls, "重建索引没有经后端契约调用"
    call = recording_backend.reindex_calls[-1]
    assert call["tenant_id"] == owner_workspace["tenant_id"]
    assert call["filename"].endswith(".pdf")
    # 字段值以 dict 传入，来源规则（哪些键进索引）留在 service
    assert isinstance(call["values"], dict)


def test_wildcards_are_escaped_before_reaching_the_backend(
    recording_backend, owner_workspace, sandbox, make_invoice_pdf
):
    """用户输入里的 `%` 必须在到达后端之前被转义。

    契约收的是"已转义的 pattern"，因为转义属于查询语义：换成词元匹配的后端时
    `%` 不再是通配符，规则完全不同。把转义放进契约会让每个后端各写一份。
    """
    _upload(owner_workspace, make_invoice_pdf())
    owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/search?q=%25abc"
    )
    call = recording_backend.query_calls[-1]
    assert "\\%" in call["pattern"], f"未转义的通配符泄漏到后端：{call['pattern']}"


def test_both_sides_are_required_by_the_contract():
    """契约把两侧放在同一个 ABC 里，缺任一侧都实例化不了。

    只换查询而保留写侧会让索引行与查询语义错位——例如换成词元匹配的查询、
    索引仍按原文整段存，结果永远零命中，而两侧各自的用例都可能是绿的。

    **两侧都要断言。** 第一版只断言了"只实现 query 会失败"，而那只证明 `reindex` 是抽象的；
    去掉 `query` 的 `@abstractmethod` 后它照样通过（反向验证照出来的）。
    """

    class OnlyQuery(SearchBackend):
        def query(self, db, **kwargs):  # type: ignore[override]
            return 0, []

    class OnlyReindex(SearchBackend):
        def reindex(self, db, **kwargs) -> None:  # type: ignore[override]
            return None

    with pytest.raises(TypeError, match="abstract"):
        OnlyQuery()  # type: ignore[abstract]
    with pytest.raises(TypeError, match="abstract"):
        OnlyReindex()  # type: ignore[abstract]


# ---------------------------------------------------------------- 行为不变

def test_sql_backend_writes_the_same_rows_as_before(owner_workspace, sandbox, make_invoice_pdf):
    """默认后端写出的索引行形状与 `docs/15 §5` 定的一致：filename/text/field 三类。"""
    job_id = _upload(owner_workspace, make_invoice_pdf())
    with SessionLocal() as db:
        kinds = {
            row[0]
            for row in db.execute(
                select(SearchEntry.kind).where(SearchEntry.tenant_id == owner_workspace["tenant_id"])
            ).all()
        }
    assert {"filename", "text", "field"} <= kinds, f"索引行类别不全：{kinds}"
    assert job_id


def test_reindex_is_idempotent_through_the_backend(owner_workspace, sandbox, make_invoice_pdf):
    """经契约重建索引仍然幂等：行数不随重跑增长（抽取前的行为不变）。"""
    from app.contexts.review import service as review

    job_id = _upload(owner_workspace, make_invoice_pdf())
    with SessionLocal() as db:
        before = db.execute(
            select(SearchEntry).where(SearchEntry.tenant_id == owner_workspace["tenant_id"])
        ).all()
        from app.contexts.review.domain import ExtractionResult

        result = db.execute(
            select(ExtractionResult).where(ExtractionResult.job_id == job_id)
        ).scalar_one()
        review.record_machine_result(
            db,
            tenant_id=owner_workspace["tenant_id"],
            job_id=job_id,
            filename=result.filename,
            text="再次重建",
            fields=result.fields,
            values=result.values,
            warnings=[],
        )
        db.commit()
        after = db.execute(
            select(SearchEntry).where(SearchEntry.tenant_id == owner_workspace["tenant_id"])
        ).all()
    assert len(after) == len(before), "重建索引后行数变了，幂等性被破坏"


def test_backend_module_does_not_import_service_at_module_level():
    """依赖方向（`docs/22 §5`）：`backend` 不在模块级导入 `service`，避免环。

    `SqlSearchBackend.reindex` 在**函数内**导入 `build_wanted_entries`，
    因为 service 在模块级导入 backend。这条用例把方向钉住：谁在模块级依赖谁。
    """
    import inspect

    source = inspect.getsource(backend_module)
    module_level = [
        line
        for line in source.splitlines()
        if line.startswith("from app.contexts.search.service") or line.startswith("import app.contexts.search.service")
    ]
    assert module_level == [], f"backend 在模块级导入了 service：{module_level}"


def test_now_parameter_is_actually_used(owner_workspace, sandbox, make_invoice_pdf):
    """契约的 `now` 参数是真在用的：索引行的 `updated_at` 来自调用方传的时间。

    用真实上传产生的文档/版本 ID——`search_entries` 对它们有外键，
    假 ID 会在插入时被拒（这条用例第一版就是那样红的）。
    """
    from app.models.document import DocumentVersion

    job_id = _upload(owner_workspace, make_invoice_pdf())
    with SessionLocal() as db:
        version = db.scalar(
            select(DocumentVersion).where(DocumentVersion.job_id == job_id)
        )
        assert version is not None
        fixed = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        SqlSearchBackend().reindex(
            db,
            tenant_id=owner_workspace["tenant_id"],
            document_id=version.document_id,
            document_version_id=version.id,
            filename="契约.pdf",
            text="契约文本",
            values={"发票号码": SERIAL},
            now=fixed,
        )
        db.commit()
        row = db.scalar(
            select(SearchEntry).where(
                SearchEntry.document_version_id == version.id,
                SearchEntry.kind == "filename",
            )
        )
    assert row is not None and row.content == "契约.pdf"
    stored = row.updated_at
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    assert stored == fixed, f"updated_at 不是调用方传的时间：{stored}"