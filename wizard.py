#!/usr/bin/env python3
"""
drivetocloud setup wizard.

End-user mode (default):
    python wizard.py
    Asks only for folder name + Google Drive credentials.
    Hetzner storage details are pre-configured by the operator and hidden.

Operator mode (first-time setup):
    python wizard.py --operator
    Configures Hetzner storage credentials in operator.env.
    Run this once, then distribute the tool to your team.
"""
import argparse
import getpass
import os
import subprocess
import sys


# ── Terminal helpers ──────────────────────────────────────────────────────────

BOLD   = "\033[1m"
DIM    = "\033[2m"
GREEN  = "\033[32m"
CYAN   = "\033[36m"
YELLOW = "\033[33m"
RED    = "\033[31m"
RESET  = "\033[0m"

def _c(color, text):      return f"{color}{text}{RESET}"
def header(text):
    print()
    print(_c(BOLD, "─" * 58))
    print(_c(BOLD, f"  {text}"))
    print(_c(BOLD, "─" * 58))
def info(text):           print(f"  {_c(DIM, text)}")
def success(text):        print(f"  {_c(GREEN, '✓')} {text}")
def warn(text):           print(f"  {_c(YELLOW, '!')} {text}")
def blank():              print()


def ask(prompt, default="", secret=False):
    hint = f" [{default}]" if default else ""
    marker = _c(CYAN, "›")
    try:
        if secret:
            val = getpass.getpass(f"  {marker} {prompt}{hint}: ")
        else:
            val = input(f"  {marker} {prompt}{hint}: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n\nWizard cancelled.")
        sys.exit(0)
    return val if val else default


def choose(prompt, options):
    print(f"\n  {prompt}")
    for i, opt in enumerate(options, 1):
        print(f"    {_c(CYAN, str(i) + '.')} {opt}")
    while True:
        raw = ask("Enter number")
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            chosen = options[int(raw) - 1]
            success(f"Selected: {chosen}")
            return chosen
        warn(f"Please enter a number between 1 and {len(options)}.")


def confirm(prompt, default="y"):
    hint = "Y/n" if default == "y" else "y/N"
    val = ask(f"{prompt} ({hint})") or default
    return val.lower().startswith("y")


# ── Env file helpers ──────────────────────────────────────────────────────────

def _load_env_file(path):
    """Return {key: value} for all KEY=VALUE lines in path."""
    result = {}
    if not os.path.exists(path):
        return result
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.strip()
            # Strip surrounding quotes that other tools (direnv, docker) may write
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            result[key.strip()] = val
    return result


