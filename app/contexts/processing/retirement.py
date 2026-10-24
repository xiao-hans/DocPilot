"""快照退役 R3（回填）与 R4（对账）的共用判定（`docs/20 §3`）。

两件事共用同一套"快照键 → 规范化源"的映射规则，分成两个脚本是为了让"写"与"只读核对"
不会被混用：回填会改数据，对账一行都不改，而 R6 删列的证据只来自对账。

一条实现约束：快照的 `files[]` 带 `storage_key`（`JobRepository._snapshot` 写入），所以逐文件
事实**优先按键关联**——`document_versions.storage_key` 是唯一列，一对一可靠。只有历史行没有
`storage_key` 时才退回按清洗后的文件名关联，那时同名多行才是不可判定的：既不猜也不算通过，
按 `docs/20 §4` 阻塞 R6。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.contexts.processing.domain import JobAttempt
from app.contexts.templates.domain import TemplateVersion
from app.models.document import DocumentVersion
from app.models.persistent_job import PersistentJob


@dataclass
class BackfillReport:
    jobs_scanned: int = 0
    fields_filled: int = 0
    progress_filled: int = 0
    file_facts_filled: int = 0
    warnings_filled: int = 0
    ambiguous_files: int = 0
    snapshot_only_files: int = 0

    @property
    def total_filled(self) -> int:
        return (
            self.fields_filled
            + self.progress_filled
            + self.file_facts_filled
            + self.warnings_filled
        )


@dataclass
class ReconciliationReport:
    """逐键比较的结果。三个列表都只放内部 ID 与键名，不放文件名与提取值原文。"""

    jobs_scanned: int = 0
    drift: list[str] = field(default_factory=list)
    snapshot_only: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.drift or self.snapshot_only or self.ambiguous)


def _files_by_name(rows: list[DocumentVersion]) -> dict[str, list[DocumentVersion]]:
    grouped: dict[str, list[DocumentVersion]] = {}
    for row in rows:
        grouped.setdefault(row.original_filename, []).append(row)
    return grouped


def _pair_files(
    snap_files: list[dict], versions: list[DocumentVersion]
) -> tuple[list[tuple[dict, DocumentVersion]], list[dict], list[dict]]:
    """把快照 `files[]` 与版本行配对，返回 (可配对, 不可判定, 仅存在于快照)。

    `storage_key` 是首选关联键：它在版本行上是唯一列，且快照里本来就带着。按文件名回退
    只服务没有键的历史行，那里同名多行才是真的没有答案。
    """
    by_key = {row.storage_key: row for row in versions}
    by_name = _files_by_name(versions)
    matched: list[tuple[dict, DocumentVersion]] = []
    ambiguous: list[dict] = []
    missing: list[dict] = []
    for entry in snap_files:
        key = entry.get("storage_key") or ""
        if key:
            row = by_key.get(key)
            if row is None:
                missing.append(entry)
            else:
                matched.append((entry, row))
            continue
        rows = by_name.get(entry.get("name"), [])
        if len(rows) == 1:
            matched.append((entry, rows[0]))
        elif len(rows) > 1:
            ambiguous.append(entry)
        else:
            missing.append(entry)
    return matched, ambiguous, missing


def _normalize_error(value: Any) -> str | None:
    # 快照里的"没有错误"可能是 None 也可能是空串；列上只允许 NULL 表示没有错误。
    return value or None


def _iter_jobs(session, tenant_id: str | None):
    statement = select(PersistentJob).order_by(PersistentJob.id)
    if tenant_id is not None:
        statement = statement.where(PersistentJob.tenant_id == tenant_id)
    return session.scalars(statement).all()


def backfill(
    session_factory, *, tenant_id: str | None = None, dry_run: bool = False
) -> BackfillReport:
    """把历史快照里的四处事实填进规范化列；已有非空值一律跳过，可中断重跑。"""
    report = BackfillReport()
    with session_factory() as session:
        for job in _iter_jobs(session, tenant_id):
            report.jobs_scanned += 1
            snapshot = job.snapshot or {}
            if job.field_definitions is None and snapshot.get("fields") is not None:
                report.fields_filled += 1
                job.field_definitions = snapshot["fields"]
            if job.progress is None and snapshot.get("progress") is not None:
                report.progress_filled += 1
                job.progress = snapshot["progress"]

            versions = session.scalars(
                select(DocumentVersion).where(DocumentVersion.job_id == job.id)
            ).all()
            matched, ambiguous, missing = _pair_files(
                list(snapshot.get("files") or []), list(versions)
            )
            report.ambiguous_files += len(ambiguous)
            report.snapshot_only_files += len(missing)
            for entry, row in matched:
                if row.page_count is None:
                    row.page_count = entry.get("page_count")
                    report.file_facts_filled += 1
                if row.text_chars is None:
                    row.text_chars = entry.get("chars")
                if row.ocr_applied is None:
                    row.ocr_applied = entry.get("ocr_applied")
                if row.parse_error is None:
                    row.parse_error = _normalize_error(entry.get("error"))

            warnings = list(snapshot.get("warnings") or [])
            if warnings:
                attempt = session.scalar(
                    select(JobAttempt).where(
                        JobAttempt.job_id == job.id,
                        JobAttempt.attempt_no == snapshot.get("attempt_count"),
                    )
                )
                if attempt is not None and attempt.warnings is None:
                    attempt.warnings = warnings
                    report.warnings_filled += 1
        if dry_run:
            # 预演：改动只活在会话里，回滚后数据库一行未变，计数仍然可读。
            session.rollback()
        else:
            session.commit()
    return report


def reconcile(session_factory, *, tenant_id: str | None = None) -> ReconciliationReport:
    """用规范化源逐键重建并与快照比对；只读，不改任何一行。"""
    report = ReconciliationReport()
    with session_factory() as session:
        for job in _iter_jobs(session, tenant_id):
            report.jobs_scanned += 1
            snapshot = job.snapshot or {}

            if snapshot.get("fields") is None:
                report.drift.append(f"job={job.id} key=fields missing_in_snapshot")
            elif job.template_version_id is not None:
                frozen = session.get(TemplateVersion, job.template_version_id)
                if frozen is None:
                    report.snapshot_only.append(f"job={job.id} key=fields template_version_gone")
                elif frozen.fields != snapshot["fields"]:
                    report.drift.append(f"job={job.id} key=fields snapshot_vs_template")
                elif job.field_definitions not in (None, snapshot["fields"]):
                    report.drift.append(f"job={job.id} key=fields column_vs_template")
            elif job.field_definitions is None:
                report.drift.append(f"job={job.id} key=fields not_backfilled")
            elif job.field_definitions != snapshot["fields"]:
                report.drift.append(f"job={job.id} key=fields column_vs_snapshot")

            if job.progress is None:
                report.drift.append(f"job={job.id} key=progress not_backfilled")
            elif job.progress != snapshot.get("progress"):
                report.drift.append(f"job={job.id} key=progress column_vs_snapshot")

            versions = list(
                session.scalars(
                    select(DocumentVersion).where(DocumentVersion.job_id == job.id)
                ).all()
            )
            snap_files = list(snapshot.get("files") or [])
            matched, ambiguous, missing = _pair_files(snap_files, versions)
            for entry in ambiguous:
                report.ambiguous.append(f"job={job.id} files={entry.get('name')!r} undecidable_name")
            for entry in missing:
                report.snapshot_only.append(f"job={job.id} files={entry.get('name')!r} no_version_row")
            for entry, row in matched:
                for column, key in (
                    ("page_count", "page_count"),
                    ("text_chars", "chars"),
                    ("ocr_applied", "ocr_applied"),
                ):
                    if getattr(row, column) != entry.get(key):
                        report.drift.append(f"job={job.id} key={column} column_vs_snapshot")
                if row.parse_error != _normalize_error(entry.get("error")):
                    report.drift.append(f"job={job.id} key=parse_error column_vs_snapshot")
            paired_keys = {row.storage_key for _entry, row in matched}
            for extra in sorted(
                row.original_filename
                for row in versions
                if row.storage_key not in paired_keys
            ):
                report.drift.append(f"job={job.id} files={extra!r} row_without_snapshot_entry")

            warnings = list(snapshot.get("warnings") or [])
            attempt = session.scalar(
                select(JobAttempt).where(
                    JobAttempt.job_id == job.id,
                    JobAttempt.attempt_no == snapshot.get("attempt_count"),
                )
            )
            if warnings:
                if attempt is None:
                    report.snapshot_only.append(f"job={job.id} key=warnings no_attempt_row")
                elif attempt.warnings != warnings:
                    report.drift.append(f"job={job.id} key=warnings column_vs_snapshot")
            elif attempt is not None and attempt.warnings not in (None, []):
                report.drift.append(f"job={job.id} key=warnings column_has_warnings_snapshot_empty")

            if snapshot.get("error") and (attempt is None or attempt.error != snapshot["error"]):
                report.drift.append(f"job={job.id} key=error column_vs_snapshot")
    return report
