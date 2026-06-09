import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

# operator.env holds shared infrastructure credentials (Hetzner creds, etc.)
# and is never shown to end users. It overrides .env so users cannot accidentally
# override infrastructure settings with their own .env entries.
load_dotenv()
load_dotenv("operator.env", override=True)


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))


@dataclass
class Config:
    # Google Drive — OAuth (personal / single account)
    oauth_client_file: str = field(default_factory=lambda: _env("GDRIVE_OAUTH_CLIENT_FILE", "credentials/oauth_client.json"))
    oauth_token_file: str = field(default_factory=lambda: _env("GDRIVE_TOKEN_FILE", "credentials/token.json"))
    gdrive_folder_id: str = field(default_factory=lambda: _env("GDRIVE_FOLDER_ID", "root"))

    # Google Drive — Service Account (workspace-wide / super admin)
    # Set GDRIVE_SERVICE_ACCOUNT_FILE to use service account instead of OAuth.
    # Set GDRIVE_IMPERSONATE_USER to impersonate a workspace user (requires domain-wide delegation).
    service_account_file: str = field(default_factory=lambda: _env("GDRIVE_SERVICE_ACCOUNT_FILE", ""))
    impersonate_user: str = field(default_factory=lambda: _env("GDRIVE_IMPERSONATE_USER", ""))

    # Destination — "sftp" (Hetzner Storage Box) or "s3" (any S3-compatible)
    destination: str = field(default_factory=lambda: _env("DESTINATION", "sftp"))

    # Hetzner Storage Box (SFTP) — used when DESTINATION=sftp
    sftp_host: str = field(default_factory=lambda: _env("SFTP_HOST", ""))
    sftp_port: int = field(default_factory=lambda: _env_int("SFTP_PORT", 23))
    sftp_username: str = field(default_factory=lambda: _env("SFTP_USERNAME", ""))
    sftp_password: str = field(default_factory=lambda: _env("SFTP_PASSWORD", ""))
    sftp_base_path: str = field(default_factory=lambda: _env("SFTP_BASE_PATH", "/gdrive"))

    # S3-compatible storage — used when DESTINATION=s3
    # Works with AWS S3, Backblaze B2, Wasabi, Cloudflare R2, MinIO, Hetzner Object Storage, etc.
    s3_bucket: str = field(default_factory=lambda: _env("S3_BUCKET", ""))
    s3_access_key: str = field(default_factory=lambda: _env("S3_ACCESS_KEY", ""))
    s3_secret_key: str = field(default_factory=lambda: _env("S3_SECRET_KEY", ""))
    s3_endpoint_url: str = field(default_factory=lambda: _env("S3_ENDPOINT_URL", ""))   # leave blank for AWS
    s3_region: str = field(default_factory=lambda: _env("S3_REGION", "us-east-1"))
    s3_prefix: str = field(default_factory=lambda: _env("S3_PREFIX", ""))               # optional folder prefix

    # Transfer
    temp_dir: str = field(default_factory=lambda: _env("TEMP_DIR", "/tmp/drivetocloud"))
    parallel_workers: int = field(default_factory=lambda: _env_int("PARALLEL_WORKERS", 4))
    chunk_size_mb: int = field(default_factory=lambda: _env_int("CHUNK_SIZE_MB", 64))
    max_retries: int = field(default_factory=lambda: _env_int("MAX_RETRIES", 3))
    state_db: str = field(default_factory=lambda: _env("STATE_DB", "transfer_state.db"))

    def __post_init__(self):
        errors = []
        if self.parallel_workers < 1:
            errors.append(f"PARALLEL_WORKERS must be >= 1, got {self.parallel_workers}")
        if self.chunk_size_mb < 1:
            errors.append(f"CHUNK_SIZE_MB must be >= 1, got {self.chunk_size_mb}")
        if self.max_retries < 0:
            errors.append(f"MAX_RETRIES must be >= 0, got {self.max_retries}")
        sftp_port = self.sftp_port
        if not (1 <= sftp_port <= 65535):
            errors.append(f"SFTP_PORT must be 1–65535, got {sftp_port}")
        if errors:
            raise ValueError("Invalid configuration:\n" + "\n".join(f"  {e}" for e in errors))

    @property
    def chunk_size_bytes(self) -> int:
        return self.chunk_size_mb * 1024 * 1024