def _write_env_file(path, updates):
    """Merge updates into an existing env file (or create it)."""
    lines = []
    written = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                raw = line.rstrip("\n")
                if "=" in raw and not raw.startswith("#"):
                    key = raw.split("=", 1)[0].strip()
                    if key in updates:
                        lines.append(f"{key}={updates[key]}")
                        written.add(key)
                        continue
                lines.append(raw)
    for key, val in updates.items():
        if key not in written:
            lines.append(f"{key}={val}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _ensure_credentials_dir():
    """Create credentials/ if it doesn't exist and tell the user."""
    if not os.path.isdir("credentials"):
        os.makedirs("credentials", exist_ok=True)
        success("Created credentials/ folder — put your key files in there.")
    else:
        info("credentials/ folder already exists.")


def _sftp_preconfigured():
    """Return True if Hetzner credentials are already set (from operator.env or env)."""
    # Load operator.env directly — don't rely on dotenv having run yet
    op = _load_env_file("operator.env")
    host = op.get("SFTP_HOST") or os.getenv("SFTP_HOST", "")
    user = op.get("SFTP_USERNAME") or os.getenv("SFTP_USERNAME", "")
    pw   = op.get("SFTP_PASSWORD") or os.getenv("SFTP_PASSWORD", "")
    return bool(host and user and pw)


# ── Operator wizard ───────────────────────────────────────────────────────────

def run_operator_wizard():
    print()
    print(_c(BOLD, "  ╔══════════════════════════════════════════╗"))
    print(_c(BOLD, "  ║    drivetocloud  —  Operator Setup       ║"))
    print(_c(BOLD, "  ╚══════════════════════════════════════════╝"))
    blank()
    info("This configures the shared Hetzner Storage Box credentials.")
    info("Settings are saved to operator.env — distribute this file")
    info("alongside the tool so users never have to enter these details.")
    blank()

    header("Hetzner Storage Box credentials")
    info("Find these in Hetzner Robot → Storage Boxes → your box.")
    blank()

    host     = ask("Hostname (e.g. u123456.your-storagebox.de)")
    port     = ask("Port", "23")
    username = ask("Username (e.g. u123456)")
    password = ask("Password", secret=True)

    blank()
    header("Default transfer settings")
    info("These are the defaults — each user can override them in their wizard.")

    workers = ask("Parallel upload workers", "8")
    chunk   = ask("Upload chunk size in MB", "64")
    retries = ask("Max retries per file",    "5")

    op_env = {
        "SFTP_HOST":        host,
        "SFTP_PORT":        port,
        "SFTP_USERNAME":    username,
        "SFTP_PASSWORD":    password,
        "DESTINATION":      "sftp",
        "PARALLEL_WORKERS": workers,
        "CHUNK_SIZE_MB":    chunk,
        "MAX_RETRIES":      retries,
        "TEMP_DIR":         "/tmp/drivetocloud",
    }

    _write_env_file("operator.env", op_env)

    blank()
    success("operator.env written.")
    blank()
    info("Next steps:")
    info("  1. Add operator.env to your .gitignore (it has credentials).")
    info("  2. Share the tool folder + operator.env with your team.")
    info("  3. Each team member runs:  python wizard.py")
    info("     They will only be asked for their folder name + Google Drive login.")
    blank()

    # Suggest gitignore entry
    gitignore = ".gitignore"
    already_ignored = False
    if os.path.exists(gitignore):
        with open(gitignore) as f:
            already_ignored = "operator.env" in f.read()
    if not already_ignored:
        if confirm("Add operator.env to .gitignore now?"):
            with open(gitignore, "a") as f:
                f.write("\noperator.env\n")
            success(".gitignore updated.")
    blank()


# ── User wizard ───────────────────────────────────────────────────────────────

def _wizard_destination(user_env: dict, preconfigured: bool) -> str:
    """
    Ask the user where to upload files.
    Returns a short tag: "hetzner" | "sftp" | "s3"
    Mutates user_env with the relevant settings.
    """
    header("Step 2 — Where to upload")

    options = []
    if preconfigured:
        options.append("Our Hetzner Storage Box  (pre-configured by your admin)")
    options += [
        "My own SFTP server",
        "My own S3-compatible storage  (AWS S3, Backblaze B2, Wasabi, Cloudflare R2, etc.)",
    ]

    dest = choose("Upload destination:", options)

    blank()

    # ── Our Hetzner ───────────────────────────────────────────────────────────
    if dest.startswith("Our Hetzner"):
        info("Using the pre-configured Hetzner Storage Box.")
        folder_name = _ask_folder_name()
        user_env["SFTP_BASE_PATH"] = folder_name
        user_env["DESTINATION"]    = "sftp"
        success(f"Files will go to: Hetzner → {folder_name}/")
        return "hetzner"

    # ── Their own SFTP ────────────────────────────────────────────────────────
    if dest.startswith("My own SFTP"):
        info("Enter your SFTP server details.")
        blank()
        user_env["SFTP_HOST"]      = ask("Hostname or IP address")
        user_env["SFTP_PORT"]      = ask("Port", "22")
        user_env["SFTP_USERNAME"]  = ask("Username")
        user_env["SFTP_PASSWORD"]  = ask("Password", secret=True)
        folder_name = _ask_folder_name()
        user_env["SFTP_BASE_PATH"] = folder_name
        user_env["DESTINATION"]    = "sftp"
        success(f"Files will go to: {user_env['SFTP_HOST']} → {folder_name}/")
        return "sftp"

    # ── Their own S3 ──────────────────────────────────────────────────────────
    blank()
    info("Works with: AWS S3, Backblaze B2, Wasabi, Cloudflare R2, MinIO, and more.")
    blank()

    provider = choose("Which provider?", [
        "Amazon S3",
        "Backblaze B2",
        "Wasabi",
        "Cloudflare R2",
        "MinIO or other self-hosted S3",
        "Other S3-compatible",
    ])

    endpoint_hints = {
        "Amazon S3":                    ("",                                              "us-east-1"),
        "Backblaze B2":                 ("https://s3.us-west-002.backblazeb2.com",        "us-west-002"),
        "Wasabi":                       ("https://s3.wasabisys.com",                      "us-east-1"),
        "Cloudflare R2":                ("https://<account-id>.r2.cloudflarestorage.com", "auto"),
        "MinIO or other self-hosted S3":("http://localhost:9000",                         "us-east-1"),
        "Other S3-compatible":          ("",                                              "us-east-1"),
    }
    default_endpoint, default_region = endpoint_hints.get(provider, ("", "us-east-1"))

    blank()
    if provider == "Amazon S3":
        info("Access keys: AWS Console → IAM → Users → Security credentials → Create access key")
    elif provider == "Backblaze B2":
        info("Keys: Backblaze dashboard → App Keys → Add a New Application Key")
        info("Endpoint: Backblaze shows this in your bucket settings (e.g. s3.us-west-002.backblazeb2.com)")
    elif provider == "Wasabi":
        info("Keys: Wasabi console → Access Keys → Create New Access Key")
    elif provider == "Cloudflare R2":
        info("Keys: Cloudflare dashboard → R2 → Manage R2 API tokens")
        info("Endpoint: shown in your R2 bucket settings")
    blank()

    user_env["S3_BUCKET"]       = ask("Bucket name")
    user_env["S3_ACCESS_KEY"]   = ask("Access key ID")
    user_env["S3_SECRET_KEY"]   = ask("Secret access key", secret=True)
    user_env["S3_ENDPOINT_URL"] = ask("Endpoint URL (leave blank for AWS)", default_endpoint)
    user_env["S3_REGION"]       = ask("Region", default_region)
    folder_name = _ask_folder_name(label="Folder prefix inside the bucket (optional)")
    if folder_name:
        user_env["S3_PREFIX"]   = folder_name
    user_env["DESTINATION"]     = "s3"
    bucket = user_env["S3_BUCKET"]
    success(f"Files will go to: s3://{bucket}/{folder_name or ''}")
    return "s3"


def _ask_folder_name(label="Folder name for your files (e.g. your name or project)") -> str:
    while True:
        val = ask(label).strip().replace(" ", "_")
        if val:
            return val
        if label.endswith("(optional)"):
            return ""
        warn("Folder name cannot be empty.")


def run_user_wizard():
    print()
    print(_c(BOLD, "  ╔══════════════════════════════════════════╗"))
    print(_c(BOLD, "  ║       drivetocloud  —  Setup Wizard      ║"))
    print(_c(BOLD, "  ╚══════════════════════════════════════════╝"))
    blank()
    info("This wizard sets up your configuration in about 2 minutes.")
    blank()

    preconfigured = _sftp_preconfigured()

    user_env: dict = {}
    local_source_dir = None
    source_type = "oauth"

    # ── Step 1: Source ────────────────────────────────────────────────────────
    header("Step 1 — Where are your files?")

    source = choose("Source type:", [
        "Google Drive — personal account  (sign in with browser)",
        "Google Drive — organization / workspace  (service account key)",
        "Local folder on this computer",
    ])

    if "Local folder" in source:
        source_type = "local"
        blank()
        info("Files will be read from a folder on this machine and uploaded.")
        blank()
        raw = ask("Full path to the folder to upload").strip()
        local_source_dir = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isdir(local_source_dir):
            warn(f"Folder not found: {local_source_dir}")
            warn("Make sure the path is correct before running.")
        else:
            success(f"Found: {local_source_dir}")

    elif "service account" in source:
        source_type = "service_account"
        blank()
        info("A service account key lets the tool access your organization's Google Drive")
        info("without individual browser logins.")
        blank()
        info("How to get the key file:")
        info("  1. Go to console.cloud.google.com")
        info("  2. IAM & Admin → Service Accounts → select your account")
        info("  3. Keys → Add Key → JSON → download and save it in the credentials/ folder")
        _ensure_credentials_dir()
        blank()
        sa_file = ask("Path to service account JSON file", "credentials/sa_key.json")
        if not os.path.exists(sa_file):
            warn(f"File not found: {sa_file} — save it there before running enumerate.")
        user_env["GDRIVE_SERVICE_ACCOUNT_FILE"] = sa_file
        impersonate = ask("Email to impersonate for workspace-wide access (optional, press Enter to skip)")
        if impersonate.strip():
            user_env["GDRIVE_IMPERSONATE_USER"] = impersonate.strip()

    else:
        # OAuth
        blank()
        info("You will sign in to Google in a browser window when you first run enumerate.")
        blank()
        info("How to get the OAuth client file:")
        info("  1. Go to console.cloud.google.com")
        info("  2. APIs & Services → Credentials")
        info("  3. Create Credentials → OAuth 2.0 Client ID → Desktop App → Download JSON")
        info("  4. Save it in the credentials/ folder here")
        _ensure_credentials_dir()
        blank()
        oauth_file = ask("Path to OAuth client JSON file", "credentials/oauth_client.json")
        if not os.path.exists(oauth_file):
            warn(f"File not found: {oauth_file} — download it from Google Cloud Console.")
        user_env["GDRIVE_OAUTH_CLIENT_FILE"] = oauth_file
        user_env["GDRIVE_TOKEN_FILE"]        = "credentials/token.json"

    # ── Step 2: Destination ───────────────────────────────────────────────────
    dest_type = _wizard_destination(user_env, preconfigured)

    # ── State DB name — isolated per folder so parallel users don't share state
    folder_key = (
        user_env.get("SFTP_BASE_PATH") or
        user_env.get("S3_PREFIX") or
        "transfer"
    )
    user_env["STATE_DB"] = f"{folder_key}_state.db"

    # ── Write .env ────────────────────────────────────────────────────────────
    header("Saving your configuration")
    _write_env_file(".env", user_env)
    success(".env written.")

    # ── Summary ───────────────────────────────────────────────────────────────
    header("All done — run these commands to start")
    blank()

    if source_type == "local":
        print(f"  {_c(CYAN, 'python main.py enumerate-local')} \"{local_source_dir}\"")
        info("    scans the folder and queues files for upload")
        blank()
        print(f"  {_c(CYAN, 'python main.py status')}")
        info("    shows how many files were found")
        blank()
        print(f"  {_c(CYAN, 'python main.py transfer')}")
        info("    starts uploading — safe to stop and resume any time")
    else:
        print(f"  {_c(CYAN, 'python main.py list-drives')}")
        info("    shows all Google Drive folders you have access to")
        blank()
        print(f"  {_c(CYAN, 'python main.py enumerate --folder')} \"<paste folder URL here>\"")
        info("    (or: --drives \"Drive Name\"  to target a specific shared drive)")
        info("    (or: python main.py enumerate   to scan everything)")
        blank()
        print(f"  {_c(CYAN, 'python main.py status')}")
        info("    shows how many files were found")
        blank()
        print(f"  {_c(CYAN, 'python main.py transfer')}")
        info("    starts uploading — safe to stop and resume any time")

    blank()
    print(f"  {_c(BOLD, 'Other useful commands:')}")
    print(f"    {_c(CYAN, 'python main.py retry-failed')}  retry any files that failed")
    print(f"    {_c(CYAN, 'python main.py show-failed')}   see what failed and why")
    blank()

    # ── Optionally kick off now ───────────────────────────────────────────────
    if source_type == "local" and local_source_dir and os.path.isdir(local_source_dir):
        if confirm("Scan your folder now?"):
            subprocess.run([sys.executable, "main.py", "enumerate-local", local_source_dir])
            blank()
            # Show status so the user knows what they're about to upload before confirming.
            result = subprocess.run([sys.executable, "main.py", "status"], capture_output=True, text=True)
            print(result.stdout)
            if result.stdout.strip():
                if confirm("Start uploading now?"):
                    subprocess.run([sys.executable, "main.py", "transfer"])
            else:
                warn("No files found to transfer. Check the folder path and try again.")
    elif source_type in ("oauth", "service_account"):
        if confirm("Verify your Google Drive connection now (list-drives)?"):
            subprocess.run([sys.executable, "main.py", "list-drives"])

    blank()
    success("Setup complete. Your configuration is saved in .env")
    blank()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--operator", action="store_true",
                        help="Operator mode: configure shared Hetzner credentials in operator.env")
    args, _ = parser.parse_known_args()

    if args.operator:
        run_operator_wizard()
    else:
        run_user_wizard()


if __name__ == "__main__":
    main()
