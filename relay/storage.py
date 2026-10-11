"""Object storage for job inputs and outputs. S3 in production, MinIO or moto in dev/tests.

Uploads use presigned POST rather than PUT. A POST policy can cap the size with
content-length-range and pin the content type, which a presigned PUT cannot do.
"""

from typing import Any
from uuid import UUID, uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from relay.config import Settings

ALLOWED_UPLOAD_TYPES = {"image/png", "image/jpeg", "image/webp"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def upload_key(tenant_id: UUID) -> str:
    return f"tenants/{tenant_id}/uploads/{uuid4().hex}"


def output_key(tenant_id: UUID, job_id: UUID, ext: str = "png") -> str:
    return f"tenants/{tenant_id}/outputs/{job_id}.{ext}"


def key_belongs_to(key: str, tenant_id: UUID) -> bool:
    # Tenants can only reference objects under their own prefix.
    return key.startswith(f"tenants/{tenant_id}/") and ".." not in key


class Storage:
    def __init__(self, bucket: str, client: Any) -> None:
        self.bucket = bucket
        self.client = client

    def presign_upload(self, key: str, content_type: str, expires: int) -> dict[str, Any]:
        conditions = [
            ["content-length-range", 1, MAX_UPLOAD_BYTES],
            {"Content-Type": content_type},
        ]
        return self.client.generate_presigned_post(
            Bucket=self.bucket,
            Key=key,
            Fields={"Content-Type": content_type},
            Conditions=conditions,
            ExpiresIn=expires,
        )

    def presign_download(self, key: str, expires: int) -> str:
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires
        )

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True

    def get_bytes(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)


def make_storage(settings: Settings) -> Storage:
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.aws_region,
        config=Config(signature_version="s3v4"),
    )
    return Storage(settings.s3_bucket, client)
