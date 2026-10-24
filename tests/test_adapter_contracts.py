"""NFR-004 适配器可替换性契约测试（TC-FUNC-060，`docs/06 §5.25`）。

需求原文（`docs/03` NFR-004）：文档解析器、提取器、搜索后端和集成适配器**应**通过接口替换或扩展。

已有用例覆盖的是**构造函数**注入：`DocumentPipeline(dispatcher=..., extractor=...)`、
`ParserDispatcher(FakeOCREngine())`，以及 `ObjectStorage` 两个实现各自跑契约。
生产路径走的却是**工厂与单例**：`get_dispatcher()` / `get_extractor(mode)` /
`build_object_storage()` / `get_object_storage()`。工厂层此前没有任何用例——
这正是"接口能替换"这句话最该被证明的地方：替换点在工厂里失效时，
构造函数层的绿全部成立，生产路径照样不可替换。

本文件在工厂层与类方法层替换实现，然后跑**真实上传接口**，断言替换生效、
链路走通、且没有别处绕过接口直连具体实现。不改产品代码。

## 一个实测到的接缝差异（本文件第一次跑就暴露了）

四类适配器的绑定时机**不一样**：

| 适配器 | 绑定时机 | 替换方式 |
|---|---|---|
| 对象存储 | **每次调用**解析（`build_object_storage()` / `get_object_storage()`） | 换工厂函数即可 |
| 解析器 | **构造时**绑定到 `DocumentPipeline.dispatcher` | 换工厂函数**无效**，要换类方法 |
| 提取器 | **构造时**绑定到 `DocumentPipeline.extractor` | 同上 |

`get_job_manager()` 是进程级单例，它的 `DocumentPipeline` 在首次调用时就
把当时的 `get_dispatcher()` / `get_extractor(EXTRACTOR_MODE)` 结果存了下来。
所以"换掉工厂"这件事对存储立刻生效，对解析与提取**完全不生效**。
这不是缺陷（单例本来就是这个语义），但它意味着：想在生产形态下替换解析器或提取器，
只能换 `EXTRACTOR_MODE` 或替换类方法——**没有一个"注入点"**。
本文件把这件事写成断言，避免它继续只存在于注释里。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.contexts.processing import api as workspace_jobs
from app.contexts.review.domain import DocumentText, ExtractionResult
from app.platform.db import SessionLocal
from app.models.document import DocumentVersion
from app.models.schema import FieldDefinition
from app.contexts.processing import pipeline as pipeline_module
from app.adapters.extraction import get_extractor
from app.adapters.extraction.base import ExtractResult, ExtractorBase
from app.adapters.extraction.mock import MockExtractor
from app.contexts.processing.manager import get_job_manager
from app.adapters.storage.object_storage import (
    LocalStorage,
    ObjectStorage,
    build_object_storage,
    get_object_storage,
)
from app.adapters.parsing.base import SOURCE_PDF_TEXT, ParseResult
from app.adapters.parsing.dispatcher import ParserDispatcher, get_dispatcher

SENTINEL = "契约测试哨兵值"
MARKER = "适配器契约标记-7f3a"
FIELDS_JSON = '[{"name": "发票号码", "type": "text", "description": "号码"}]'


class _RecordingStorage(ObjectStorage):
    """记录所有经接口发生的读写，转发到真实 LocalStorage 落盘。"""

    def __init__(self, inner: ObjectStorage) -> None:
        self._inner = inner
        self.put_keys: list[str] = []
        self.read_keys: list[str] = []
        self.local_path_keys: list[str] = []

    def put(self, key: str, source, *, content_type: str | None = None) -> None:
        self.put_keys.append(key)
        self._inner.put(key, source, content_type=content_type)

    def open_read(self, key: str):
        self.read_keys.append(key)
        return self._inner.open_read(key)

    def delete(self, key: str) -> bool:
        return self._inner.delete(key)

    def stat(self, key: str):
        return self._inner.stat(key)

    def delete_prefix(self, prefix: str) -> int:
        return self._inner.delete_prefix(prefix)

    def list_keys(self, prefix: str = ""):
        return self._inner.list_keys(prefix)

    def local_path(self, key: str):
        self.local_path_keys.append(key)
        return self._inner.local_path(key)


class _Recorder:
    """收集被替换实现收到的调用，供断言使用。"""

    def __init__(self) -> None:
        self.parsed_suffixes: list[str] = []
        self.extracted_texts: list[str] = []


def _install_sentinels(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> None:
    """在**类方法**层替换解析与提取，在**工厂**层替换对象存储。

    M17 之前这里必须在类方法层替换：单例流水线在构造时就绑定了 dispatcher 与
    extractor，换工厂函数对它们无效。M17 把流水线改成每次执行时解析工厂之后，
    工厂层替换也能生效——但类方法层的替换对**已注入**的实例仍然有效，
    所以这里保留类方法层，让判据不受"注入与否"影响。
    """

    def fake_parse(self, path: Path, options: Any = None) -> ParseResult:
        recorder.parsed_suffixes.append(path.suffix)
        result = ParseResult(
            text=f"{MARKER} 来自假解析器", pages=1, source_type=SOURCE_PDF_TEXT
        )
        result.metadata["filename"] = path.name
        result.metadata["page_count"] = 1
        return result

    def fake_extract(self, text, fields, metadata=None) -> ExtractResult:
        recorder.extracted_texts.append(text)
        return ExtractResult(
            values={field.name: f"{SENTINEL}-{field.name}" for field in fields},
            warnings=[],
        )

    monkeypatch.setattr(ParserDispatcher, "parse", fake_parse)
    monkeypatch.setattr(MockExtractor, "extract", fake_extract)


@pytest.fixture()
def adapter_seams(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """替换四类适配器。存储换工厂，解析与提取换类方法。"""
    recorder = _Recorder()
    storage = _RecordingStorage(LocalStorage(tmp_path / "objects"))
    _install_sentinels(monkeypatch, recorder)
    # 写入端（上传接口）与读取端（流水线）分别取工厂，两处都要换。
    monkeypatch.setattr(workspace_jobs, "build_object_storage", lambda *a, **k: storage)
    monkeypatch.setattr(pipeline_module, "get_object_storage", lambda: storage)
    return {"recorder": recorder, "storage": storage}


def _upload(workspace: dict, pdf: Path) -> str:
    response = workspace["client"].post(
        f"/api/v1/tenants/{workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": workspace["csrf"]},
        files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))],
        data={"fields": FIELDS_JSON},
    )
    assert response.status_code == 201, response.text
    return response.json()["job_id"]


def _result_values(workspace: dict, job_id: str) -> dict:
    rows = workspace["client"].get(
        f"/api/v1/tenants/{workspace['tenant_id']}/jobs/{job_id}/result"
    ).json()["result"]
    assert len(rows) == 1
    return rows[0]["values"]


def test_factories_are_the_real_seams(adapter_seams, owner_workspace, sandbox, make_invoice_pdf):
    """经接缝替换适配器后，一次真实上传必须走完全链路。

    这条是 NFR-004 的核心判据：替换点在生产路径上生效，而不只在构造函数里生效。
    """
    job_id = _upload(owner_workspace, make_invoice_pdf())
    job = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{job_id}"
    ).json()
    assert job["status"] == "succeeded", job

    # 解析器：被调用过，且拿到的是对象存储落下来的临时路径（后缀保持 .pdf）
    assert adapter_seams["recorder"].parsed_suffixes == [".pdf"]
    # 提取器：收到的是假解析器产出的文本，不是 PDF 里的真文本
    assert adapter_seams["recorder"].extracted_texts == [f"{MARKER} 来自假解析器"]
    # 存储：写入经接口发生，且键形状仍是 {tenant}/{job}/{uuid}.pdf
    assert len(adapter_seams["storage"].put_keys) == 1
    key = adapter_seams["storage"].put_keys[0]
    assert key.startswith(f"{owner_workspace['tenant_id']}/{job_id}/")
    assert key.endswith(".pdf")


def test_replaced_adapters_reach_the_persisted_result(
    adapter_seams, owner_workspace, sandbox, make_invoice_pdf
):
    """替换生效要一路走到落库的结果与文本，而不是只走到流水线内存里。"""
    job_id = _upload(owner_workspace, make_invoice_pdf())
    assert _result_values(owner_workspace, job_id)["发票号码"] == f"{SENTINEL}-发票号码"

    with SessionLocal() as db:
        result = db.execute(
            select(ExtractionResult).where(ExtractionResult.job_id == job_id)
        ).scalar_one()
        assert result.values["发票号码"] == f"{SENTINEL}-发票号码"
        # 假解析器的文本必须落进 document_texts：证明文本也来自被替换的解析器
        text_row = db.execute(
            select(DocumentText)
            .join(DocumentVersion, DocumentVersion.id == DocumentText.document_version_id)
            .where(DocumentVersion.job_id == job_id)
        ).scalar_one()
        assert text_row.text == f"{MARKER} 来自假解析器"


def test_reading_path_uses_the_factory_not_the_concrete_class(
    adapter_seams, owner_workspace, sandbox, make_invoice_pdf
):
    """解析读回必须经 `get_object_storage()`，不得绕过接口直连 `LocalStorage`。

    这是"信息隐藏"那条设计检查的可执行版本：如果哪天有人在流水线里直接构造
    `LocalStorage(...)`，读路径就换不掉了，而本用例会红。
    """
    _upload(owner_workspace, make_invoice_pdf())
    assert adapter_seams["storage"].local_path_keys, "解析链路没有经接口取本地路径"
    assert adapter_seams["storage"].read_keys == [], (
        "local_path 应当由 storage 自己实现，不该再走一次 open_read"
    )


def test_upload_and_pipeline_agree_on_the_same_storage_instance(
    adapter_seams, owner_workspace, sandbox, make_invoice_pdf
):
    """写入端与读取端必须是同一个存储实例。

    两者分别经 `build_object_storage()` 与 `get_object_storage()` 取，
    若替换只做一半，对象会写到一个地方、读到另一个地方——本机 local 后端下
    这会表现成"上传成功但解析读不到文件"，而错误信息不会指向替换点。
    """
    job_id = _upload(owner_workspace, make_invoice_pdf())
    assert owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{job_id}"
    ).json()["status"] == "succeeded"
    written = set(adapter_seams["storage"].put_keys)
    read = set(adapter_seams["storage"].local_path_keys)
    assert read == written, f"写入 {written} 与读取 {read} 不是同一批键"


def test_dispatcher_and_extractor_resolve_the_factory_per_run(monkeypatch):
    """解析器与提取器**每次执行时**解析工厂（维护轮 M17 闭合了 M2 记的差异）。

    M2 时这里断言的是"构造时绑定、换工厂无效"——那是当时的事实。M17 把流水线改成
    每次执行时解析工厂，于是"换掉工厂"在生产形态下生效，`docs/06 §5.25` 记的那处差异闭合。
    这条用例现在断言**新的**行为：替换工厂后，单例流水线取到的就是替换后的实现。

    显式注入仍然优先：构造参数传了实现时，工厂不参与。
    """
    from app.adapters.extraction.base import ExtractResult, ExtractorBase
    from app.adapters.parsing.dispatcher import ParserDispatcher

    class _Marker(ExtractorBase):
        def extract(self, text, fields, metadata=None) -> ExtractResult:
            return ExtractResult(values={}, warnings=[])

    marker_dispatcher = ParserDispatcher()
    marker_extractor = _Marker()
    monkeypatch.setattr(pipeline_module, "get_dispatcher", lambda: marker_dispatcher)
    monkeypatch.setattr(pipeline_module, "get_extractor", lambda mode: marker_extractor)
    manager = get_job_manager()
    # 单例的流水线没有显式注入，所以取到的是替换后的实现
    monkeypatch.setattr(manager._pipeline, "_injected_dispatcher", None)
    monkeypatch.setattr(manager._pipeline, "_injected_extractor", None)
    assert manager._pipeline.dispatcher is marker_dispatcher
    assert manager._pipeline.extractor is marker_extractor

    # 显式注入优先：传了实现时工厂不参与
    explicit = ParserDispatcher()
    pipeline = pipeline_module.DocumentPipeline(dispatcher=explicit)
    assert pipeline.dispatcher is explicit


def test_unreplaced_factories_still_build_the_defaults():
    """反向：替换撤掉后，工厂回到默认实现——证明上面的用例测的是替换而不是恒真。

    没有这条，那些用例在"工厂永远返回假实现"的情况下也会绿。
    """
    assert isinstance(get_dispatcher(), ParserDispatcher)
    assert get_extractor("mock").__class__.__name__ == "MockExtractor"
    assert isinstance(build_object_storage(), ObjectStorage)
    assert get_object_storage() is not None
    # 工厂的拒绝分支同样要保持：未知模式与未知后端都必须显式失败，不静默退化。
    with pytest.raises(ValueError):
        get_extractor("bogus")


def test_search_backend_factory_exists_now(monkeypatch):
    """NFR-004 把"搜索后端"列为可替换项。**维护轮 M16 已补上抽象**（`docs/22`）。

    这条用例从"记录缺口"改成"记录已补"，并按 M2 的教训验工厂**在生产路径上生效**：
    只断言工厂返回了实现证明不了 service 真的用它。搜索后端的完整契约在
    `tests/test_search_backend.py`，这里只钉住"缺口已闭合"这件事。
    """
    from app.contexts.search import backend as search_backend
    from app.contexts.search.backend import SearchBackend, SqlSearchBackend

    assert hasattr(search_backend, "get_search_backend"), (
        "搜索后端工厂消失了：请同步更新 TC-FUNC-060 与 docs/07 NFR-004"
    )
    assert issubclass(SqlSearchBackend, SearchBackend)
    assert isinstance(search_backend.get_search_backend(), SearchBackend)


def test_extractor_factory_swap_reaches_the_production_path(
    owner_workspace, sandbox, make_invoice_pdf, monkeypatch: pytest.MonkeyPatch
):
    """**只换工厂函数**，真实上传的结果必须来自替换后的提取器（维护轮 M17）。

    这是 M2 缺的那条判据。M2 时它不可能成立——单例流水线构造时就把工厂结果存了下来，
    所以那时的契约测试只能在类方法层替换，而"接口可替换"这句话最该被证明的地方
    正是工厂层。M17 闭合后，这条用例才写得出。
    """
    from app.contexts.processing.manager import get_job_manager

    sentinel = "工厂替换生效标记"

    class _FactoryOnly(ExtractorBase):
        def extract(self, text, fields, metadata=None) -> ExtractResult:
            return ExtractResult(values={field.name: sentinel for field in fields}, warnings=[])

    monkeypatch.setattr(pipeline_module, "get_extractor", lambda mode: _FactoryOnly())
    monkeypatch.setattr(get_job_manager()._pipeline, "_injected_extractor", None)

    job_id = _upload(owner_workspace, make_invoice_pdf())
    values = _result_values(owner_workspace, job_id)
    assert set(values.values()) == {sentinel}, f"结果不是来自工厂替换的提取器：{values}"


def test_object_storage_contract_is_satisfied_by_both_implementations(tmp_path: Path):
    """两个后端实现必须同时满足 `ObjectStorage` 的抽象方法集。

    抽象基类的"实现完整性"由 Python 在实例化时检查，这条把它显式写成判据：
    新增一个抽象方法而某个后端没跟上时，这里会红，而不是等到线上调用才报
    `TypeError: Can't instantiate abstract class`。
    """
    from app.adapters.storage.object_storage import S3Storage

    required = set(ObjectStorage.__abstractmethods__)
    for cls in (LocalStorage, S3Storage):
        own = {
            name
            for name in required
            if name in cls.__dict__ or getattr(cls, name, None) is not getattr(ObjectStorage, name, None)
        }
        assert own == required, f"{cls.__name__} 未实现 {sorted(required - own)}"

    storage = LocalStorage(tmp_path / "c")
    assert isinstance(storage, ObjectStorage)
    # 契约的可观察行为：写→存在→大小→读回→删→不再存在
    storage.put("t/j/a.pdf", io.BytesIO(b"hello"), content_type="application/pdf")
    assert storage.stat("t/j/a.pdf").exists is True
    assert storage.stat("t/j/a.pdf").size == 5
    assert storage.read_bytes("t/j/a.pdf") == b"hello"
    assert storage.delete("t/j/a.pdf") is True
    assert storage.stat("t/j/a.pdf").exists is False
    assert storage.delete("t/j/a.pdf") is False