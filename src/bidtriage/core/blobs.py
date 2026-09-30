"""SHA-256-addressed blob storage (SPEC-01 technical notes).

Local disk in dev, S3-compatible bucket in production. Addressing by content hash means a
attachment sent to four estimators is stored once.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from bidtriage.core.config import get_settings


def blob_key(sha256: str) -> str:
    """Fan out on the first two bytes so a directory never holds millions of entries."""
    return f"{sha256[:2]}/{sha256[2:4]}/{sha256}"


@runtime_checkable
class BlobStore(Protocol):
    def put(self, sha256: str, data: bytes) -> str: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...


class LocalBlobStore:
    def __init__(self, directory: str | Path) -> None:
        self.root = Path(directory)

    def _path(self, key: str) -> Path:
        return self.root / key

    def put(self, sha256: str, data: bytes) -> str:
        key = blob_key(sha256)
        path = self._path(key)
        if path.exists():
            return key
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return key

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()


class S3BlobStore:
    """S3-compatible store. boto3 is an optional extra (`s3`); imported lazily."""

    def __init__(self, bucket: str, *, endpoint_url: str | None = None, prefix: str = "") -> None:
        import boto3

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._s3 = boto3.client("s3", endpoint_url=endpoint_url or None)

    def _name(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, sha256: str, data: bytes) -> str:
        key = blob_key(sha256)
        if not self.exists(key):
            self._s3.put_object(Bucket=self.bucket, Key=self._name(key), Body=data)
        return key

    def get(self, key: str) -> bytes:
        body: bytes = self._s3.get_object(Bucket=self.bucket, Key=self._name(key))["Body"].read()
        return body

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._s3.head_object(Bucket=self.bucket, Key=self._name(key))
        except ClientError:
            return False
        return True


class NullBlobStore:
    """Drops bytes on the floor; used by tests that only care about metadata."""

    def put(self, sha256: str, data: bytes) -> str:
        return blob_key(sha256)

    def get(self, key: str) -> bytes:
        raise KeyError(key)

    def exists(self, key: str) -> bool:
        return False


def get_blob_store() -> BlobStore:
    s = get_settings()
    if s.blob_bucket:
        return S3BlobStore(s.blob_bucket, endpoint_url=s.blob_endpoint_url, prefix=s.blob_prefix)
    return LocalBlobStore(s.blob_dir)
