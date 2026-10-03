"""处理任务的数据传输模型；持久化由 JobRepository 负责。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from dataclasses import field as dc_field
from enum import Enum

from app.models.schema import FieldDefinition


class JobStatus(str, Enum):
  QUEUED = "queued"
  PROCESSING = "processing"
  SUCCEEDED = "succeeded"
  PARTIAL = "partial"  # 部分文件失败，成功结果保留
  FAILED = "failed"


class Progress(str, Enum):
  QUEUED = "queued"
  PARSING = "parsing"  # 文件解析 / OCR
  EXTRACTING = "extracting"  # AI 提取
  EXCEL = "excel"  # 生成 Excel
  DONE = "done"


@dataclass
class FileInfo:
  """单个输入文件的处理信息。"""

  name: str  # 原始文件名
  storage_key: str = ""  # 对象存储相对键；处理链路据此读取文件
  path: str = ""  # 仅限无租户的遗留进程内入口；不得写入对象存储快照
  page_count: int = 0
  chars: int = 0  # 参与提取的有效文本量
  ocr_applied: bool = False
  error: str | None = None


@dataclass
class Job:
  id: str
  fields: list[FieldDefinition]
  created_at: float
  files: list[FileInfo] = dc_field(default_factory=list)
  status: JobStatus = JobStatus.QUEUED
  progress: Progress = Progress.QUEUED
  result: list[dict] | None = None  # [{file, values: {字段: 值}}]
  warnings: list[str] = dc_field(default_factory=list)
  error: str | None = None
  excel_path: str | None = None
  tenant_id: str | None = None
  owner_id: str | None = None
  template_version_id: str | None = None
  idempotency_key: str | None = None
  attempt_count: int = 0
  finished_at: float | None = None

  @staticmethod
  def new_id() -> str:
    return uuid.uuid4().hex

  @staticmethod
  def now() -> float:
    return time.time()

  def to_dict(self) -> dict:
    """给前端 / API 用的可序列化视图。"""
    return {
      "id": self.id,
      "status": self.status.value,
      "progress": self.progress.value,
      "created_at": self.created_at,
      "files": [
        {
          "name": f.name,
          "page_count": f.page_count,
          "chars": f.chars,
          "ocr_applied": f.ocr_applied,
          "error": f.error,
        }
        for f in self.files
      ],
      "fields": [f.model_dump() for f in self.fields],
      "result": self.result,
      "warnings": self.warnings,
      "error": self.error,
      "template_version_id": self.template_version_id,
      "idempotency_key": self.idempotency_key,
      "attempt_count": self.attempt_count,
      "finished_at": self.finished_at,
    }
