"""Keep an installed Jotted up to date with GitHub. Checked by `jotted start` and `jotted update`.

Only an install made with `uv tool install git+<repo>` updates itself. uv records the commit it
was built from (direct_url.json), which is compared with the head of the branch on GitHub; when
they differ, `uv tool upgrade jotted` fetches the new one. A checkout run with `uv run`, or an
install pinned to a tag or commit, is never touched. Offline or rate-limited, the check is skipped.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from importlib import metadata

DIST = "jotted"
BRANCH = "main"
SKIP_VAR = "JOTTED_NO_UPDATE"  # set to 1 to never check
DONE_VAR = "JOTTED_UPDATED"  # set on the re-run after an update, so it happens once


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class Install:
    repo: str  # "owner/name" on GitHub
    commit: str


def installed(direct_url: str | None = None) -> Install | None:
    """The GitHub repo and commit this copy was installed from; None for a checkout or a pinned install."""
    if direct_url is None:
        try:
            direct_url = metadata.distribution(DIST).read_text("direct_url.json")
        except metadata.PackageNotFoundError:
            return None
    if not direct_url:
        return None
    info = json.loads(direct_url)
    vcs = info.get("vcs_info") or {}
    url = info.get("url", "")
    if vcs.get("vcs") != "git" or vcs.get("requested_revision") or "github.com/" not in url:
        return None
    repo = url.split("github.com/", 1)[1].removesuffix(".git").strip("/")
    return Install(repo=repo, commit=vcs["commit_id"])


def latest(repo: str, branch: str = BRANCH, timeout: float = 4) -> str | None:
    """The commit at the head of `branch` on GitHub; None if GitHub can't be reached."""
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/commits/{branch}",
                                 headers={"Accept": "application/vnd.github.sha", "User-Agent": DIST})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
            sha = resp.read().decode().strip()
    except OSError:
        return None
    return sha if len(sha) == 40 else None


def upgrade() -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise UpdateError("uv is not on PATH; run `uv tool upgrade jotted` yourself")
    done = subprocess.run([uv, "tool", "upgrade", DIST], capture_output=True, text=True)
    if done.returncode != 0:
        raise UpdateError((done.stderr or done.stdout).strip() or f"uv exited with {done.returncode}")


def check(console, *, force: bool = False) -> bool:
    """Update if GitHub has a newer commit. True if this copy was replaced (re-run to use it)."""
    if not force and (os.environ.get(SKIP_VAR) or os.environ.get(DONE_VAR)):
        return False
    have = installed()
    if have is None:
        if force:
            console.print("This copy isn't a `uv tool install` from GitHub, so it doesn't update itself.")
        return False
    head = latest(have.repo)
    if head is None:
        if force:
            console.print("[yellow]Couldn't reach GitHub to check for updates.[/yellow]")
        return False
    if head == have.commit:
        if force:
            console.print(f"Jotted is up to date ({have.commit[:7]}).")
        return False
    console.print(f"Updating Jotted ({have.commit[:7]} → {head[:7]})…")
    try:
        upgrade()
    except UpdateError as e:
        console.print(f"[yellow]Update failed, carrying on with this version:[/yellow] {e}")
        return False
    now = installed()
    if now is None or now.commit == have.commit:
        console.print("[yellow]uv didn't pick up the new version; carrying on with this one.[/yellow]")
        return False
    console.print(f"[green]Updated to {now.commit[:7]}.[/green]")
    return True
