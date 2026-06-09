#!/usr/bin/env python3
"""
drivetocloud — Transfer Google Drive files to cloud storage.

Commands:
  enumerate    Walk Drive and record all files in the state DB (run first).
  load-csv     Load specific files from GetMega inventory CSVs into the state DB.
  transfer     Transfer pending files to the destination (resumable, parallel).
  status       Show current progress.
  retry-failed Reset failed files back to pending.
"""
import argparse
import csv
import glob
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from config import Config
from gdrive_client import EXPORT_FORMATS, GoogleDriveClient, is_exportable
from s3_client import S3StorageClient
from sftp_client import HetznerStorageBoxClient, _fmt_size
from state import DriveFile, StateManager
from worker import TransferWorker


# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=level,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )
    # Suppress noisy third-party loggers unless verbose
    if not verbose:
        for name in ("googleapiclient", "google.auth", "urllib3", "boto3", "botocore", "s3transfer"):
            logging.getLogger(name).setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _remote_key(path: str, name: str, mime_type: str) -> str:
    """Build the relative remote path, appending the right extension for exported Workspace files."""
    if mime_type in EXPORT_FORMATS:
        _, ext = EXPORT_FORMATS[mime_type]
        if not name.lower().endswith(ext):
            name = name + ext
    # Replace slashes inside the filename itself so they don't create phantom directories.
    name = name.replace("/", "-")
    parts = [p for p in (path, name) if p]
    return "/".join(parts)


def _validate(config: Config, need_drive: bool = True, need_sftp: bool = True):
    errors = []
    if need_drive:
        if config.service_account_file:
            if not os.path.exists(config.service_account_file):
                errors.append(
                    f"  Service account key not found: {config.service_account_file}\n"
                    "  → Go to Google Cloud Console → IAM & Admin → Service Accounts\n"
                    "    → select your SA → Keys → Add Key → JSON, then set\n"
                    "    GDRIVE_SERVICE_ACCOUNT_FILE=path/to/key.json in .env"
                )
        elif not os.path.exists(config.oauth_client_file):
            errors.append(
                f"  OAuth client file not found: {config.oauth_client_file}\n"
                "  → Download from Google Cloud Console → APIs & Services → Credentials\n"
                "    → Create OAuth 2.0 Client ID → Desktop App → Download JSON, then set\n"
                "    GDRIVE_OAUTH_CLIENT_FILE=path/to/file.json in .env\n"
                "  OR: set GDRIVE_SERVICE_ACCOUNT_FILE to use a service account instead."
            )
    if need_sftp:
        if config.destination == "s3":
            for var, val in (("S3_BUCKET",     config.s3_bucket),
                             ("S3_ACCESS_KEY", config.s3_access_key),
                             ("S3_SECRET_KEY", config.s3_secret_key)):
                if not val:
                    errors.append(
                        f"  Missing env var: {var}\n"
                        f"  → Add {var}=<value> to your .env file\n"
                        f"  → Also set S3_ENDPOINT_URL if using a non-AWS provider"
                    )
        else:
            for var, val in (("SFTP_HOST",     config.sftp_host),
                             ("SFTP_USERNAME", config.sftp_username),
                             ("SFTP_PASSWORD", config.sftp_password)):
                if not val:
                    errors.append(
                        f"  Missing env var: {var}\n"
                        f"  → Add {var}=<value> to your .env file"
                    )
    if errors:
        print("\nConfiguration error(s):\n")
        print("\n\n".join(errors))
        print()
        sys.exit(1)


def _make_storage_client(config: Config):
    """Return the right storage backend based on DESTINATION config."""
    if config.destination == "s3":
        return S3StorageClient(
            bucket=config.s3_bucket,
            access_key=config.s3_access_key,
            secret_key=config.s3_secret_key,
            endpoint_url=config.s3_endpoint_url,
            region=config.s3_region,
            prefix=config.s3_prefix,
            chunk_size_bytes=config.chunk_size_bytes,
        )
    return HetznerStorageBoxClient(
        host=config.sftp_host,
        port=config.sftp_port,
        username=config.sftp_username,
        password=config.sftp_password,
        base_path=config.sftp_base_path,
    )


def _make_drive_client(config: Config) -> "GoogleDriveClient":
    """Create a GoogleDriveClient using service account or OAuth depending on config."""
    return GoogleDriveClient(
        oauth_client_file=config.oauth_client_file,
        token_file=config.oauth_token_file,
        service_account_file=config.service_account_file,
        impersonate_user=config.impersonate_user,
    )


