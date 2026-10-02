"""单任务流水线：对任务内每个文件依次解析 → 提取，更新任务状态与结果。"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable

from app.config import EXTRACTOR_MODE
from app.models.job import Job, JobStatus, Progress
from app.services.extraction import ExtractionError, get_extractor
from app.services.extraction.base import ExtractorBase
from app.services.parsing import ParseError, get_dispatcher
from app.services.parsing.dispatcher import ParserDispatcher


class DocumentPipeline:
    """单任务流水线：对任务内每个文件依次 解析 → 提取，更新任务状态与结果。"""

    def __init__(
        self,
        dispatcher: ParserDispatcher | None = None,
        extractor: ExtractorBase | None = None,
        on_update: Callable[[Job], None] | None = None,
        on_result: Callable[[Job, FileInfo, object, object], None] | None = None,
    ) -> None:
        self.dispatcher = dispatcher or get_dispatcher()
        self.extractor = extractor or get_extractor(EXTRACTOR_MODE)
        self.on_update = on_update
        self.on_result = on_result

    def _notify(self, job: Job) -> None:
        if self.on_update is not None:
            self.on_update(job)

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
                parse_result = self.dispatcher.parse(Path(finfo.path))

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
