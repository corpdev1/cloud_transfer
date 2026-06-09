import logging
import os

logger = logging.getLogger(__name__)

_MIN_PART_SIZE = 5 * 1024 * 1024  # S3 requires parts >= 5 MB (except last)


def _require_boto3():
    try:
        import boto3
        from boto3.s3.transfer import TransferConfig
        from botocore.config import Config as BotoConfig
        from botocore.exceptions import ClientError
        return boto3, TransferConfig, BotoConfig, ClientError
    except ImportError:
        raise ImportError(
            "S3 support requires boto3.\n"
            "  Fix: pip install boto3"
        )


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


class S3StorageClient:
    """
    S3-compatible storage client. Works with AWS S3, Backblaze B2,
    Wasabi, Cloudflare R2, MinIO, Hetzner Object Storage, and any
    other S3-compatible endpoint.

    Has the same upload_file / key_exists interface as HetznerStorageBoxClient
    so worker.py needs no changes.
    """

    def __init__(
        self,
        bucket: str,
        access_key: str,
        secret_key: str,
        endpoint_url: str = "",
        region: str = "us-east-1",
        prefix: str = "",
        chunk_size_bytes: int = 64 * 1024 * 1024,
    ):
        boto3, TransferConfig, BotoConfig, ClientError = _require_boto3()

        if not bucket:
            raise ValueError("S3 bucket name is required. Set S3_BUCKET in .env")
        if not access_key or not secret_key:
            raise ValueError(
                "S3 credentials are required. Set S3_ACCESS_KEY and S3_SECRET_KEY in .env"
            )

        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._chunk_size = max(chunk_size_bytes, _MIN_PART_SIZE)
        self._ClientError = ClientError

        client_kwargs = dict(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region or "us-east-1",
            config=BotoConfig(
                s3={"addressing_style": "path"},   # required for most non-AWS endpoints
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "adaptive"},
                max_pool_connections=32,
            ),
        )
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url

        self._s3 = boto3.client("s3", **client_kwargs)
        self._transfer_config = TransferConfig(
            multipart_threshold=self._chunk_size,
            multipart_chunksize=self._chunk_size,
            max_concurrency=1,   # file-level parallelism is handled by the caller
            use_threads=False,
        )

    def _remote_key(self, key: str) -> str:
        key = key.strip("/")
        return f"{self._prefix}/{key}" if self._prefix else key

    def key_exists(self, remote_key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self._bucket, Key=self._remote_key(remote_key))
            return True
        except self._ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return False
            raise

    def upload_file(self, local_path: str, remote_key: str, _chunk_size: int = 0):
        key = self._remote_key(remote_key)
        size = os.path.getsize(local_path)
        logger.debug(f"S3 put s3://{self._bucket}/{key} ({_fmt_size(size)})")
        self._s3.upload_file(
            local_path, self._bucket, key,
            Config=self._transfer_config,
        )