def _handle_drive_error(e: Exception, context: str = ""):
    """Print a clear, actionable error message for common Drive API failures."""
    msg = str(e)
    prefix = f"[{context}] " if context else ""

    if "insufficientPermissions" in msg or "Request had insufficient authentication scopes" in msg:
        print(f"\n{prefix}Drive API error: Insufficient permissions.")
        print("  → The authenticated account lacks read access to this resource.")
        if "service account" in msg.lower():
            print("  → Ensure domain-wide delegation is enabled for this service account.")
            print("  → Add the Drive read scope in Google Admin: Security → API Controls → Domain-wide delegation")
        print()
    elif "notFound" in msg or "404" in msg:
        print(f"\n{prefix}Drive API error: Resource not found.")
        print("  → The file, folder, or drive ID may be wrong, or the account has no access.")
        print("  → Run 'python main.py list-drives' to see accessible drives.")
        print()
    elif "userRateLimitExceeded" in msg or "rateLimitExceeded" in msg:
        print(f"\n{prefix}Drive API error: Rate limit hit. The tool will retry automatically.")
        print("  → To avoid this: reduce PARALLEL_WORKERS in .env (default: 4)")
        print()
    elif "invalid_grant" in msg or "Token has been expired" in msg:
        print(f"\n{prefix}Drive auth error: Token expired or revoked.")
        print(f"  → Delete the token file and re-authenticate:")
        print(f"    rm credentials/token.json && python main.py enumerate")
        print()
    elif "Connection refused" in msg or "Name or service not known" in msg or "nodename nor servname" in msg:
        print(f"\n{prefix}Network error: Cannot reach Google Drive API.")
        print("  → Check your internet connection.")
        print()
    else:
        print(f"\n{prefix}Drive error: {e}")
        print()


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_list_drives(args, config: Config):
    """List all shared drives accessible to the authenticated account."""
    _validate(config, need_drive=True, need_sftp=False)
    drive = _make_drive_client(config)
    try:
        drive.authenticate()
    except Exception as e:
        _handle_drive_error(e, "authenticate")
        sys.exit(1)

    try:
        shared = drive.list_shared_drives()
    except Exception as e:
        _handle_drive_error(e, "list-drives")
        sys.exit(1)

    auth_mode = "service account" if config.service_account_file else "OAuth"
    if config.impersonate_user:
        auth_mode += f" (impersonating {config.impersonate_user})"
    print(f"\nAuthenticated via: {auth_mode}")
    print(f"\n{'TYPE':<10}  {'NAME':<40}  ID")
    print("─" * 80)
    print(f"{'My Drive':<10}  {'(personal files)':<40}  my-drive")
    for d in shared:
        print(f"{'Shared':<10}  {d['name']:<40}  {d['id']}")
    print(f"\n{len(shared)} shared drive(s) found.")
    print("\nTo transfer specific drives:")
    print('  python main.py enumerate --drives "Drive Name" "Another Drive"')
    print("  python main.py transfer\n")


def _resolve_drives(drive_client, requested: list[str]) -> list[dict]:
    """Match --drives args (names or IDs) to actual drive objects."""
    if not requested:
        return []
    shared = drive_client.list_shared_drives()
    id_map = {d["id"]: d for d in shared}

    # Build name map, flagging any names that are ambiguous when lowercased.
    name_map: dict[str, dict] = {}
    ambiguous: set[str] = set()
    for d in shared:
        lower = d["name"].lower()
        if lower in name_map:
            ambiguous.add(lower)
        name_map[lower] = d

    resolved = []
    for req in requested:
        if req == "my-drive":
            resolved.append({"id": "my-drive", "name": "My Drive"})
        elif req in id_map:
            resolved.append(id_map[req])
        elif req.lower() in name_map:
            if req.lower() in ambiguous:
                logger.error(
                    f"Drive name '{req}' matches multiple drives (names differ only in case). "
                    f"Use the drive ID instead — run 'list-drives' to see IDs."
                )
                sys.exit(1)
            resolved.append(name_map[req.lower()])
        else:
            logger.error(f"Drive not found: '{req}' — run 'list-drives' to see available drives.")
            sys.exit(1)
    return resolved


