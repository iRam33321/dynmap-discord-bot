"""
Dynmap Discord Deployment Bot
------------------------------
Monitors a Discord channel for .zip file uploads containing Dynmap static
website files (plugins/dynmap/web/) and automatically deploys them to a
GitHub Pages repository.

Environment variables (see .env.example):
  DISCORD_TOKEN      - Your Discord bot token
  CHANNEL_ID         - Discord channel ID to monitor
  GITHUB_REPO_PATH   - Absolute path to your local GitHub Pages repository
  GITHUB_PAGES_URL   - The public GitHub Pages URL (e.g. https://user.github.io/repo)
  DELETE_ZIP         - Set to "true" to delete downloaded ZIPs after deployment (default: false)
  IGNORE_DUPLICATES  - Set to "true" to skip ZIPs with the same filename/size as the last upload (default: false)
  GIT_USER_NAME      - Git commit author name (default: "Dynmap Bot")
  GIT_USER_EMAIL     - Git commit author email (default: "bot@dynmap")
"""

import os
import io
import re
import shutil
import hashlib
import logging
import zipfile
import asyncio
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime, timezone

import discord
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dynmap-bot")

# ---------------------------------------------------------------------------
# Load environment variables
# ---------------------------------------------------------------------------
load_dotenv()

DISCORD_TOKEN     = os.environ["DISCORD_TOKEN"]
CHANNEL_ID        = int(os.environ["CHANNEL_ID"])
GITHUB_REPO_PATH  = Path(os.environ["GITHUB_REPO_PATH"]).resolve()
GITHUB_PAGES_URL  = os.environ["GITHUB_PAGES_URL"].rstrip("/")
GITHUB_PAT        = os.getenv("GITHUB_PAT", "")   # optional — used to authenticate git push over HTTPS
DELETE_ZIP        = os.getenv("DELETE_ZIP", "false").lower() == "true"
IGNORE_DUPLICATES = os.getenv("IGNORE_DUPLICATES", "false").lower() == "true"
GIT_USER_NAME     = os.getenv("GIT_USER_NAME", "Dynmap Bot")
GIT_USER_EMAIL    = os.getenv("GIT_USER_EMAIL", "bot@dynmap")

