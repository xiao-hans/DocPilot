"""任务数据模型（v1 内存态，无数据库）。"""

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
  path: str = ""  # 磁盘路径，供 pipeline 读取文件
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

  @staticmethod
  def new_id() -> str:
    return uuid.uuid4().hex[:12]

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
    }
