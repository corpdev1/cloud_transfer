import csv
import io
import logging
import os
import socket
import threading
import time
import zipfile
from pathlib import Path
from typing import Iterator

import httplib2
import google_auth_httplib2
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Service account auth (set via config — no import error if library missing)
try:
    from google.oauth2 import service_account as _sa_module
    _SA_AVAILABLE = True
except ImportError:
    _SA_AVAILABLE = False

EXPORT_FORMATS = {
    "application/vnd.google-apps.document":     ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet":  ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",      ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "application/vnd.google-apps.drawing":      ("image/svg+xml",                                                            ".svg"),
    "application/vnd.google-apps.script":       ("application/vnd.google-apps.script+json",                                  ".json"),
}

# Ordered fallback chains for files that exceed export size limits.
# text/csv and text/plain have no hard size cap and serve as last resorts.
EXPORT_FALLBACKS = {
    "application/vnd.google-apps.spreadsheet": [
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
        ("text/csv",        ".csv"),
        ("application/pdf", ".pdf"),
    ],
    "application/vnd.google-apps.document": [
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
        ("text/plain",      ".txt"),
        ("application/pdf", ".pdf"),
    ],
    "application/vnd.google-apps.presentation": [
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
        ("application/pdf", ".pdf"),
    ],
    "application/vnd.google-apps.drawing": [
        ("image/svg+xml",   ".svg"),
        ("image/png",       ".png"),
        ("application/pdf", ".pdf"),
    ],
    "application/vnd.google-apps.script": [
        ("application/vnd.google-apps.script+json", ".json"),
    ],
}

SKIP_MIME_TYPES = {
    "application/vnd.google-apps.folder",
    "application/vnd.google-apps.shortcut",
    "application/vnd.google-apps.unknown",
}


def is_exportable(mime_type: str) -> bool:
    """Return True if the file can be directly downloaded or exported via the Drive API."""
    if not mime_type.startswith("application/vnd.google-apps."):
        return True
    return mime_type in EXPORT_FORMATS

_auth_lock = threading.Lock()


