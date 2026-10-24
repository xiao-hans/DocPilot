"""单任务流水线：对任务内每个文件依次解析 → 提取，更新任务状态与结果。"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from app.platform.config import EXTRACTOR_MODE
from app.models.job import FileInfo, Job, JobStatus, Progress
from app.adapters.extraction import ExtractionError, get_extractor
from app.adapters.extraction.base import ExtractorBase
from app.adapters.storage.object_storage import get_object_storage
from app.adapters.parsing import ParseError, get_dispatcher
from app.adapters.parsing.dispatcher import ParserDispatcher


class DocumentPipeline:
    """单任务流水线：对任务内每个文件依次 解析 → 提取，更新任务状态与结果。

    ## 适配器的解析时机（维护轮 M17）

    解析器与提取器**每次执行时**解析工厂，而不是构造时绑定一次。理由是一条实测到的差异
    （`docs/06 §5.25` 记过）：`get_job_manager()` 是进程级单例，它的 `DocumentPipeline`
    在首次调用时就把当时的 `get_dispatcher()` / `get_extractor(EXTRACTOR_MODE)` 结果存了下来，
    于是"换掉工厂"这件事在生产形态下**对解析与提取不生效**——对象存储没有这个问题，
    因为它本来就在每次调用时取。

    这不是性能取舍：解析与提取都是秒级操作（PyMuPDF / RapidOCR / LLM 调用），
    一次工厂调用（读一个模块级常量 + 构造对象）可以忽略。而"能不能换掉实现"
    是 NFR-004 要的能力。

    显式注入（构造参数）仍然优先：测试与需要固定实现的调用方照旧可以传。
    """

    def __init__(
        self,
        dispatcher: ParserDispatcher | None = None,
        extractor: ExtractorBase | None = None,
        on_update: Callable[[Job], None] | None = None,
        on_result: Callable[[Job, FileInfo, object, object], None] | None = None,
    ) -> None:
        # 显式注入存下来；没注入的每次执行时再解析工厂（见类 docstring）。
        self._injected_dispatcher = dispatcher
        self._injected_extractor = extractor
        self.on_update = on_update
        self.on_result = on_result

    @property
    def dispatcher(self) -> ParserDispatcher:
        return self._injected_dispatcher or get_dispatcher()

    @property
    def extractor(self) -> ExtractorBase:
        return self._injected_extractor or get_extractor(EXTRACTOR_MODE)

    def _notify(self, job: Job) -> None:
        if self.on_update is not None:
            self.on_update(job)

    @contextmanager
    def _input_path(self, finfo: FileInfo) -> Iterator[Path]:
        """把待解析文件定位成本机路径。

        解析器（PyMuPDF、RapidOCR）只接受文件路径，所以对象存储要落一次临时文件，退出时
        由上下文管理器删除；这个路径绝不能进数据库、日志或响应。
        遗留的无租户任务没有对象键，只在 `path` 上继续可读（阶段 G4 退役）。
        """
        if finfo.storage_key:
            with get_object_storage().local_path(finfo.storage_key) as path:
                yield path
            return
        if finfo.path:
            yield Path(finfo.path)
            return
        raise ParseError(f"{finfo.name}: 既没有对象键也没有本地路径")

    def run(self, job: Job) -> Job:
        job.status = JobStatus.PROCESSING
        job.result = []
        self._notify(job)

        errors_count = 0

        for finfo in job.files:
            try:
                # --- 解析 ---
                job.progress = Progress.PARSING
                self._notify(job)
                with self._input_path(finfo) as source_path:
                    parse_result = self.dispatcher.parse(source_path)

                # 回填 FileInfo
                finfo.page_count = parse_result.pages
                finfo.chars = len(parse_result.text)
                finfo.ocr_applied = parse_result.source_type != "pdf_text"
                job.warnings.extend(parse_result.warnings)

                # --- 提取 ---
                job.progress = Progress.EXTRACTING
                self._notify(job)
                extract = self.extractor.extract(
                    parse_result.text, job.fields, parse_result.metadata
                )

                # 生成结果行
                row = {
                    "file": finfo.name,
                    "values": extract.values,
                    "warnings": extract.warnings,
                }
                job.result.append(row)
                if self.on_result is not None:
                    self.on_result(job, finfo, parse_result, extract)
                self._notify(job)

            except ParseError as exc:
                finfo.error = str(exc)
                job.warnings.append(f"{finfo.name}: 解析失败 — {exc}")
                errors_count += 1
                self._notify(job)

            except ExtractionError as exc:
                finfo.error = str(exc)
                job.warnings.append(f"{finfo.name}: 提取失败 — {exc}")
                errors_count += 1
                self._notify(job)

            except Exception as exc:
                # 未预期异常（如 OSError）归为单文件失败，保证流水线不中途崩溃
                finfo.error = str(exc)
                job.warnings.append(f"{finfo.name}: 未知错误 — {exc}")
                errors_count += 1
                self._notify(job)

        # 汇总状态：部分文件失败不再冒充完全成功（FR-020 要求可区分部分成功）
        if errors_count == 0:
            job.status = JobStatus.SUCCEEDED
        elif errors_count < len(job.files):
            job.status = JobStatus.PARTIAL
        else:
            job.status = JobStatus.FAILED
            job.error = f"全部 {errors_count} 个文件处理失败"

        job.progress = Progress.DONE
        self._notify(job)
        return job
