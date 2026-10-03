"""对象存储抽象（docs/17 §5）。

调用方只传相对对象键（`{tenant}/{job}/{uuid}.ext`），不自行拼接本机路径，也不假设
对象可通过文件系统路径访问：S3 适配器的读取结果是流，没有 `Path`。元信息里刻意不含
绝对路径，避免它被写进日志、审计或响应。
"""

from __future__ import annotations

import shutil
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from app import config as app_config
from app.services.document_storage import resolve_storage_path, validate_object_key


@dataclass(frozen=True)
class ObjectInfo:
    exists: bool
    size: int | None = None


class StorageError(RuntimeError):
    """后端故障的统一出口。

    S3 客户端抛的是 botocore 异常，调用方（上传接口按异常类型决定要不要回收对象）
    不应该为此依赖某个具体后端的异常层次。
    """


class ObjectStorage(ABC):
    """最小对象存储契约：写入流、读取流、删除、存在性与大小。"""

    @abstractmethod
    def put(self, key: str, source: BinaryIO, *, content_type: str | None = None) -> None:
        """把可读流写入对象键；同名键按覆盖语义处理。"""

    @abstractmethod
    def open_read(self, key: str) -> BinaryIO:
        """以二进制只读流打开对象；对象不存在时抛 `FileNotFoundError`。"""

    @abstractmethod
    def delete(self, key: str) -> bool:
        """删除对象；返回是否确实删掉了一个对象，不存在时返回 False。"""

    @abstractmethod
    def stat(self, key: str) -> ObjectInfo:
        """查询存在性与字节数，不返回任何本机路径。"""

    @abstractmethod
    def delete_prefix(self, prefix: str) -> int:
        """删除某个前缀下的全部对象并返回删除数量，用于按任务回收已写入的对象。"""

    @abstractmethod
    def list_keys(self, prefix: str = "") -> Iterator[str]:
        """惰性列出前缀下的相对对象键，供只读审计使用；不返回本机路径。"""

    def read_bytes(self, key: str) -> bytes:
        with self.open_read(key) as stream:
            return stream.read()

    @contextmanager
    def local_path(self, key: str) -> Iterator[Path]:
        """给需要文件路径的解析器（PyMuPDF、RapidOCR）一个可读的本地路径。

        本地实现直接返回真实路径；对象存储实现下载到临时文件并在退出时删除。
        调用方不得把这里得到的路径写进数据库或响应。
        """
        with tempfile.TemporaryDirectory(prefix="docpilot-object-") as tmp:
            suffix = Path(key).suffix
            target = Path(tmp) / (f"object{suffix}" if suffix else "object")
            with self.open_read(key) as source, target.open("wb") as handle:
                while chunk := source.read(1024 * 1024):
                    handle.write(chunk)
            yield target