# ---------------------------------------------------------------------------
# State — track the last processed file to support optional duplicate detection
# ---------------------------------------------------------------------------
_last_processed_hash: str | None = None

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _sha256_of_bytes(data: bytes) -> str:
    """Return the SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def _ensure_repo_dir() -> None:
    """Create the repository directory if it does not already exist."""
    GITHUB_REPO_PATH.mkdir(parents=True, exist_ok=True)
    log.info("Repository directory ready: %s", GITHUB_REPO_PATH)


def _clean_repo_dir() -> None:
    """
    Delete every file and folder inside the repository root EXCEPT the
    hidden .git directory, which must be preserved to keep git history.
    """
    log.info("Cleaning repository directory (preserving .git)…")
    for entry in GITHUB_REPO_PATH.iterdir():
        if entry.name == ".git":
            continue  # never touch the git metadata
        if entry.is_dir():
            shutil.rmtree(entry)
            log.debug("  Removed directory: %s", entry.name)
        else:
            entry.unlink()
            log.debug("  Removed file: %s", entry.name)
    log.info("Repository directory cleaned.")


def _safe_extract(zip_bytes: bytes, dest: Path) -> int:
    """
    Safely extract all members of a ZIP archive into *dest*, guarding
    against path traversal attacks (zip-slip).

    Returns the number of members extracted.
    """
    extracted = 0
    dest_resolved = dest.resolve()

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        members = zf.infolist()
        log.info("ZIP contains %d members.", len(members))

        for member in members:
            raw_name = member.filename

            # Skip directory-only entries; directories are created implicitly
            if not raw_name or raw_name.endswith("/"):
                continue

            # Reject any member whose name contains traversal sequences or
            # starts with an absolute path.  We refuse rather than sanitise so
            # that a malicious or malformed ZIP never silently lands unexpected
            # files inside the repository.
            if (
                raw_name.startswith(("/", "\\"))
                or ".." + os.sep in raw_name
                or raw_name.startswith("..")
                or "/../" in raw_name
                or raw_name.replace("\\", "/").startswith("../")
                or "/../" in raw_name.replace("\\", "/")
            ):
                log.warning("Skipping unsafe ZIP member (path traversal attempt): %s", raw_name)
                continue

            target = (dest_resolved / raw_name).resolve()

            # Final safety net: resolved target must still be inside dest
            if not str(target).startswith(str(dest_resolved) + os.sep):
                log.warning("Skipping unsafe ZIP member (escapes dest): %s", raw_name)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)

            with zf.open(member) as src, open(target, "wb") as dst:
                # Stream in 4 MiB chunks to handle large tile files efficiently
                while chunk := src.read(4 * 1024 * 1024):
                    dst.write(chunk)

            extracted += 1
            if extracted % 500 == 0:
                log.info("  … extracted %d files so far", extracted)

    log.info("Extraction complete — %d files extracted.", extracted)
    return extracted


def _git_run(args: list[str], cwd: Path) -> str:
    """
    Run a git sub-command inside *cwd* and return stdout.
    Raises subprocess.CalledProcessError on non-zero exit.
    """
    cmd = ["git", "-C", str(cwd)] + args
    log.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _deploy_to_git() -> str:
    """
    Stage all changes, commit, and push to the remote repository.
    Returns the final git log one-liner for the commit.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    commit_msg = f"chore: deploy Dynmap snapshot — {timestamp}"

    # Configure identity (only for this repo, does not touch global config)
    _git_run(["config", "user.name",  GIT_USER_NAME],  cwd=GITHUB_REPO_PATH)
    _git_run(["config", "user.email", GIT_USER_EMAIL], cwd=GITHUB_REPO_PATH)

    # Embed the PAT into the remote URL so git push never prompts for a password.
    # We read the current remote, inject credentials, set it, push, then restore
    # the original URL so the PAT is never persisted in plain text on disk.
    if GITHUB_PAT:
        original_url = _git_run(["remote", "get-url", "origin"], cwd=GITHUB_REPO_PATH)
        # Build authenticated URL: https://x-access-token:PAT@github.com/...
        authed_url = original_url.replace(
            "https://", f"https://x-access-token:{GITHUB_PAT}@"
        )
        _git_run(["remote", "set-url", "origin", authed_url], cwd=GITHUB_REPO_PATH)

    # Stage all changes (additions, modifications, deletions)
    _git_run(["add", "--all"], cwd=GITHUB_REPO_PATH)

    # Check whether there is anything to commit
    status = _git_run(["status", "--porcelain"], cwd=GITHUB_REPO_PATH)
    if not status:
        log.info("No changes detected — nothing to commit.")
        if GITHUB_PAT:
            _git_run(["remote", "set-url", "origin", original_url], cwd=GITHUB_REPO_PATH)
        return "No changes"

    _git_run(["commit", "-m", commit_msg], cwd=GITHUB_REPO_PATH)
    log.info("Committed: %s", commit_msg)

    _git_run(["push"], cwd=GITHUB_REPO_PATH)
    log.info("Pushed to remote.")

    # Restore the original URL (without embedded credentials)
    if GITHUB_PAT:
        _git_run(["remote", "set-url", "origin", original_url], cwd=GITHUB_REPO_PATH)

    # Return the short log line for the new commit
    return _git_run(["log", "-1", "--oneline"], cwd=GITHUB_REPO_PATH)


# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True          # required to read message attachments
client = discord.Client(intents=intents)


@client.event
async def on_ready() -> None:
    log.info("Logged in as %s (id: %s)", client.user, client.user.id)
    log.info("Monitoring channel ID: %d", CHANNEL_ID)
    log.info("Repository path:       %s", GITHUB_REPO_PATH)
    log.info("GitHub Pages URL:      %s", GITHUB_PAGES_URL)


