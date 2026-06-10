#!/usr/bin/env python3
"""
trim_drive_folder.py

Scans the getmega Google Drive folder, keeps the top 400 highest-quality
files split proportionally across Marketing and Fin & Legal, and moves
the rest to Trash.

Usage:
    python trim_drive_folder.py --dry-run   # preview only, nothing deleted
    python trim_drive_folder.py             # live run — asks for confirmation
"""
import math
import os
import sys
import time
from pathlib import Path

import google_auth_httplib2
import httplib2
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Config ─────────────────────────────────────────────────────────────────────
TARGET_FOLDER_ID = "1q3jLjeuA9MxCMxUn0_YqYHcbcV-ni9E1"
KEEP_TOTAL       = 400
CLIENT_FILE      = "client_secret_503999698402-vq3ggcnbe146ttht2sou6kaku4970rdn.apps.googleusercontent.com.json"
TOKEN_FILE       = "credentials/upload_token.json"
SCOPES           = ["https://www.googleapis.com/auth/drive"]

# ── Quality scoring ────────────────────────────────────────────────────────────
# Content-rich presentation and document formats score highest.
# Within each tier, larger files score higher (log scale).
TYPE_SCORE = {
    "pdf":     10,
    "pptx":    10,
    "key":     10,
    "docx":     9,
    "xlsx":     9,
    "numbers":  9,
    "pages":    9,
    "doc":      8,
    "xls":      8,
    "ppt":      8,
    "odp":      7,
    "ods":      7,
    "odt":      7,
    "zip":      4,
    "csv":      4,
    "json":     3,
    "xml":      3,
    "png":      3,
    "jpg":      3,
    "jpeg":     3,
    "gif":      2,
    "mp4":      2,
    "mov":      2,
    "txt":      1,
    "md":       1,
}


def _fmt_size(n):
    n = int(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _quality_score(f):
    name = f.get("name", "")
    ext  = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    ts   = TYPE_SCORE.get(ext, 1)
    size = int(f.get("size") or 0)
    return ts * math.log(max(size, 100))


def _categorize(f):
    path = (f.get("_path") or "").lower()
    if "marketing" in path:
        return "marketing"
    if any(k in path for k in ("fin", "legal", "financial", "finance")):
        return "finlegal"
    return "other"


# ── Auth ───────────────────────────────────────────────────────────────────────

def authenticate():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_FILE):
                print(f"ERROR: OAuth client file not found: {CLIENT_FILE}")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        Path(TOKEN_FILE).write_text(creds.to_json())
    http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=60))
    return build("drive", "v3", http=http, cache_discovery=False)


# ── Drive scan ─────────────────────────────────────────────────────────────────

