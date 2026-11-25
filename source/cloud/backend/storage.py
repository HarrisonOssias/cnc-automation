"""Part files (STEP models, PDF drawings, images) in S3.

Locally this is MinIO in docker compose; in production, point the same settings at AWS S3.
Two clients: one talks to S3 from inside the cluster, the other only signs download links
for the public hostname that browsers can reach.
"""

import os

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

BUCKET = os.environ.get("S3_BUCKET", "forge-part-files")
REGION = os.environ.get("S3_REGION", "ca-central-1")
_settings = dict(
    region_name=REGION,
    aws_access_key_id=os.environ.get("S3_ACCESS_KEY", "forge"),
    aws_secret_access_key=os.environ.get("S3_SECRET_KEY", "forge-secret"),
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
)
_internal = boto3.client("s3", endpoint_url=os.environ.get("S3_ENDPOINT", "http://localhost:9000"), **_settings)
_public = boto3.client("s3", endpoint_url=os.environ.get("S3_PUBLIC_ENDPOINT", "http://localhost:9000"), **_settings)


def ensure_bucket() -> None:
    try:
        _internal.head_bucket(Bucket=BUCKET)
    except ClientError:
        _internal.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})


def put(key: str, data: bytes, content_type: str) -> None:
    _internal.put_object(Bucket=BUCKET, Key=key, Body=data, ContentType=content_type)


def get(key: str) -> bytes:
    return _internal.get_object(Bucket=BUCKET, Key=key)["Body"].read()


def download_url(key: str, filename: str, expires_s: int = 300) -> str:
    """A short-lived signed link, so the bucket itself stays private."""
    return _public.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": key, "ResponseContentDisposition": f'inline; filename="{filename}"'},
        ExpiresIn=expires_s,
    )