def cmd_enumerate(args, config: Config):
    """Walk Drive and populate the state DB with every file."""
    _validate(config, need_drive=True, need_sftp=False)

    drive = _make_drive_client(config)
    try:
        drive.authenticate()
    except Exception as e:
        _handle_drive_error(e, "authenticate")
        sys.exit(1)

    state = StateManager(config.state_db)

    # Build the file iterator — folder, selected drives, or everything
    folder_arg   = getattr(args, "folder", None)
    selected_drives = getattr(args, "drives", None) or []

    if folder_arg:
        # Accept full URL or bare ID
        m = re.search(r'/folders/([a-zA-Z0-9_-]+)', folder_arg)
        folder_id = m.group(1) if m else folder_arg
        logger.info(f"Enumerating folder: {folder_id}")
        file_source = drive.list_folder_files(folder_id)
    elif selected_drives:
        resolved = _resolve_drives(drive, selected_drives)
        logger.info(f"Enumerating {len(resolved)} selected drive(s): {[d['name'] for d in resolved]}")
        def _file_iter():
            for d in resolved:
                if d["id"] == "my-drive":
                    logger.info("  Scanning My Drive ...")
                    yield from drive.list_my_drive_files()
                else:
                    logger.info(f"  Scanning Shared Drive: {d['name']}")
                    yield from drive.list_drive_files(d["id"], d["name"])
        file_source = _file_iter()
    else:
        logger.info("Enumerating all drives (use --drives or --folder to target specific ones)")
        file_source = drive.list_all_files(config.gdrive_folder_id)

    count = 0
    total_bytes = 0
    skipped = []

    seen_keys: dict[str, str] = {}  # remote_key → file_id, for collision detection

    try:
        items = list(file_source)
    except Exception as e:
        _handle_drive_error(e, "enumerate")
        sys.exit(1)

    for item in items:
        size = int(item.get("size") or 0)
        mime = item["mimeType"]
        name = (item.get("name") or "").strip()
        if not name:
            logger.warning(f"Skipping file with empty name (id={item.get('id', '?')})")
            continue
        path = item.get("_path", "")

        if not is_exportable(mime):
            skipped.append({
                "name": name,
                "path": path,
                "mimeType": mime,
                "fileId": item["id"],
                "driveLink": f"https://drive.google.com/file/d/{item['id']}/view",
            })
            continue

        key = _remote_key(path, name, mime)
        if key in seen_keys:
            logger.warning(f"Duplicate remote path '{key}' — {item['id']} collides with {seen_keys[key]}, appending file ID")
            key = key + f"_{item['id']}"
        seen_keys[key] = item["id"]

        state.upsert_file(DriveFile(
            file_id=item["id"],
            name=name,
            path=path,
            size=size,
            mime_type=mime,
            remote_key=key,
        ))
        count += 1
        total_bytes += size

        if count % 500 == 0:
            logger.info(f"  {count:,} files enumerated ({_fmt_size(total_bytes)} so far)...")

    # Write skipped files to JSON so nothing is silently lost.
    skipped_path = "skipped_files.json"
    try:
        with open(skipped_path, "w", encoding="utf-8") as f:
            json.dump(skipped, f, indent=2, ensure_ascii=False)
    except OSError as e:
        logger.warning(f"Could not write {skipped_path}: {e} — skipped file list is not saved")

    logger.info(f"Enumeration complete: {count:,} files queued ({_fmt_size(total_bytes)} total)")
    if skipped:
        logger.info(f"Skipped {len(skipped):,} non-exportable files — see {skipped_path}")
    logger.info(f"Run 'python main.py transfer' to start uploading.")


def _parse_csv_size(s: str) -> int:
    s = s.strip()
    if not s:
        return 0
    parts = s.split()
    if len(parts) != 2:
        return 0
    try:
        num = float(parts[0])
        mult = {'B': 1, 'KB': 1024, 'MB': 1024**2, 'GB': 1024**3, 'TB': 1024**4}.get(parts[1].upper(), 0)
        return int(num * mult)
    except ValueError:
        return 0