class GoogleDriveClient:
    def __init__(self, oauth_client_file: str, token_file: str,
                 service_account_file: str = "", impersonate_user: str = ""):
        self.oauth_client_file = oauth_client_file
        self.token_file = token_file
        self.service_account_file = service_account_file
        self.impersonate_user = impersonate_user
        self._service = None
        self._sheets_service = None
        self._authorized_http = None

    def authenticate(self):
        if self.service_account_file:
            self._authenticate_service_account()
        else:
            self._authenticate_oauth()

    def _authenticate_service_account(self):
        if not _SA_AVAILABLE:
            raise RuntimeError(
                "Service account auth requires the google-auth library.\n"
                "  Fix: pip install google-auth"
            )
        if not os.path.exists(self.service_account_file):
            raise FileNotFoundError(
                f"Service account key file not found: {self.service_account_file}\n"
                "  Fix: Go to Google Cloud Console → IAM & Admin → Service Accounts\n"
                "       → select your service account → Keys → Add Key → JSON\n"
                "       Then set GDRIVE_SERVICE_ACCOUNT_FILE=path/to/key.json in .env"
            )
        with _auth_lock:
            creds = _sa_module.Credentials.from_service_account_file(
                self.service_account_file, scopes=SCOPES
            )
            if self.impersonate_user:
                creds = creds.with_subject(self.impersonate_user)
                logger.info(f"Service account auth: impersonating {self.impersonate_user}")
            else:
                logger.info("Service account auth (no impersonation)")

        self._authorized_http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=600))
        self._service = build("drive", "v3", http=self._authorized_http, cache_discovery=False)
        logger.info("Google Drive authenticated via service account")

    def _authenticate_oauth(self):
        with _auth_lock:
            creds = None
            if os.path.exists(self.token_file):
                creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    try:
                        creds.refresh(Request())
                    except Exception:
                        creds = None  # force re-auth below
                if not creds or not creds.valid:
                    if not os.path.exists(self.oauth_client_file):
                        raise FileNotFoundError(
                            f"OAuth client file not found: {self.oauth_client_file}\n"
                            "  Fix: Go to Google Cloud Console → APIs & Services → Credentials\n"
                            "       → Create OAuth 2.0 Client ID → Desktop App → Download JSON\n"
                            "       Then set GDRIVE_OAUTH_CLIENT_FILE=path/to/file.json in .env"
                        )
                    flow = InstalledAppFlow.from_client_secrets_file(self.oauth_client_file, SCOPES)
                    creds = flow.run_local_server(port=0)
                Path(self.token_file).parent.mkdir(parents=True, exist_ok=True)
                Path(self.token_file).write_text(creds.to_json())

        self._authorized_http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=600))
        self._service = build("drive", "v3", http=self._authorized_http, cache_discovery=False)
        logger.info("Google Drive authenticated via OAuth")

    @property
    def service(self):
        if self._service is None:
            raise RuntimeError("Call authenticate() before using the client.")
        return self._service

    def list_all_files(self, folder_id: str = "root") -> Iterator[dict]:
        """
        Yield every transferable file from:
          1. My Drive — flat scan (catches orphans, multi-parent files, everything)
          2. Each Shared Drive — recursive walk
        """
        # ── My Drive: flat scan ───────────────────────────────────────────────
        logger.info("Building folder map for My Drive...")
        folder_map = self._build_folder_map()
        logger.info(f"  {len(folder_map):,} folders indexed. Scanning files...")
        yield from self._flat_scan_my_drive(folder_map)

        # ── Shared Drives ─────────────────────────────────────────────────────
        for drive in self._list_shared_drives():
            logger.info(f"Enumerating Shared Drive: {drive['name']}")
            prefix = f"[Shared] {drive['name']}"
            yield from self._walk_shared_drive(drive["id"], prefix)

    # ── My Drive flat scan ────────────────────────────────────────────────────

    def _build_folder_map(self) -> dict:
        """
        Fetch all folders in My Drive and return a map of
        folder_id → {"name": str, "parent": str | None}.
        Used to resolve full paths for any file without recursive API calls.
        """
        folder_map = {}
        page_token = None
        while True:
            resp = self._api_call(
                self.service.files().list(
                    q="mimeType = 'application/vnd.google-apps.folder' and trashed = false",
                    fields="nextPageToken, files(id, name, parents)",
                    pageSize=1000,
                    pageToken=page_token,
                    corpora="user",
                    includeItemsFromAllDrives=False,
                    supportsAllDrives=False,
                )
            )
            for f in resp.get("files", []):
                parents = f.get("parents", [])
                folder_map[f["id"]] = {
                    "name": f["name"],
                    "parent": parents[0] if parents else None,
                }
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return folder_map

    def _resolve_path(self, parents: list, folder_map: dict) -> str:
        """Walk up the parent chain to build a full path string."""
        if not parents:
            return ""
        parts = []
        node_id = parents[0]
        seen = set()
        while node_id and node_id not in seen:
            seen.add(node_id)
            folder = folder_map.get(node_id)
            if not folder:
                break  # reached root or an unmapped folder
            parts.append(folder["name"])
            node_id = folder.get("parent")
        return "/".join(reversed(parts))

    def _flat_scan_my_drive(self, folder_map: dict) -> Iterator[dict]:
        """List every non-folder file in My Drive and attach a resolved path."""
        page_token = None
        while True:
            resp = self._api_call(
                self.service.files().list(
                    q="mimeType != 'application/vnd.google-apps.folder' and trashed = false",
                    fields="nextPageToken, files(id, name, mimeType, size, parents)",
                    pageSize=1000,
                    pageToken=page_token,
                    corpora="user",
                    includeItemsFromAllDrives=False,
                    supportsAllDrives=False,
                )
            )
            for item in resp.get("files", []):
                mime = item["mimeType"]
                if mime in SKIP_MIME_TYPES:
                    continue
                item["_path"] = self._resolve_path(item.get("parents", []), folder_map)
                yield item

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    # ── Shared Drives recursive walk ──────────────────────────────────────────

    def list_shared_drives(self) -> list:
        """Return all shared drives the authenticated user can access."""
        drives, page_token = [], None
        while True:
            resp = self._api_call(
                self.service.drives().list(pageSize=100, pageToken=page_token)
            )
            drives.extend(resp.get("drives", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return drives

    # keep private alias for internal callers
    _list_shared_drives = list_shared_drives

    def list_my_drive_files(self) -> Iterator[dict]:
        folder_map = self._build_folder_map()
        yield from self._flat_scan_my_drive(folder_map)

    def list_drive_files(self, drive_id: str, drive_name: str) -> Iterator[dict]:
        yield from self._walk_shared_drive(drive_id, f"[Shared] {drive_name}")

    def list_folder_files(self, folder_id: str) -> Iterator[dict]:
        """Walk a specific folder (My Drive or Shared Drive) recursively."""
        stack = [(folder_id, "")]
        while stack:
            fid, prefix = stack.pop()
            page_token = None
            while True:
                resp = self._api_call(
                    self.service.files().list(
                        q=f"'{fid}' in parents and trashed = false",
                        fields="nextPageToken, files(id, name, mimeType, size, parents)",
                        pageSize=1000,
                        pageToken=page_token,
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                    )
                )
                for item in resp.get("files", []):
                    mime = item["mimeType"]
                    if mime == "application/vnd.google-apps.folder":
                        sub = f"{prefix}/{item['name']}" if prefix else item["name"]
                        stack.append((item["id"], sub))
                    elif mime not in SKIP_MIME_TYPES:
                        item["_path"] = prefix
                        yield item
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

    def _walk_shared_drive(self, drive_id: str, path_prefix: str) -> Iterator[dict]:
        stack = [(drive_id, path_prefix)]
        while stack:
            folder_id, prefix = stack.pop()
            page_token = None
            while True:
                resp = self._api_call(
                    self.service.files().list(
                        q=f"'{folder_id}' in parents and trashed = false",
                        fields="nextPageToken, files(id, name, mimeType, size, parents)",
                        pageSize=1000,
                        pageToken=page_token,
                        corpora="drive",
                        driveId=drive_id,
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                    )
                )
                for item in resp.get("files", []):
                    mime = item["mimeType"]
                    if mime in SKIP_MIME_TYPES:
                        continue
                    if mime == "application/vnd.google-apps.folder":
                        sub_path = f"{prefix}/{item['name']}" if prefix else item["name"]
                        stack.append((item["id"], sub_path))
                    else:
                        item["_path"] = prefix
                        yield item
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

    # ── Download ──────────────────────────────────────────────────────────────

    def _get_mime_type(self, file_id: str) -> str:
        meta = self._api_call(
            self.service.files().get(fileId=file_id, fields="mimeType", supportsAllDrives=True)
        )
        return meta.get("mimeType", "")

    def download_file(self, file_id: str, mime_type: str, dest_path: str, chunk_size: int) -> str | None:
        """Download file to dest_path. Returns actual extension used (e.g. '.csv') for
        Google Workspace files, or None for binary files. Extension may differ from the
        original remote_key if a smaller fallback format was chosen."""
        if mime_type not in EXPORT_FALLBACKS:
            try:
                request = self.service.files().get_media(fileId=file_id, supportsAllDrives=True)
                self._stream_download(request, dest_path, chunk_size)
            except HttpError as e:
                if os.path.exists(dest_path):
                    os.remove(dest_path)
                # File is a Google Workspace native type stored with a misleading extension
                # (common when loaded from CSV inventory). Fetch the real mime type and export.
                if e.resp.status == 403 and "fileNotDownloadable" in str(e):
                    actual_mime = self._get_mime_type(file_id)
                    if actual_mime in EXPORT_FALLBACKS:
                        logger.info(f"Switching {file_id} to export (actual mime: {actual_mime})")
                        return self._export_with_fallback(file_id, EXPORT_FALLBACKS[actual_mime], dest_path, chunk_size)
                    # Unknown workspace type (form, site, map, etc.) — try PDF as a last resort.
                    if actual_mime.startswith("application/vnd.google-apps."):
                        logger.info(f"Unknown workspace type {actual_mime} for {file_id}, trying PDF export")
                        try:
                            request = self.service.files().export_media(fileId=file_id, mimeType="application/pdf")
                            self._stream_download(request, dest_path, chunk_size)
                            return ".pdf"
                        except Exception:
                            if os.path.exists(dest_path):
                                os.remove(dest_path)
                raise
            except Exception:
                if os.path.exists(dest_path):
                    os.remove(dest_path)
                raise
            return None
        try:
            return self._export_with_fallback(file_id, EXPORT_FALLBACKS[mime_type], dest_path, chunk_size)
        except Exception:
            if os.path.exists(dest_path):
                os.remove(dest_path)
            # For spreadsheets, try the Sheets API as a last resort when all Drive
            # export formats fail (exportSizeLimitExceeded or PDF generation crash).
            if mime_type == "application/vnd.google-apps.spreadsheet":
                logger.warning(f"All Drive export formats failed for {file_id}, falling back to Sheets API")
                try:
                    return self._export_via_sheets_api(file_id, dest_path)
                except Exception:
                    if os.path.exists(dest_path):
                        os.remove(dest_path)
                    raise
            raise

    def _export_with_fallback(self, file_id: str, formats: list, dest_path: str, chunk_size: int) -> str:
        """Try each (mime, ext) in order, skipping on exportSizeLimitExceeded. Returns ext used."""
        last_exc = None
        size_exceeded = False
        for mime, ext in formats:
            try:
                request = self.service.files().export_media(fileId=file_id, mimeType=mime)
                self._stream_download(request, dest_path, chunk_size)
                return ext
            except HttpError as e:
                if e.resp.status == 403 and "exportSizeLimitExceeded" in str(e):
                    size_exceeded = True
                    last_exc = e
                    logger.warning(f"Export as {mime} too large for {file_id}, trying next format")
                    if os.path.exists(dest_path):
                        os.remove(dest_path)
                    continue
                # PDF export crashes with 500 on very large sheets after xlsx/csv already
                # exceeded the size limit — treat it as another size-limit failure.
                if e.resp.status == 500 and size_exceeded:
                    last_exc = e
                    if os.path.exists(dest_path):
                        os.remove(dest_path)
                    continue
                raise
        raise last_exc

    def _get_sheets_service(self):
        if self._sheets_service is None:
            if self._authorized_http is None:
                raise RuntimeError("Call authenticate() before using the client.")
            self._sheets_service = build("sheets", "v4", http=self._authorized_http, cache_discovery=False)
        return self._sheets_service

    def _export_via_sheets_api(self, file_id: str, dest_path: str) -> str:
        """Export a spreadsheet via the Sheets API, bypassing Drive export size limits.
        Returns '.csv' for single-sheet files, '.zip' for multi-sheet files."""
        svc = self._get_sheets_service()

        meta = self._api_call(
            svc.spreadsheets().get(
                spreadsheetId=file_id,
                fields="sheets.properties(title,sheetType,gridProperties)",
            )
        )
        sheets = [
            s["properties"]
            for s in meta.get("sheets", [])
            if s["properties"].get("sheetType", "GRID") == "GRID"
        ]
        if not sheets:
            raise RuntimeError(f"No grid sheets found in spreadsheet {file_id}")
        logger.info(f"Sheets API export: {len(sheets)} sheet(s) in {file_id}")

        def read_sheet(title: str, row_count: int) -> list:
            rows, start, chunk = [], 1, 50_000
            while start <= row_count:
                end = min(start + chunk - 1, row_count)
                result = self._api_call(
                    svc.spreadsheets().values().get(
                        spreadsheetId=file_id,
                        range=f"'{title}'!A{start}:ZZZ{end}",
                        valueRenderOption="FORMATTED_VALUE",
                        dateTimeRenderOption="FORMATTED_STRING",
                    )
                )
                batch = result.get("values", [])
                rows.extend(batch)
                if not batch or len(batch) < (end - start + 1):
                    break
                start += chunk
            return rows

        def to_csv_bytes(rows: list) -> bytes:
            buf = io.StringIO()
            w = csv.writer(buf)
            max_cols = max((len(r) for r in rows), default=0)
            for row in rows:
                w.writerow(row + [""] * (max_cols - len(row)))
            return buf.getvalue().encode("utf-8", errors="replace")

        if len(sheets) == 1:
            props = sheets[0]
            row_count = props.get("gridProperties", {}).get("rowCount", 10_000)
            rows = read_sheet(props["title"], row_count)
            logger.info(f"  '{props['title']}': {len(rows)} rows")
            with open(dest_path, "wb") as f:
                f.write(to_csv_bytes(rows))
            return ".csv"
        else:
            with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for props in sheets:
                    title = props["title"]
                    row_count = props.get("gridProperties", {}).get("rowCount", 10_000)
                    rows = read_sheet(title, row_count)
                    logger.info(f"  '{title}': {len(rows)} rows")
                    safe_title = title.replace("/", "_").replace("\\", "_")
                    zf.writestr(f"{safe_title}.csv", to_csv_bytes(rows))
            return ".zip"

    def _stream_download(self, request, dest_path: str, chunk_size: int):
        with open(dest_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request, chunksize=chunk_size)
            done = False
            while not done:
                _, done = self._retry_chunk(downloader)

    def _retry_chunk(self, downloader: MediaIoBaseDownload):
        for attempt in range(5):
            try:
                return downloader.next_chunk()
            except HttpError as e:
                if e.resp.status in (429, 500, 502, 503) and attempt < 4:
                    wait = 2 ** attempt * 3
                    logger.warning(f"Chunk error {e.resp.status}, retrying in {wait}s")
                    time.sleep(wait)
                else:
                    raise
            except (socket.timeout, TimeoutError, ConnectionError, OSError) as e:
                if attempt < 4:
                    wait = 2 ** attempt * 5
                    logger.warning(f"Chunk network error ({type(e).__name__}), retrying in {wait}s")
                    time.sleep(wait)
                else:
                    raise

    def _api_call(self, request, retries: int = 5):
        for attempt in range(retries):
            try:
                return request.execute()
            except HttpError as e:
                if e.resp.status in (429, 500, 502, 503) and attempt < retries - 1:
                    wait = 2 ** attempt * 3
                    logger.warning(f"API error {e.resp.status}, retrying in {wait}s")
                    time.sleep(wait)
                else:
                    raise
            except (socket.timeout, TimeoutError, ConnectionError, OSError) as e:
                if attempt < retries - 1:
                    wait = 2 ** attempt * 5
                    logger.warning(f"API network error ({type(e).__name__}), retrying in {wait}s")
                    time.sleep(wait)
                else:
                    raise