def list_all_files(service, folder_id):
    """Recursively walk folder_id, return list of file dicts with _path set."""
    results = []

    def _walk(fid, path_prefix):
        page_token = None
        while True:
            resp = service.files().list(
                q=f"'{fid}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType, size)",
                pageSize=1000,
                pageToken=page_token,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            ).execute()
            for item in resp.get("files", []):
                if item["mimeType"] == "application/vnd.google-apps.folder":
                    sub = f"{path_prefix}/{item['name']}" if path_prefix else item["name"]
                    _walk(item["id"], sub)
                else:
                    item["_path"] = path_prefix
                    results.append(item)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    _walk(folder_id, "")
    return results


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv

    if dry_run:
        print("\n── DRY RUN ── nothing will be deleted\n")

    print("Authenticating with Google Drive...")
    service = authenticate()

    print(f"Scanning folder {TARGET_FOLDER_ID} ...")
    all_files = list_all_files(service, TARGET_FOLDER_ID)
    print(f"Found {len(all_files):,} files total.\n")

    # Bucket by category
    buckets = {"marketing": [], "finlegal": [], "other": []}
    for f in all_files:
        buckets[_categorize(f)].append(f)

    print(f"  Marketing:   {len(buckets['marketing']):,} files  ({_fmt_size(sum(int(f.get('size') or 0) for f in buckets['marketing']))})")
    print(f"  Fin & Legal: {len(buckets['finlegal']):,} files  ({_fmt_size(sum(int(f.get('size') or 0) for f in buckets['finlegal']))})")
    if buckets["other"]:
        print(f"  Other:       {len(buckets['other']):,} files  (will all be trashed — not in either category)")
    print()

    n_mkt = len(buckets["marketing"])
    n_fin = len(buckets["finlegal"])

    if n_mkt == 0 and n_fin == 0:
        print("ERROR: Could not find files in Marketing or Fin & Legal subfolders.")
        print("       Check that the folder paths contain 'marketing' or 'fin/legal'.")
        sys.exit(1)

    keep_mkt = min(286, n_mkt)
    keep_fin = min(114, n_fin)

    print(f"Proportional split of {KEEP_TOTAL} files to keep:")
    print(f"  Marketing:   {keep_mkt}  (of {n_mkt:,})")
    print(f"  Fin & Legal: {keep_fin}  (of {n_fin:,})")
    print()

    # Rank by quality, split into keep / delete
    def split(files, keep_n):
        ranked = sorted(files, key=_quality_score, reverse=True)
        return ranked[:keep_n], ranked[keep_n:]

    keep_mkt_files, del_mkt = split(buckets["marketing"], keep_mkt)
    keep_fin_files, del_fin = split(buckets["finlegal"],  keep_fin)

    to_keep   = keep_mkt_files + keep_fin_files
    to_delete = del_mkt + del_fin + buckets["other"]

    keep_bytes   = sum(int(f.get("size") or 0) for f in to_keep)
    delete_bytes = sum(int(f.get("size") or 0) for f in to_delete)

    print(f"Will KEEP:   {len(to_keep):,} files  ({_fmt_size(keep_bytes)})")
    print(f"Will TRASH:  {len(to_delete):,} files  ({_fmt_size(delete_bytes)})")
    print()

    # Show top 15 kept files
    print("── Top 15 files being KEPT (highest quality score) ──────────────────")
    for f in sorted(to_keep, key=_quality_score, reverse=True)[:15]:
        cat  = "MKT" if f in keep_mkt_files else "F&L"
        path = f.get("_path", "")
        name = f"{path}/{f['name']}" if path else f["name"]
        print(f"  [{cat}] {_fmt_size(f.get('size') or 0):>10}  {name}")
    print()

    # Show sample of what will be deleted
    print("── Sample of files being TRASHED (lowest quality score, first 15) ───")
    for f in sorted(to_delete, key=_quality_score)[:15]:
        path = f.get("_path", "")
        name = f"{path}/{f['name']}" if path else f["name"]
        print(f"  {_fmt_size(f.get('size') or 0):>10}  {name}")
    if len(to_delete) > 15:
        print(f"  ... and {len(to_delete) - 15:,} more")
    print()

    if dry_run:
        print("DRY RUN complete — re-run without --dry-run to trash the files.")
        return

    # Confirm
    print(f"This will move {len(to_delete):,} files to Google Drive Trash.")
    print("Files stay in Trash until you empty it (recoverable).\n")
    confirm = input("Type  YES  to confirm: ").strip()
    if confirm != "YES":
        print("Aborted — nothing deleted.")
        return

    # Trash
    print(f"\nTrashing {len(to_delete):,} files...")
    ok = fail = 0
    for i, f in enumerate(to_delete, 1):
        for attempt in range(3):
            try:
                service.files().update(
                    fileId=f["id"],
                    body={"trashed": True},
                    supportsAllDrives=True,
                ).execute()
                ok += 1
                break
            except HttpError as e:
                if e.resp.status == 429:
                    time.sleep(2 ** attempt)
                else:
                    print(f"  Failed ({e.resp.status}): {f['name']}")
                    fail += 1
                    break
            except Exception as e:
                print(f"  Failed: {f['name']} — {e}")
                fail += 1
                break
        if i % 100 == 0:
            print(f"  {i:,}/{len(to_delete):,} processed...")
        time.sleep(0.05)  # avoid rate limits

    print(f"\nDone.  Trashed: {ok:,}   Failed: {fail}")
    if ok:
        print("Files are in Google Drive Trash — go to drive.google.com/drive/trash to permanently delete or restore.")


if __name__ == "__main__":
    main()