def cmd_load_csv(args, config: Config):
    """Load files from GetMega inventory CSVs into the state DB, filtered by bucket/owner."""
    MARKETING_EMAILS = {'utkarsh@getmega', 'ankit.gupta@getmega', 'ajay@getmega'}
    PRODENGG_EMAILS  = {'ankit.gupta@getmega', 'vikas.mishra@getmega'}

    csv_files = sorted(glob.glob(os.path.join(args.csv_dir, '*.csv')))
    if not csv_files:
        print(f"No CSV files found in: {args.csv_dir}")
        sys.exit(1)

    # All 5 CSVs are identical — only read the first one.
    csv_path = csv_files[0]
    logger.info(f"Reading {csv_path} ...")

    state = StateManager(config.state_db)
    count = 0
    total_bytes = 0

    with open(csv_path, encoding='utf-8') as fh:
        for row in csv.DictReader(fh):
            bucket    = row['bucket_name']
            path_sub  = row['Path / Subsection'].strip()
            url       = row['Drive URL'].strip()

            if bucket == 'Marketing':
                if not any(e in path_sub for e in MARKETING_EMAILS):
                    continue
            elif bucket == 'Product & Engineering':
                if not any(e in path_sub for e in PRODENGG_EMAILS):
                    continue
            else:
                continue

            m = re.search(r'/d/([^/\?]+)', url)
            if not m:
                continue
            file_id = m.group(1)

            # Split into directory path + filename
            parts = path_sub.rsplit('/', 1)
            name  = parts[-1]
            path  = parts[0] if len(parts) > 1 else ''

            size = _parse_csv_size(row.get('Size', ''))

            owner = row.get('Owner', '').strip().lower()
            state.upsert_file(DriveFile(
                file_id=file_id,
                name=name,
                path=path,
                size=size,
                mime_type='application/octet-stream',
                remote_key=path_sub,
                owner=owner,
            ))
            count += 1
            total_bytes += size

            if count % 5000 == 0:
                logger.info(f"  {count:,} files loaded ({_fmt_size(total_bytes)} so far)...")

    logger.info(f"Done: {count:,} files ({_fmt_size(total_bytes)}) added to state DB.")
    logger.info("Run 'python main.py transfer' to start uploading.")


def cmd_enumerate_local(args, config: Config):
    """Walk a local directory and queue all files for transfer to Hetzner."""
    import hashlib
    import mimetypes
    src = Path(args.source).resolve()
    if not src.is_dir():
        print(f"Not a directory: {src}")
        sys.exit(1)

    state = StateManager(config.state_db)
    count = 0
    total_bytes = 0

    logger.info(f"Scanning local directory: {src}")
    for local_path in sorted(src.rglob("*", follow_symlinks=False)):
        if not local_path.is_file(follow_symlinks=False):
            continue
        rel = local_path.relative_to(src)
        name = local_path.name
        path = str(rel.parent) if str(rel.parent) != "." else ""
        size = local_path.stat().st_size
        mime = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
        remote_key = str(rel)
        file_id = hashlib.md5(str(local_path).encode()).hexdigest()

        state.upsert_file(DriveFile(
            file_id=file_id,
            name=name,
            path=path,
            size=size,
            mime_type=mime,
            remote_key=remote_key,
            source="local",
            local_path=str(local_path),
        ))
        count += 1
        total_bytes += size
        if count % 1000 == 0:
            logger.info(f"  {count:,} files scanned ({_fmt_size(total_bytes)})...")

    logger.info(f"Done: {count:,} local files ({_fmt_size(total_bytes)}) queued.")
    logger.info("Run 'python main.py transfer' to upload to Hetzner.")