@client.event
async def on_message(message: discord.Message) -> None:
    global _last_processed_hash

    # Only process messages from the configured channel
    if message.channel.id != CHANNEL_ID:
        return

    # Ignore messages from bots (including ourselves)
    if message.author.bot:
        return

    # Find the first .zip attachment
    zip_attachment: discord.Attachment | None = None
    for attachment in message.attachments:
        if attachment.filename.lower().endswith(".zip"):
            zip_attachment = attachment
            break

    if zip_attachment is None:
        if message.attachments:
            log.info(
                "Ignoring non-ZIP attachment from %s: %s",
                message.author,
                [a.filename for a in message.attachments],
            )
        return

    log.info(
        "Received ZIP from %s: %s (%d bytes)",
        message.author,
        zip_attachment.filename,
        zip_attachment.size,
    )

    # Acknowledge receipt so the user knows we are working on it
    status_msg = await message.reply(
        f"📦 Received **{zip_attachment.filename}** ({zip_attachment.size / 1_048_576:.1f} MB). Deploying…"
    )

    # Run the blocking deployment work in a thread pool to avoid freezing the event loop
    try:
        commit_line = await asyncio.get_running_loop().run_in_executor(
            None,
            _deploy_zip,
            zip_attachment.url,
            zip_attachment.filename,
            zip_attachment.size,
        )
    except Exception as exc:
        log.error("Deployment failed: %s", exc, exc_info=True)
        await status_msg.edit(
            content=(
                f"❌ Deployment failed for **{zip_attachment.filename}**.\n"
                f"```\n{exc}\n```"
            )
        )
        return

    if commit_line == "No changes":
        await status_msg.edit(
            content=(
                f"⚠️ **{zip_attachment.filename}** contained no changes compared to the current "
                f"repository state. GitHub Pages was not updated."
            )
        )
    else:
        await status_msg.edit(
            content=(
                f"✅ **{zip_attachment.filename}** deployed successfully!\n"
                f"🌐 {GITHUB_PAGES_URL}\n"
                f"```\n{commit_line}\n```"
            )
        )
    log.info("Deployment complete — %s", commit_line)


def _deploy_zip(url: str, filename: str, size: int) -> str:
    """
    Synchronous deployment pipeline — intended to run in an executor thread:
      1. Download the ZIP from Discord's CDN
      2. Optionally skip if it is a duplicate of the last processed file
      3. Prepare the repository directory
      4. Extract the ZIP safely
      5. Commit and push via git
      6. Optionally clean up the temporary download

    Returns the git log one-liner (or "No changes").
    """
    global _last_processed_hash

    import urllib.request

    # ------------------------------------------------------------------ 1. Download
    log.info("Downloading ZIP from Discord CDN…")
    with urllib.request.urlopen(url) as response:
        zip_bytes = response.read()
    log.info("Downloaded %d bytes.", len(zip_bytes))

    # ------------------------------------------------------------------ 2. Duplicate check
    if IGNORE_DUPLICATES:
        file_hash = _sha256_of_bytes(zip_bytes)
        if file_hash == _last_processed_hash:
            log.info("Duplicate upload detected (same SHA-256) — skipping.")
            return "No changes"
        _last_processed_hash = file_hash

    # ------------------------------------------------------------------ 3. Prepare repo dir
    _ensure_repo_dir()
    _clean_repo_dir()

    # ------------------------------------------------------------------ 4. Extract
    _safe_extract(zip_bytes, GITHUB_REPO_PATH)

    # ------------------------------------------------------------------ 5. Git commit + push
    commit_line = _deploy_to_git()

    # ------------------------------------------------------------------ 6. Cleanup
    if DELETE_ZIP:
        # ZIP was held in memory; nothing on disk to delete unless you want
        # to save a temporary copy — log for completeness.
        log.info("DELETE_ZIP is enabled (ZIP was in-memory; nothing to delete).")

    return commit_line


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("Starting Dynmap Discord Deployment Bot…")
    client.run(DISCORD_TOKEN)
