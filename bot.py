"""
Dynmap Discord Deployment Bot
------------------------------
Two deployment modes:

  MODE 1 — ZIP upload
    Upload a .zip file to the monitored Discord channel. The bot downloads,
    extracts, and pushes the contents to GitHub Pages.

  MODE 2 — FTP/SFTP trigger  ← NEW
    Send the trigger phrase (default: "Map is ready for print") in the
    monitored channel. The bot connects to your Minecraft server via FTP or
    SFTP, downloads plugins/dynmap/web/ directly, and pushes to GitHub Pages.
    No ZIP required — fully automatic end-to-end.

Environment variables (copy .env.example to .env and fill in):

  Required:
    DISCORD_TOKEN      Discord bot token
    CHANNEL_ID         Discord channel ID to monitor
    GITHUB_REPO_PATH   Absolute path to local GitHub Pages repo clone
    GITHUB_PAGES_URL   Public GitHub Pages URL

  FTP / SFTP (required for Mode 2):
    FTP_HOST           Minecraft server hostname or IP
    FTP_USER           FTP/SFTP username
    FTP_PASS           FTP/SFTP password
    FTP_PATH           Remote path to dynmap web dir
                       (default: /plugins/dynmap/web)
    FTP_PORT           Port — 21 for FTP, 22 for SFTP (default: 21)
    USE_SFTP           Set "true" to use SFTP instead of plain FTP
    TRIGGER_PHRASE     Message that starts an FTP deploy
                       (default: Map is ready for print)

  Optional:
    GITHUB_PAT         GitHub personal access token for HTTPS push auth
    GIT_USER_NAME      Commit author name  (default: Dynmap Bot)
    GIT_USER_EMAIL     Commit author email (default: bot@dynmap)
    DELETE_ZIP         "true" — note ZIP deletion after deploy
    IGNORE_DUPLICATES  "true" — skip ZIPs identical to last deploy
    HEALTH_PORT        Port for the keep-alive HTTP server (default: 9090)
"""

import os
import io
import re
import shutil
import hashlib
import logging
import zipfile
import asyncio
import subprocess
import ftplib
from pathlib import Path
from datetime import datetime, timezone

import discord
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dynmap-bot")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()

DISCORD_TOKEN     = os.environ["DISCORD_TOKEN"]
CHANNEL_ID        = int(os.environ["CHANNEL_ID"])
GITHUB_REPO_PATH  = Path(os.environ["GITHUB_REPO_PATH"]).resolve()
GITHUB_PAGES_URL  = os.environ["GITHUB_PAGES_URL"].rstrip("/")
GITHUB_PAT        = os.getenv("GITHUB_PAT", "")
GIT_USER_NAME     = os.getenv("GIT_USER_NAME", "Dynmap Bot")
GIT_USER_EMAIL    = os.getenv("GIT_USER_EMAIL", "bot@dynmap")
DELETE_ZIP        = os.getenv("DELETE_ZIP", "false").lower() == "true"
IGNORE_DUPLICATES = os.getenv("IGNORE_DUPLICATES", "false").lower() == "true"
HEALTH_PORT       = int(os.getenv("HEALTH_PORT", "9090"))

# FTP / SFTP settings
_ftp_host_raw     = os.getenv("FTP_HOST", "")
# Strip port if someone stored the host as "hostname:21"
if ":" in _ftp_host_raw:
    _ftp_host_raw, _embedded_port = _ftp_host_raw.rsplit(":", 1)
    _ftp_default_port = int(_embedded_port)
else:
    _ftp_default_port = 21
FTP_HOST          = _ftp_host_raw
FTP_USER          = os.getenv("FTP_USER", "")
FTP_PASS          = os.getenv("FTP_PASS", "")
FTP_PATH          = os.getenv("FTP_PATH", "/plugins/dynmap/web")
FTP_PORT          = int(os.getenv("FTP_PORT", str(_ftp_default_port)))
USE_SFTP          = os.getenv("USE_SFTP", "false").lower() == "true"
TRIGGER_PHRASE    = os.getenv("TRIGGER_PHRASE", "Map is ready for print").lower()

# FTP mode is available when host credentials are configured
FTP_ENABLED = bool(FTP_HOST and FTP_USER and FTP_PASS)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_last_processed_hash: str | None = None

# ---------------------------------------------------------------------------
# Shared git helpers
# ---------------------------------------------------------------------------