class LocalStorage(ObjectStorage):
    """本机目录实现：保持阶段 F 之前的行为，同时把路径解析收敛到一处。"""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def _path(self, key: str) -> Path:
        return resolve_storage_path(key, self._root)

    def put(self, key: str, source: BinaryIO, *, content_type: str | None = None) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            while chunk := source.read(64 * 1024):
                handle.write(chunk)

    def open_read(self, key: str) -> BinaryIO:
        return self._path(key).open("rb")

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def stat(self, key: str) -> ObjectInfo:
        path = self._path(key)
        if not path.is_file():
            return ObjectInfo(exists=False)
        return ObjectInfo(exists=True, size=path.stat().st_size)

    def delete_prefix(self, prefix: str) -> int:
        base = self._path(prefix)
        if base.is_file():
            base.unlink()
            self._prune_emptied(base.parent)
            return 1
        if not base.is_dir():
            return 0
        removed = sum(1 for path in base.rglob("*") if path.is_file())
        shutil.rmtree(base)
        self._prune_emptied(base.parent)
        return removed

    def list_keys(self, prefix: str = "") -> Iterator[str]:
        root = self._root.resolve()
        base = root if not prefix else self._path(prefix)
        if base.is_file():
            yield base.relative_to(root).as_posix()
            return
        if not base.is_dir():
            return
        for path in sorted(base.rglob("*")):
            if path.is_file():
                yield path.resolve().relative_to(root).as_posix()

    def _prune_emptied(self, directory: Path) -> None:
        """收回空的上级目录：补偿之后不该留下没有对象的目录脚手架。"""
        root = self._root.resolve()
        current = directory
        while current.resolve() != root:
            try:
                # 目录非空（并发上传刚建好兄弟任务目录）时停下，不影响正确性。
                current.rmdir()
            except OSError:
                return
            current = current.parent

    @contextmanager
    def local_path(self, key: str) -> Iterator[Path]:
        path = self._path(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        yield path


class S3Storage(ObjectStorage):
    """S3 兼容端点实现。

    本机开发指向 SeaweedFS 的 S3 端口，仅作为模拟器，不构成生产存储选型（docs/17 §3）；
    所有 S3 调用都必须经过本适配器与 `tests/test_object_storage.py` 的兼容性验证。
    """

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str,
        region: str = "us-east-1",
        access_key: str = "",
        secret_key: str = "",
        prefix: str = "",
    ) -> None:
        try:
            import boto3
        except ImportError as exc:  # 依赖缺失要让使用者知道，而不是让整站在导入期崩掉
            raise RuntimeError("S3 存储适配器需要 boto3：pip install boto3") from exc
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
        )

    def _key(self, key: str) -> str:
        return self._prefixed("/".join(validate_object_key(key).parts))

    def put(self, key: str, source: BinaryIO, *, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else None
        try:
            self._client.upload_fileobj(source, self._bucket, self._key(key), ExtraArgs=extra)
        except _client_errors() as exc:
            raise StorageError("对象写入失败") from exc

    def open_read(self, key: str) -> BinaryIO:
        try:
            return self._client.get_object(Bucket=self._bucket, Key=self._key(key))["Body"]
        except _client_errors() as exc:
            if _is_missing(exc):
                raise FileNotFoundError(key) from exc
            raise StorageError("对象读取失败") from exc

    def delete(self, key: str) -> bool:
        if not self.stat(key).exists:
            return False
        try:
            self._client.delete_object(Bucket=self._bucket, Key=self._key(key))
        except _client_errors() as exc:
            raise StorageError("对象删除失败") from exc
        return True

    def stat(self, key: str) -> ObjectInfo:
        try:
            head = self._client.head_object(Bucket=self._bucket, Key=self._key(key))
        except _client_errors() as exc:
            if _is_missing(exc):
                return ObjectInfo(exists=False)
            raise StorageError("对象元信息查询失败") from exc
        return ObjectInfo(exists=True, size=int(head["ContentLength"]))

    def delete_prefix(self, prefix: str) -> int:
        object_prefix = self._key(prefix) + "/"
        removed = 0
        try:
            for page in self._client.get_paginator("list_objects_v2").paginate(
                Bucket=self._bucket, Prefix=object_prefix
            ):
                keys = [item["Key"] for item in page.get("Contents", [])]
                if keys:
                    self._client.delete_objects(
                        Bucket=self._bucket,
                        Delete={
                            "Objects": [{"Key": key} for key in keys],
                            "Quiet": True,
                        },
                    )
                    removed += len(keys)
        except _client_errors() as exc:
            raise StorageError("对象前缀清理失败") from exc
        return removed


    def _prefixed(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    def list_keys(self, prefix: str = "") -> Iterator[str]:
        # 返回的键始终相对存储根，与 `LocalStorage.list_keys` 一致：调用方拿到的键要能
        # 直接回填成 `storage_key`，不能因为查询前缀而少一截。
        root_prefix = f"{self._prefix}/" if self._prefix else ""
        object_prefix = (self._key(prefix) + "/") if prefix else root_prefix
        strip = len(root_prefix)
        try:
            pages = self._client.get_paginator("list_objects_v2").paginate(
                Bucket=self._bucket, Prefix=object_prefix
            )
            for page in pages:
                for item in page.get("Contents", []):
                    object_key = item["Key"]
                    if object_key.endswith("/"):
                        continue  # 某些实现用零字节"/"结尾键当目录占位
                    yield object_key[strip:]
        except _client_errors() as exc:
            raise StorageError("对象列举失败") from exc


def _client_errors():
    """延迟取 botocore 的异常类型：未安装 boto3 时导入本模块不应该连带失败。"""
    from botocore.exceptions import ClientError

    return ClientError


def _is_missing(exc) -> bool:
    return exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}


_s3_cached: tuple[tuple[str, ...], S3Storage] | None = None


def _s3_storage() -> S3Storage:
    endpoint = app_config.S3_ENDPOINT_URL
    bucket = app_config.S3_BUCKET
    if not endpoint or not bucket:
        raise RuntimeError("STORAGE_BACKEND=s3 需要同时设置 S3_ENDPOINT_URL 与 S3_BUCKET")
    spec = (
        bucket,
        endpoint,
        app_config.S3_REGION,
        app_config.S3_ACCESS_KEY,
        app_config.S3_SECRET_KEY,
        app_config.S3_KEY_PREFIX,
    )
    global _s3_cached
    if _s3_cached is not None and _s3_cached[0] == spec:
        return _s3_cached[1]
    # 只留一个实例：配置是进程级的，同时存在两个 S3 目标说明有人在测试里改了配置，
    # 继续缓存旧实例会让改配置的一方读到旧端点。
    storage = S3Storage(
        bucket=bucket,
        endpoint_url=endpoint,
        region=app_config.S3_REGION,
        access_key=app_config.S3_ACCESS_KEY,
        secret_key=app_config.S3_SECRET_KEY,
        prefix=app_config.S3_KEY_PREFIX,
    )
    _s3_cached = (spec, storage)
    return storage


def build_object_storage(root: Path | None = None) -> ObjectStorage:
    """按当前配置构造存储实例。

    后端与根目录都在调用时从 `app.config` 读取，而不是导入期固定：写入端与读取端必须
    看到同一个根目录，否则测试隔离了上传目录后，Worker 会去读一个不存在的位置。
    """
    backend = app_config.STORAGE_BACKEND
    if backend == "s3":
        return _s3_storage()
    if backend != "local":
        raise RuntimeError(f"未知的 STORAGE_BACKEND：{backend!r}（可选 local / s3）")
    return LocalStorage(Path(root or app_config.UPLOAD_DIR))


def get_object_storage() -> ObjectStorage:
    return build_object_storage()