def cmd_transfer(args, config: Config):
    """Transfer all pending files to Hetzner S3 using parallel workers."""
    _validate(config, need_drive=False, need_sftp=True)  # Drive validation deferred — only needed for Drive-source files

    state = StateManager(config.state_db)
    reset_count = state.reset_in_progress()
    if reset_count:
        logger.info(f"Reset {reset_count} previously interrupted transfers back to pending.")

    shard = getattr(args, "shard", None)
    total_shards = getattr(args, "total_shards", None)
    if shard is not None and total_shards is not None:
        if not (0 <= shard < total_shards):
            print(f"Error: --shard must be between 0 and {total_shards - 1}")
            sys.exit(1)

    stats = state.get_stats()
    all_pending = state.get_all_pending(shard=shard, total_shards=total_shards)
    total = len(all_pending)

    if total == 0:
        already_done = stats["done"]["count"]
        shard_info = f" (shard {shard}/{total_shards})" if shard is not None else ""
        logger.info(f"Nothing to transfer{shard_info}. {already_done:,} files already done.")
        if stats["failed"]["count"]:
            logger.info(f"  {stats['failed']['count']:,} failed files — run 'retry-failed' to retry them.")
        return

    shard_info = f" | shard={shard}/{total_shards}" if shard is not None else ""
    logger.info(
        f"Starting transfer: {total:,} files pending "
        f"({_fmt_size(sum(f.size for f in all_pending))}) | "
        f"workers={config.parallel_workers} chunk={config.chunk_size_mb}MB{shard_info}"
    )

    # Local files need no Drive auth — only authenticate if there are Drive-sourced files.
    drive_files = [f for f in all_pending if f.source != "local"]
    owners = sorted({f.owner for f in drive_files if f.owner})
    if drive_files and not owners:
        owners = [""]  # Drive files but no owner info — use default token
    elif not drive_files:
        owners = []    # all local — skip Drive auth entirely

    def _token_file(owner: str) -> str:
        if not owner:
            return config.oauth_token_file
        safe = owner.replace("@", "_at_").replace(".", "_")
        return os.path.join(os.path.dirname(config.oauth_token_file), f"token_{safe}.json")

    # Validate Drive config only if we actually have Drive-source files.
    if drive_files:
        _validate(config, need_drive=True, need_sftp=False)

    # Warm up all owner accounts sequentially (browser prompts happen here).
    owner_clients: dict[str, GoogleDriveClient] = {}
    for owner in owners:
        if config.service_account_file:
            # Service account: single client, optionally impersonating the owner
            impersonate = owner if owner else config.impersonate_user
            client = GoogleDriveClient(
                oauth_client_file=config.oauth_client_file,
                token_file=config.oauth_token_file,
                service_account_file=config.service_account_file,
                impersonate_user=impersonate,
            )
            logger.info(f"Authenticating via service account{f' (impersonating {impersonate})' if impersonate else ''}")
        else:
            tok = _token_file(owner)
            client = GoogleDriveClient(config.oauth_client_file, tok)
            logger.info(f"Authenticating as: {owner or '(default)'} → {tok}")
        try:
            client.authenticate()
        except Exception as e:
            _handle_drive_error(e, f"auth/{owner or 'default'}")
            sys.exit(1)
        owner_clients[owner] = client

    # One shared storage client (SFTP or S3) — thread-safe for concurrent uploads.
    try:
        sftp = _make_storage_client(config)
    except Exception as e:
        print(f"\nStorage client error: {e}\n")
        sys.exit(1)

    # Per-thread, per-owner Drive clients (each thread gets its own authenticated service).
    _thread_local = threading.local()

    def _thread_drive(owner: str) -> GoogleDriveClient:
        if not hasattr(_thread_local, "drives"):
            _thread_local.drives = {}
        if owner not in _thread_local.drives:
            if config.service_account_file:
                impersonate = owner if owner else config.impersonate_user
                c = GoogleDriveClient(
                    oauth_client_file=config.oauth_client_file,
                    token_file=config.oauth_token_file,
                    service_account_file=config.service_account_file,
                    impersonate_user=impersonate,
                )
            else:
                tok = _token_file(owner)
                c = GoogleDriveClient(config.oauth_client_file, tok)
            c.authenticate()
            _thread_local.drives[owner] = c
        return _thread_local.drives[owner]

    def _get_drive(file: DriveFile):
        if file.source == "local":
            return None
        return _thread_drive(file.owner)

    counter_lock = threading.Lock()
    done_n = 0
    fail_n = 0
    transfer_start = time.time()
    bytes_at_start = stats["done"]["bytes"]

    def process(file: DriveFile):
        nonlocal done_n, fail_n

        if not state.claim_file(file.file_id):
            logger.debug(f"Skipping {file.file_id} — already claimed by another worker")
            return

        worker = TransferWorker(config, _get_drive(file), sftp, state)
        ok = worker.transfer(file)

        with counter_lock:
            if ok:
                done_n += 1
            else:
                fail_n += 1
            finished = done_n + fail_n
            if finished % max(1, min(50, total // 20)) == 0 or finished == total:
                pct = finished / total * 100
                s = state.get_stats()
                elapsed = time.time() - transfer_start
                bytes_this_run = s["done"]["bytes"] - bytes_at_start
                rate = bytes_this_run / elapsed if elapsed > 0 else 0
                pending_bytes = s["pending"]["bytes"] + s["in_progress"]["bytes"]
                eta_str = ""
                if rate > 0 and pending_bytes > 0:
                    eta_secs = pending_bytes / rate
                    h, rem = divmod(int(eta_secs), 3600)
                    m = rem // 60
                    eta_str = f" eta={h}h{m:02d}m"
                logger.info(
                    f"Progress: {finished:,}/{total:,} ({pct:.1f}%) | "
                    f"done={s['done']['count']:,} failed={s['failed']['count']:,} "
                    f"transferred={_fmt_size(s['done']['bytes'])} "
                    f"speed={_fmt_size(int(rate))}/s{eta_str}"
                )

    with ThreadPoolExecutor(max_workers=config.parallel_workers) as pool:
        futures = [pool.submit(process, f) for f in all_pending]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                logger.error(f"Unexpected worker error: {e}")

    final = state.get_stats()
    logger.info(
        f"Transfer complete — done: {final['done']['count']:,} "
        f"({_fmt_size(final['done']['bytes'])}), "
        f"failed: {final['failed']['count']:,}"
    )
    if final["failed"]["count"]:
        logger.info("Re-run with 'retry-failed' then 'transfer' to retry failures.")


def cmd_status(args, config: Config):
    """Print a progress table."""
    state = StateManager(config.state_db)
    stats = state.get_stats()

    total_files = sum(v["count"] for v in stats.values())
    total_bytes = sum(v["bytes"] for v in stats.values())
    done_files = stats["done"]["count"]

    print(f"\n{'Status':<15} {'Files':>10} {'Size':>15}")
    print("─" * 42)
    for status in ("done", "in_progress", "pending", "failed"):
        d = stats[status]
        print(f"{status:<15} {d['count']:>10,} {_fmt_size(d['bytes']):>15}")
    print("─" * 42)
    print(f"{'TOTAL':<15} {total_files:>10,} {_fmt_size(total_bytes):>15}")

    if total_files > 0 and done_files > 0:
        pct = done_files / total_files * 100
        print(f"\nCompletion: {pct:.1f}%  ({_fmt_size(stats['done']['bytes'])} of {_fmt_size(total_bytes)} transferred)")
    print()


def cmd_retry(args, config: Config):
    """Reset all failed files back to pending."""
    state = StateManager(config.state_db)
    n = state.retry_failed()
    logger.info(f"Reset {n:,} failed files to pending. Run 'transfer' to retry them.")


def cmd_show_failed(args, config: Config):
    """Print all failed files and their error messages."""
    state = StateManager(config.state_db)
    failed = state.get_failed()
    if not failed:
        print("No failed files.")
        return
    print(f"\n{len(failed):,} failed files:\n")
    for f in failed:
        display = f"{f['path']}/{f['name']}" if f['path'] else f['name']
        error = (f['error'] or 'unknown error')[:120]
        print(f"  [{f['attempts']}x] {display}")
        print(f"         {error}")
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="drivetocloud",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-drives",  help="List all accessible shared drives")
    p_enum = sub.add_parser("enumerate", help="Walk Drive and populate the state DB")
    p_enum.add_argument("--drives", nargs="+", metavar="DRIVE",
                        help='Drive names or IDs to enumerate. Use "my-drive" for personal files.')
    p_enum.add_argument("--folder",
                        help="Enumerate a specific folder by ID or URL (e.g. https://drive.google.com/drive/.../folders/ID)")
    p_csv = sub.add_parser("load-csv", help="Load filtered GetMega inventory files into the state DB")
    p_csv.add_argument("--csv-dir", default="getmega_files", help="Directory containing the inventory CSV files (default: getmega_files)")
    p_local = sub.add_parser("enumerate-local", help="Queue a local directory for transfer to Hetzner")
    p_local.add_argument("source", help="Local directory path to upload")
    p_transfer = sub.add_parser("transfer", help="Transfer pending files to Hetzner S3")
    p_transfer.add_argument("--shard", type=int, default=None, help="Shard index (0-based) for multi-machine splits")
    p_transfer.add_argument("--total-shards", type=int, default=None, help="Total number of shards (e.g. 2)")
    sub.add_parser("status",       help="Show transfer progress")
    sub.add_parser("retry-failed", help="Reset failed files back to pending")
    sub.add_parser("show-failed",  help="Print failed files and their error messages")

    args = parser.parse_args()
    setup_logging(args.verbose)

    config = Config()

    {
        "list-drives":  cmd_list_drives,
        "enumerate":       cmd_enumerate,
        "enumerate-local": cmd_enumerate_local,
        "load-csv":     cmd_load_csv,
        "transfer":     cmd_transfer,
        "status":       cmd_status,
        "retry-failed": cmd_retry,
        "show-failed":  cmd_show_failed,
    }[args.command](args, config)


if __name__ == "__main__":
    main()
