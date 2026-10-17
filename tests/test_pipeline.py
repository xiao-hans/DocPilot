"""内存任务流水线测试：覆盖单文件/多文件/故障注入/状态流转/结果持久化。

所有测试零网络依赖、确定性、使用 tmp_path 隔离。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pymupdf

from app.models.job import Job, JobStatus, Progress
from app.models.schema import FieldDefinition, validate_fields
from app.services.extraction import ExtractionError, ExtractResult
from app.services.extraction.base import ExtractorBase
from app.services.job_manager import JobManager
from app.services.parsing import CorruptedFileError, ParseResult
from app.services.parsing.dispatcher import ParserDispatcher
from app.services.pipeline import DocumentPipeline

# ────────────────────────────────────────────
# Helpers: 生成 PDF、构造假对象
# ────────────────────────────────────────────

# 发票模板字段（来自 app/models/schema.py INVOICE_TEMPLATE）
INVOICE_FIELDS = validate_fields(
    [
        {"name": "发票号码", "type": "text", "description": "发票右上角的发票号码（20 位数字）"},
        {"name": "开票日期", "type": "date", "description": "发票上的开票日期"},
        {"name": "销售方名称", "type": "text", "description": "销售方全称"},
        {"name": "购买方名称", "type": "text", "description": "购买方全称"},
        {"name": "价税合计", "type": "number", "description": "发票价税合计"},
        {"name": "税额", "type": "number", "description": "合计税额"},
    ]
)


def _make_invoice_text(
    invoice_no: str = "01100200011100045239",
    date: str = "2025-03-15",
    seller: str = "恒信达科技有限公司",
    buyer: str = "北京创想信息技术有限公司",
    total: str = "¥12,800.50",
    tax: str = "¥735.50",
) -> str:
    """构造一段发票文本，确保 MockExtractor 能命中各字段。"""
    return (
        f"增值税电子普通发票\n"
        f"发票号码：{invoice_no}\n"
        f"开票日期：{date}\n"
        f"销售方名称：{seller}\n"
        f"购买方名称：{buyer}\n"
        f"价税合计：{total}\n"
        f"税额：{tax}\n"
    )


def make_text_pdf(path: Path, text: str = "", pages: int = 1) -> None:
    """用 pymupdf 生成含指定文本的 PDF。"""
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), text or f"Page {i + 1} content")
    doc.save(path)
    doc.close()


def _make_invoice_pdf(
    path: Path,
    invoice_no: str = "01100200011100045239",
    date: str = "2025-03-15",
    seller: str = "恒信达科技有限公司",
    buyer: str = "北京创想信息技术有限公司",
    total: str = "¥12,800.50",
    tax: str = "¥735.50",
) -> None:
    """生成一份包含发票文本的真实 PDF。"""
    text = _make_invoice_text(invoice_no, date, seller, buyer, total, tax)
    make_text_pdf(path, text=text)


# ────────────────────────────────────────────
# 假对象：Dispatcher / Extractor
# ────────────────────────────────────────────


class FakeDispatcher(ParserDispatcher):
    """可控的假解析器：返回预设结果或抛异常，记录调用次数。"""

    def __init__(
        self,
        results: list[ParseResult | Exception] | None = None,
        default_result: ParseResult | None = None,
    ) -> None:
        super().__init__(ocr=None)
        self._results = list(results) if results is not None else []
        self._default = default_result or ParseResult(text="default text", pages=1)
        self.call_count = 0
        self.call_paths: list[Path] = []
        # 记录每次被调用时的 job.progress（外部可在 run 前设置 _progress_at_call）
        self._progress_at_call: list[Any] = []

    def set_progress_ref(self, progress_ref: list[Any]) -> None:
        """传入一个可变列表，parse 被调用时把 job.progress 追加进去。"""
        self._progress_at_call = progress_ref

    def parse(self, path: Path) -> ParseResult:  # type: ignore[override]
        self.call_count += 1
        self.call_paths.append(path)
        if self._progress_at_call is not None and len(self._progress_at_call) > 0:
            # 记录调用前的最后一个 progress 值
            self._progress_at_call.append(self._progress_at_call[-1])
        if self._results:
            r = self._results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return self._default


class FakeExtractor(ExtractorBase):
    """可控的假提取器。"""

    def __init__(
        self,
        results: list[ExtractResult | Exception] | None = None,
        default_result: ExtractResult | None = None,
    ) -> None:
        super().__init__()
        self._results = list(results) if results is not None else []
        self._default = default_result or ExtractResult(
            values={}, warnings=[]
        )
        self.call_count = 0

    def extract(
        self, text: str, fields: list[FieldDefinition], metadata: dict | None = None
    ) -> ExtractResult:
        self.call_count += 1
        if self._results:
            r = self._results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return self._default


class RecordingExtractor(ExtractorBase):
    """记录每次 extract 调用的参数，始终返回默认值。"""

    def __init__(self, default_values: dict | None = None) -> None:
        super().__init__()
        self.call_count = 0
        self.calls: list[dict] = []
        self._default_values = default_values or {}

    def extract(
        self, text: str, fields: list[FieldDefinition], metadata: dict | None = None
    ) -> ExtractResult:
        self.call_count += 1
        self.calls.append(
            {"text": text, "fields": fields, "metadata": metadata}
        )
        return ExtractResult(
            values={f.name: self._default_values.get(f.name, "") for f in fields},
            warnings=[],
        )


# ────────────────────────────────────────────
# 类别 1：单文件成功流程
# ────────────────────────────────────────────


def test_single_file_success(tmp_path: Path):
    """真实 PDF → create_job → process → SUCCEEDED + DONE + 正确 result/FileInfo。"""
    pdf = tmp_path / "invoice.pdf"
    _make_invoice_pdf(pdf)

    pipeline = DocumentPipeline(dispatcher=None, extractor=None)
    mgr = JobManager(pipeline=pipeline)

    job = mgr.create_job(
        files=[("invoice.pdf", str(pdf))],
        fields=INVOICE_FIELDS,
    )
    job = mgr.process(job.id)

    # 状态
    assert job.status == JobStatus.SUCCEEDED
    assert job.progress == Progress.DONE

    # result：恰好 1 行
    assert job.result is not None
    assert len(job.result) == 1
    row = job.result[0]
    assert "file" in row
    assert "values" in row
    assert "warnings" in row

    # values 含真实提取值
    values = row["values"]
    invoice_no = values.get("发票号码", "")
    assert len(invoice_no) == 20 and invoice_no.isdigit(), f"发票号码应为 20 位数字，实际: {invoice_no}"
    total = values.get("价税合计")
    assert total is not None, "价税合计不应为空"
    assert isinstance(total, (int, float)), f"价税合计应为数字，实际: {type(total)}"

    # FileInfo 回填
    finfo = job.files[0]
    assert finfo.page_count >= 1
    assert finfo.chars > 0
    assert isinstance(finfo.ocr_applied, bool)
    assert finfo.error is None


# ────────────────────────────────────────────
# 类别 2：多文件批量流程
# ────────────────────────────────────────────


def test_multi_file_batch(tmp_path: Path):
    """3 个不同 PDF → result 行数=3，每行 values 对应各自文件内容。"""
    specs = [
        ("inv_a.pdf", "01100200011100045239", "¥1,000.00"),
        ("inv_b.pdf", "01100200011100045240", "¥2,500.50"),
        ("inv_c.pdf", "01100200011100045241", "¥999.99"),
    ]
    files = []
    for name, inv_no, total in specs:
        pdf = tmp_path / name
        _make_invoice_pdf(pdf, invoice_no=inv_no, total=total)
        files.append((name, str(pdf)))

    pipeline = DocumentPipeline(dispatcher=None, extractor=None)
    mgr = JobManager(pipeline=pipeline)

    job = mgr.create_job(files=files, fields=INVOICE_FIELDS)
    job = mgr.process(job.id)

    assert job.status == JobStatus.SUCCEEDED
    assert job.progress == Progress.DONE
    assert job.result is not None
    assert len(job.result) == len(specs), f"期望 {len(specs)} 行结果，实际 {len(job.result)}"

    # 行顺序与入队一致
    for i, (name, inv_no, _) in enumerate(specs):
        row = job.result[i]
        assert row["file"] == name, f"第 {i} 行 file={row['file']}，期望 {name}"
        assert row["values"]["发票号码"] == inv_no


# ────────────────────────────────────────────
# 类别 3：Parser 失败
# ────────────────────────────────────────────


def test_parser_failure_single_file_all_fail(tmp_path: Path):
    """单文件 parser 抛 ParseError → job FAILED + finfo.error + job.error。"""
    pdf = tmp_path / "bad.pdf"
    pdf.write_bytes(b"not a pdf")

    fake_disp = FakeDispatcher(results=[CorruptedFileError("文件损坏")])
    pipeline = DocumentPipeline(dispatcher=fake_disp, extractor=None)
    mgr = JobManager(pipeline=pipeline)

    job = mgr.create_job(files=[("bad.pdf", str(pdf))], fields=INVOICE_FIELDS)
    job = mgr.process(job.id)

    assert job.status == JobStatus.FAILED
    assert job.progress == Progress.DONE
    assert job.files[0].error is not None
    assert "文件损坏" in job.files[0].error
    assert job.error is not None
    assert "1" in job.error  # 全部 1 个文件失败


def test_parser_failure_mixed_partial_success(tmp_path: Path):
    """两文件：一好一坏 → PARTIAL + 好文件结果保留 + 坏文件 finfo.error。"""
    good_pdf = tmp_path / "good.pdf"
    _make_invoice_pdf(good_pdf, invoice_no="01100200011100045239")

    bad_pdf = tmp_path / "bad.pdf"
    bad_pdf.write_bytes(b"garbage")

    fake_disp = FakeDispatcher(
        results=[
            ParseResult(
                text=_make_invoice_text(invoice_no="01100200011100045239"),
                pages=1,
                source_type="pdf_text",
                metadata={"filename": "good.pdf", "page_count": 1},
            ),
            CorruptedFileError("文件损坏"),
        ]
    )
    pipeline = DocumentPipeline(dispatcher=fake_disp, extractor=None)
    mgr = JobManager(pipeline=pipeline)

    job = mgr.create_job(
        files=[("good.pdf", str(good_pdf)), ("bad.pdf", str(bad_pdf))],
        fields=INVOICE_FIELDS,
    )
    job = mgr.process(job.id)

    # 部分成功必须是独立终态，不能冒充完全成功
    assert job.status == JobStatus.PARTIAL
    assert job.progress == Progress.DONE
    assert job.result is not None
    assert len(job.result) == 1  # 只有好文件
    assert job.result[0]["values"]["发票号码"] == "01100200011100045239"
    # 坏文件 finfo.error 记录
    bad_finfo = job.files[1]
    assert bad_finfo.error is not None
    assert "文件损坏" in bad_finfo.error
    # warnings 包含失败记录
    assert any("bad.pdf" in w and "解析失败" in w for w in job.warnings)


# ────────────────────────────────────────────
# 类别 4：Extractor 失败
# ────────────────────────────────────────────


def test_extractor_failure_single_file_all_fail(tmp_path: Path):
    """单文件 extractor 抛 ExtractionError → job FAILED。"""
    pdf = tmp_path / "inv.pdf"
    _make_invoice_pdf(pdf)

    fake_ext = FakeExtractor(results=[ExtractionError("模型调用失败")])
    pipeline = DocumentPipeline(dispatcher=None, extractor=fake_ext)
    mgr = JobManager(pipeline=pipeline)

    job = mgr.create_job(files=[("inv.pdf", str(pdf))], fields=INVOICE_FIELDS)
    job = mgr.process(job.id)

    assert job.status == JobStatus.FAILED
    assert job.progress == Progress.DONE
    assert job.files[0].error is not None
    assert "模型调用失败" in job.files[0].error
    assert job.error is not None
    assert any("提取失败" in w for w in job.warnings)


def test_extractor_failure_mixed_partial_success(tmp_path: Path):
    """两文件：一好一坏（extractor 只对第二个抛错）→ PARTIAL + 好文件结果保留。"""
    pdf_a = tmp_path / "a.pdf"
    _make_invoice_pdf(pdf_a, invoice_no="01100200011100045239", total="¥1,000.00")
    pdf_b = tmp_path / "b.pdf"
    _make_invoice_pdf(pdf_b, invoice_no="01100200011100045240", total="¥2,000.00")

    # 第一次 extract 正常返回，第二次抛错
    good_result = ExtractResult(
        values={
            "发票号码": "01100200011100045239",
            "开票日期": "2025-03-15",
            "销售方名称": "恒信达科技有限公司",
            "购买方名称": "北京创想信息技术有限公司",
            "价税合计": 1000.0,
            "税额": 0.0,
        },
        warnings=[],
    )
    fake_ext = FakeExtractor(
        results=[good_result, ExtractionError("提取服务不可用")]
    )
    pipeline = DocumentPipeline(dispatcher=None, extractor=fake_ext)
    mgr = JobManager(pipeline=pipeline)

    job = mgr.create_job(
        files=[("a.pdf", str(pdf_a)), ("b.pdf", str(pdf_b))],
        fields=INVOICE_FIELDS,
    )
    job = mgr.process(job.id)

    assert job.status == JobStatus.PARTIAL
    assert job.result is not None
    assert len(job.result) == 1
    assert job.result[0]["values"]["发票号码"] == "01100200011100045239"
    # b.pdf 失败
    assert job.files[1].error is not None
    assert "提取服务不可用" in job.files[1].error
    assert any("b.pdf" in w and "提取失败" in w for w in job.warnings)


# ────────────────────────────────────────────
# 类别 5：状态变化
# ────────────────────────────────────────────


def test_progress_transitions(tmp_path: Path):
    """记录处理中 progress 序列：PARSING→EXTRACTING，且有 PROCESSING 状态。"""
    class TrackingDispatcher(ParserDispatcher):
        """每次 parse 前后记录 job 的 progress 和 status。"""

        def __init__(self) -> None:
            super().__init__(ocr=None)
            self.call_count = 0

        def parse(self, path: Path) -> ParseResult:  # type: ignore[override]
            self.call_count += 1
            # 返回简单结果
            return ParseResult(
                text="invoice text 2025-01-01 恒信达科技 1200.50元",
                pages=1,
                source_type="pdf_text",
                metadata={"filename": path.name, "page_count": 1},
            )

    pdf = tmp_path / "track.pdf"
    _make_invoice_pdf(pdf)

    tracking_disp = TrackingDispatcher()
    pipeline = DocumentPipeline(dispatcher=tracking_disp, extractor=None)
    mgr = JobManager(pipeline=pipeline)

    job = mgr.create_job(files=[("track.pdf", str(pdf))], fields=INVOICE_FIELDS)

    # process 前状态
    assert job.status == JobStatus.QUEUED
    assert job.progress == Progress.QUEUED

    # 挂钩进度回调：在 pipeline.run 内部没法直接 hook，
    # 但我们可以在 run 前后断言终态
    job = mgr.process(job.id)

    # 终态断言
    assert job.status == JobStatus.SUCCEEDED
    assert job.progress == Progress.DONE
    assert tracking_disp.call_count == 1


def test_progress_transitions_with_fake(tmp_path: Path):
    """使用 FakeDispatcher 在不同阶段检查 progress。"""
    check_points: list[str] = []

    class InterceptDispatcher(ParserDispatcher):
        """记录 parse 被调用时的 job.progress（通过引用检查）。"""

        def __init__(self, job_ref: list[Job]) -> None:
            super().__init__(ocr=None)
            self._job_ref = job_ref
            self.call_count = 0

        def parse(self, path: Path) -> ParseResult:  # type: ignore[override]
            self.call_count += 1
            if self._job_ref:
                j = self._job_ref[0]
                check_points.append(f"parse_called:{j.progress.value}:{j.status.value}")
            return ParseResult(
                text="invoice text 2025-01-01 恒信达科技 1200.50元",
                pages=1,
                source_type="pdf_text",
                metadata={"filename": path.name, "page_count": 1},
            )

    pdf = tmp_path / "intercept.pdf"
    _make_invoice_pdf(pdf)

    job_ref: list[Job] = []

    class TrackingExtractor(ExtractorBase):
        """记录 extract 被调用时的 job.progress。"""

        def __init__(self) -> None:
            super().__init__()
            self.call_count = 0

        def extract(
            self,
            text: str,
            fields: list[FieldDefinition],
            metadata: dict | None = None,
        ) -> ExtractResult:
            self.call_count += 1
            if job_ref:
                j = job_ref[0]
                check_points.append(f"extract_called:{j.progress.value}:{j.status.value}")
            return ExtractResult(values={f.name: "" for f in fields}, warnings=[])

    disp = InterceptDispatcher(job_ref)
    ext = TrackingExtractor()
    pipeline = DocumentPipeline(dispatcher=disp, extractor=ext)
    mgr = JobManager(pipeline=pipeline)

    job = mgr.create_job(files=[("intercept.pdf", str(pdf))], fields=INVOICE_FIELDS)
    job_ref.append(job)
    job = mgr.process(job.id)

    # 验证 checkpoint
    assert len(check_points) >= 2
    # parse 应在 PARSING + PROCESSING 时被调用
    assert any("parse_called:parsing:processing" in cp for cp in check_points)
    # extract 应在 EXTRACTING + PROCESSING 时被调用
    assert any("extract_called:extracting:processing" in cp for cp in check_points)


def test_idempotent_process(tmp_path: Path):
    """连续两次 process() 第二次直接返回，pipeline.run 只执行一次。"""
    pdf = tmp_path / "idem.pdf"
    _make_invoice_pdf(pdf)

    call_counter = {"run": 0}
    original_run = DocumentPipeline.run

    def counting_run(self: DocumentPipeline, job: Job) -> Job:
        call_counter["run"] += 1
        return original_run(self, job)

    DocumentPipeline.run = counting_run  # type: ignore[assignment]
    try:
        pipeline = DocumentPipeline(dispatcher=None, extractor=None)
        mgr = JobManager(pipeline=pipeline)

        job = mgr.create_job(files=[("idem.pdf", str(pdf))], fields=INVOICE_FIELDS)
        mgr.process(job.id)

        assert call_counter["run"] == 1

        # 第二次 process：job 已非 QUEUED，应直接返回
        mgr.process(job.id)
        assert call_counter["run"] == 1, "第二次 process 不应再调用 pipeline.run"
    finally:
        DocumentPipeline.run = original_run  # type: ignore[assignment]


# ────────────────────────────────────────────
# 类别 6：Job 结果保存
# ────────────────────────────────────────────


def test_job_result_readable_after_process(tmp_path: Path):
    """process 后 job.result 可读，含 file/values/warnings 键。"""
    pdf = tmp_path / "readable.pdf"
    _make_invoice_pdf(pdf)

    pipeline = DocumentPipeline(dispatcher=None, extractor=None)
    mgr = JobManager(pipeline=pipeline)

    job = mgr.create_job(files=[("readable.pdf", str(pdf))], fields=INVOICE_FIELDS)
    job = mgr.process(job.id)

    assert job.result is not None
    for row in job.result:
        assert "file" in row, "result 行缺少 'file' 键"
        assert "values" in row, "result 行缺少 'values' 键"
        assert "warnings" in row, "result 行缺少 'warnings' 键"
        assert isinstance(row["values"], dict)


def test_get_job_returns_same_instance(tmp_path: Path):
    """get_job(id) 取回 process 后的同一 Job 实例。"""
    pdf = tmp_path / "same.pdf"
    _make_invoice_pdf(pdf)

    pipeline = DocumentPipeline(dispatcher=None, extractor=None)
    mgr = JobManager(pipeline=pipeline)

    job = mgr.create_job(files=[("same.pdf", str(pdf))], fields=INVOICE_FIELDS)
    mgr.process(job.id)

    fetched = mgr.get_job(job.id)
    assert fetched is job
    assert fetched.status == JobStatus.SUCCEEDED


def test_list_jobs_reverse_chronological(tmp_path: Path):
    """`list_jobs()` 按创建时间倒序返回。

    **判据只能断言契约承诺的那件事。** 产品承诺的是"创建时间（`created_at`）倒序"，
    同刻度内按 `id` 倒序以保证确定性——`id` 是随机 uuid，与创建顺序无关，
    所以"按创建顺序倒序"不是系统承诺的，断言它必然间歇性红。

    这条用例原来写的是 `returned_ids == list(reversed(ids))`（按创建顺序倒序）。
    M1 把排序键从 `created_at` 改成 `(created_at, id)` 之后，同刻度内的顺序变成
    按 uuid 倒序，旧断言就只剩约 1/6 的偶然通过率；后来 8 次连跑全红。
    同刻度场景的确定性由 `test_list_jobs_order_is_stable_when_created_at_ties` 专门覆盖。

    这里构造**刻度严格递增**的输入，让"倒序"有确定答案；再用非递增断言兜住同刻度情形。
    """
    pipeline = DocumentPipeline(dispatcher=None, extractor=None)
    mgr = JobManager(pipeline=pipeline)

    ids = []
    stamps = []
    for i in range(3):
        pdf = tmp_path / f"order_{i}.pdf"
        make_text_pdf(pdf, text=f"doc {i}")
        job = mgr.create_job(files=[(f"order_{i}.pdf", str(pdf))], fields=INVOICE_FIELDS)
        # 显式错开刻度：被测的是"倒序"，不是本机时钟分辨率
        job.created_at = 1_700_000_000.0 + i
        ids.append(job.id)
        stamps.append(job.created_at)

    jobs = mgr.list_jobs()
    returned_ids = [j.id for j in jobs]
    returned_stamps = [j.created_at for j in jobs]

    # 契约本体：创建时间非递增
    assert returned_stamps == sorted(returned_stamps, reverse=True)
    # 刻度互不相同时，顺序完全确定，可以断言到具体 id
    assert returned_stamps == list(reversed(stamps))
    assert returned_ids == list(reversed(ids))


def test_list_jobs_order_is_stable_when_created_at_ties(tmp_path: Path):
    """同一 `created_at` 刻度上的任务也必须给出确定顺序。

    `Job.now()` 取自 `time.time()`，本机分辨率约 1 毫秒（实测连续 5 次调用同值），
    紧凑循环创建的任务会落在同一刻度上。修复前这里会红：`sorted(..., reverse=True)`
    是稳定排序，同刻度内退化成插入顺序，于是"最新在前"变成"最早在前"。
    判据用固定刻度构造同值场景，不依赖本机时钟快慢。
    """
    pipeline = DocumentPipeline(dispatcher=None, extractor=None)
    mgr = JobManager(pipeline=pipeline)

    pdf = tmp_path / "tie.pdf"
    make_text_pdf(pdf, text="tie")

    fixed = 1_700_000_000.0
    ids = []
    for i in range(3):
        job = mgr.create_job(files=[(f"tie_{i}.pdf", str(pdf))], fields=INVOICE_FIELDS)
        job.created_at = fixed  # 三个任务同刻度
        ids.append(job.id)

    returned = [j.id for j in mgr.list_jobs()]
    assert returned == sorted(ids, reverse=True)
    # 连调两次结果一致：同刻度下不能出现"这次一个顺序、下次另一个顺序"。
    assert [j.id for j in mgr.list_jobs()] == returned


def test_create_job_initial_status_queued(tmp_path: Path):
    pipeline = DocumentPipeline(dispatcher=None, extractor=None)
    mgr = JobManager(pipeline=pipeline)

    pdf = tmp_path / "q.pdf"
    make_text_pdf(pdf, text="hello")

    job = mgr.create_job(files=[("q.pdf", str(pdf))], fields=INVOICE_FIELDS)
    assert job.status == JobStatus.QUEUED
    assert job.progress == Progress.QUEUED
    assert job.result is None
    assert job.error is None


def test_to_dict_no_path_field(tmp_path: Path):
    """to_dict() 不含 path 字段（防泄露），files 含 name/page_count/chars/ocr_applied/error。"""
    pdf = tmp_path / "dict.pdf"
    _make_invoice_pdf(pdf)

    pipeline = DocumentPipeline(dispatcher=None, extractor=None)
    mgr = JobManager(pipeline=pipeline)

    job = mgr.create_job(files=[("dict.pdf", str(pdf))], fields=INVOICE_FIELDS)
    job = mgr.process(job.id)

    d = job.to_dict()
    # 顶层不含 path
    assert "path" not in d

    # files 中的每个 dict 不含 path
    for f in d["files"]:
        assert "path" not in f
        assert "name" in f
        assert "page_count" in f
        assert "chars" in f
        assert "ocr_applied" in f
        assert "error" in f

    # page_count/chars 有值（已回填）
    assert d["files"][0]["page_count"] >= 1
    assert d["files"][0]["chars"] > 0
    assert d["files"][0]["error"] is None


# ────────────────────────────────────────────
# 补充：100 条清理策略
# ────────────────────────────────────────────


def test_list_jobs_keeps_last_100(tmp_path: Path):
    """创建 105 个 job 后 list_jobs() 只返回 100 个，旧的被清理。"""
    pipeline = DocumentPipeline(dispatcher=None, extractor=None)
    mgr = JobManager(pipeline=pipeline)

    pdf = tmp_path / "hundred.pdf"
    make_text_pdf(pdf, text="bulk job")

    all_ids = []
    for i in range(105):
        job = mgr.create_job(
            files=[(f"bulk_{i}.pdf", str(pdf))], fields=INVOICE_FIELDS
        )
        all_ids.append(job.id)

    jobs = mgr.list_jobs()
    assert len(jobs) == 100, f"期望 100 个 job，实际 {len(jobs)}"

    returned_ids = {j.id for j in jobs}
    all_id_set = set(all_ids)
    # 返回的 job 全部来自已创建集合
    assert returned_ids.issubset(all_id_set)
    # 恰好有 5 个被清理
    cleaned = all_id_set - returned_ids
    assert len(cleaned) == 5, f"期望清理 5 个，实际清理了 {len(cleaned)}"