def _git_run(args: list[str], cwd: Path) -> str:
    cmd = ["git", "-C", str(cwd)] + args
    log.debug("git %s", " ".join(args))
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _ensure_repo_dir() -> None:
    GITHUB_REPO_PATH.mkdir(parents=True, exist_ok=True)
    log.info("Repository directory ready: %s", GITHUB_REPO_PATH)


def _clean_repo_dir() -> None:
    """Wipe repo root, preserving .git so history is kept."""
    log.info("Cleaning repository directory (preserving .git)…")
    for entry in GITHUB_REPO_PATH.iterdir():
        if entry.name == ".git":
            continue
        (shutil.rmtree if entry.is_dir() else entry.unlink)(entry)
    log.info("Repository directory cleaned.")


def _deploy_to_git() -> str:
    """Stage all changes, commit, push. Returns the new commit one-liner."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    commit_msg = f"chore: deploy Dynmap snapshot — {timestamp}"

    _git_run(["config", "user.name",  GIT_USER_NAME],  cwd=GITHUB_REPO_PATH)
    _git_run(["config", "user.email", GIT_USER_EMAIL], cwd=GITHUB_REPO_PATH)

    original_url = None
    if GITHUB_PAT:
        original_url = _git_run(["remote", "get-url", "origin"], cwd=GITHUB_REPO_PATH)
        authed_url   = original_url.replace("https://", f"https://x-access-token:{GITHUB_PAT}@")
        _git_run(["remote", "set-url", "origin", authed_url], cwd=GITHUB_REPO_PATH)

    _git_run(["add", "--all"], cwd=GITHUB_REPO_PATH)
    status = _git_run(["status", "--porcelain"], cwd=GITHUB_REPO_PATH)

    if not status:
        log.info("No changes — nothing to commit.")
        if original_url:
            _git_run(["remote", "set-url", "origin", original_url], cwd=GITHUB_REPO_PATH)
        return "No changes"

    _git_run(["commit", "-m", commit_msg], cwd=GITHUB_REPO_PATH)
    _git_run(["push"], cwd=GITHUB_REPO_PATH)
    log.info("Pushed to remote.")

    if original_url:
        _git_run(["remote", "set-url", "origin", original_url], cwd=GITHUB_REPO_PATH)

    return _git_run(["log", "-1", "--oneline"], cwd=GITHUB_REPO_PATH)


# ---------------------------------------------------------------------------
# MODE 1 — ZIP upload helpers
# ---------------------------------------------------------------------------

def _sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_extract(zip_bytes: bytes, dest: Path) -> int:
    """Extract ZIP into dest, blocking path traversal attacks."""
    extracted   = 0
    dest_resolved = dest.resolve()

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        log.info("ZIP contains %d members.", len(zf.infolist()))
        for member in zf.infolist():
            raw = member.filename
            if not raw or raw.endswith("/"):
                continue
            if (
                raw.startswith(("/", "\\"))
                or raw.startswith("..")
                or "/../" in raw
                or raw.replace("\\", "/").startswith("../")
            ):
                log.warning("Skipping unsafe ZIP member: %s", raw)
                continue
            target = (dest_resolved / raw).resolve()
            if not str(target).startswith(str(dest_resolved) + os.sep):
                log.warning("Skipping ZIP member that escapes dest: %s", raw)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                while chunk := src.read(4 * 1024 * 1024):
                    dst.write(chunk)
            extracted += 1
            if extracted % 500 == 0:
                log.info("  … %d files extracted so far", extracted)

    log.info("Extraction complete — %d files.", extracted)
    return extracted


def _deploy_zip(url: str, filename: str, size: int) -> str:
    """Download ZIP from Discord CDN, extract, commit, push."""
    global _last_processed_hash
    import urllib.request

    log.info("Downloading ZIP from Discord CDN…")
    with urllib.request.urlopen(url) as r:
        zip_bytes = r.read()
    log.info("Downloaded %d bytes.", len(zip_bytes))

    if IGNORE_DUPLICATES:
        h = _sha256_of_bytes(zip_bytes)
        if h == _last_processed_hash:
            log.info("Duplicate ZIP — skipping.")
            return "No changes"
        _last_processed_hash = h

    _ensure_repo_dir()
    _clean_repo_dir()
    _safe_extract(zip_bytes, GITHUB_REPO_PATH)
    return _deploy_to_git()


# ---------------------------------------------------------------------------
# MODE 2 — FTP / SFTP download helpers
# ---------------------------------------------------------------------------

def _ftp_download_dir(ftp: ftplib.FTP, remote_dir: str, local_dir: Path) -> int:
    """
    Recursively download all files from remote_dir into local_dir via FTP.
    Returns the number of files downloaded.
    """
    local_dir.mkdir(parents=True, exist_ok=True)
    total = 0

    try:
        ftp.cwd(remote_dir)
    except ftplib.error_perm as e:
        log.warning("Cannot CWD to %s: %s", remote_dir, e)
        return 0

    # Collect directory listing
    lines: list[str] = []
    ftp.retrlines("LIST", lines.append)

    for line in lines:
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        name     = parts[8].strip()
        is_dir   = line.startswith("d")

        if name in (".", ".."):
            continue

        if is_dir:
            # Recurse into subdirectory
            total += _ftp_download_dir(ftp, f"{remote_dir}/{name}", local_dir / name)
            ftp.cwd(remote_dir)          # return to parent after recursion
        else:
            local_file = local_dir / name
            log.debug("  FTP GET %s/%s", remote_dir, name)
            with open(local_file, "wb") as f:
                ftp.retrbinary(f"RETR {name}", f.write, blocksize=1024 * 1024)
            total += 1
            if total % 200 == 0:
                log.info("  … %d files downloaded via FTP", total)

    return total


def _sftp_download_dir(sftp, remote_dir: str, local_dir: Path) -> int:
    """
    Recursively download all files from remote_dir into local_dir via SFTP.
    Requires paramiko.
    """
    import stat as stat_module
    local_dir.mkdir(parents=True, exist_ok=True)
    total = 0

    for entry in sftp.listdir_attr(remote_dir):
        name       = entry.filename
        remote_path = f"{remote_dir}/{name}"
        local_path  = local_dir / name

        if stat_module.S_ISDIR(entry.st_mode):
            total += _sftp_download_dir(sftp, remote_path, local_path)
        else:
            log.debug("  SFTP GET %s", remote_path)
            sftp.get(remote_path, str(local_path))
            total += 1
            if total % 200 == 0:
                log.info("  … %d files downloaded via SFTP", total)

    return total


def _deploy_via_ftp() -> str:
    """
    Connect to the Minecraft server, download plugins/dynmap/web/ entirely,
    wipe the repo, copy the downloaded files in, then commit and push.
    """
    _ensure_repo_dir()
    _clean_repo_dir()

    if USE_SFTP:
        # ── SFTP path ──────────────────────────────────────────────────────
        log.info("Connecting via SFTP to %s:%d…", FTP_HOST, FTP_PORT)
        try:
            import paramiko
        except ImportError:
            raise RuntimeError(
                "paramiko is not installed. Add it to requirements.txt and redeploy."
            )
        transport = paramiko.Transport((FTP_HOST, FTP_PORT))
        transport.connect(username=FTP_USER, password=FTP_PASS)
        sftp = paramiko.SFTPClient.from_transport(transport)
        try:
            log.info("Downloading %s via SFTP…", FTP_PATH)
            count = _sftp_download_dir(sftp, FTP_PATH, GITHUB_REPO_PATH)
        finally:
            sftp.close()
            transport.close()
    else:
        # ── FTP path ───────────────────────────────────────────────────────
        log.info("Connecting via FTP to %s:%d…", FTP_HOST, FTP_PORT)
        ftp = ftplib.FTP()
        ftp.connect(FTP_HOST, FTP_PORT, timeout=30)
        ftp.login(FTP_USER, FTP_PASS)
        ftp.set_pasv(True)             # passive mode works through most firewalls
        try:
            log.info("Downloading %s via FTP…", FTP_PATH)
            count = _ftp_download_dir(ftp, FTP_PATH, GITHUB_REPO_PATH)
        finally:
            try:
                ftp.quit()
            except Exception:
                pass

    log.info("FTP download complete — %d files.", count)
    return _deploy_to_git()


# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
client  = discord.Client(intents=intents)


@client.event
async def on_ready() -> None:
    log.info("Logged in as %s (id: %s)", client.user, client.user.id)
    log.info("Monitoring channel ID: %d", CHANNEL_ID)
    log.info("GitHub Pages URL:      %s", GITHUB_PAGES_URL)
    log.info("FTP mode enabled:      %s", FTP_ENABLED)
    if FTP_ENABLED:
        proto = "SFTP" if USE_SFTP else "FTP"
        log.info("  %s host: %s:%d  path: %s", proto, FTP_HOST, FTP_PORT, FTP_PATH)
        log.info("  Trigger phrase: %r", TRIGGER_PHRASE)


@client.event
async def on_message(message: discord.Message) -> None:
    # Only process the configured channel; ignore bots
    if message.channel.id != CHANNEL_ID or message.author.bot:
        return

    content_lower = message.content.strip().lower()

    # ── MODE 2: FTP trigger phrase ────────────────────────────────────────
    if FTP_ENABLED and TRIGGER_PHRASE and content_lower == TRIGGER_PHRASE:
        log.info("FTP trigger received from %s", message.author)
        proto      = "SFTP" if USE_SFTP else "FTP"
        status_msg = await message.reply(
            f"🔌 Trigger received! Connecting via {proto} to download Dynmap files…"
        )
        try:
            commit_line = await asyncio.get_running_loop().run_in_executor(
                None, _deploy_via_ftp
            )
        except Exception as exc:
            log.error("FTP deployment failed: %s", exc, exc_info=True)
            await status_msg.edit(
                content=f"❌ FTP deployment failed.\n```\n{exc}\n```"
            )
            return

        if commit_line == "No changes":
            await status_msg.edit(
                content="⚠️ FTP download complete but no file changes detected. GitHub Pages not updated."
            )
        else:
            await status_msg.edit(
                content=(
                    f"✅ Dynmap deployed via {proto}!\n"
                    f"🌐 {GITHUB_PAGES_URL}\n"
                    f"```\n{commit_line}\n```"
                )
            )
        return

    # ── MODE 1: ZIP upload ────────────────────────────────────────────────
    zip_attachment: discord.Attachment | None = None
    for att in message.attachments:
        if att.filename.lower().endswith(".zip"):
            zip_attachment = att
            break

    if zip_attachment is None:
        return   # ignore plain messages and non-ZIP attachments

    log.info("ZIP upload from %s: %s (%d bytes)",
             message.author, zip_attachment.filename, zip_attachment.size)

    status_msg = await message.reply(
        f"📦 Received **{zip_attachment.filename}** "
        f"({zip_attachment.size / 1_048_576:.1f} MB). Deploying…"
    )

    try:
        commit_line = await asyncio.get_running_loop().run_in_executor(
            None, _deploy_zip,
            zip_attachment.url, zip_attachment.filename, zip_attachment.size,
        )
    except Exception as exc:
        log.error("ZIP deployment failed: %s", exc, exc_info=True)
        await status_msg.edit(
            content=f"❌ Deployment failed for **{zip_attachment.filename}**.\n```\n{exc}\n```"
        )
        return

    if commit_line == "No changes":
        await status_msg.edit(
            content=(
                f"⚠️ **{zip_attachment.filename}** had no changes vs the current repo. "
                "GitHub Pages not updated."
            )
        )
    else:
        await status_msg.edit(
            content=(
                f"✅ **{zip_attachment.filename}** deployed!\n"
                f"🌐 {GITHUB_PAGES_URL}\n"
                f"```\n{commit_line}\n```"
            )
        )


# ---------------------------------------------------------------------------
# Health-check server (keeps Render's free tier awake)
# ---------------------------------------------------------------------------

from aiohttp import web as aiohttp_web


async def _health_handler(request):
    return aiohttp_web.Response(text="OK")


async def _start_health_server() -> None:
    app = aiohttp_web.Application()
    app.router.add_get("/",       _health_handler)
    app.router.add_get("/health", _health_handler)
    runner = aiohttp_web.AppRunner(app)
    await runner.setup()
    site = aiohttp_web.TCPSite(runner, "0.0.0.0", HEALTH_PORT)
    try:
        await site.start()
        log.info("Health server listening on port %d", HEALTH_PORT)
    except OSError as e:
        # Port already in use (e.g. running alongside another service in dev)
        log.warning("Health server could not start on port %d: %s — continuing without it.", HEALTH_PORT, e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _main() -> None:
    await _start_health_server()
    await client.start(DISCORD_TOKEN)


if __name__ == "__main__":
    log.info("Starting Dynmap Discord Deployment Bot…")
    asyncio.run(_main())
