"""
Publish the forecast to GitHub Pages.

Builds a PUBLIC copy of the pages into docs/, commits it and pushes. GitHub
serves docs/ on the repository's Pages URL, so the address is permanent, needs
no laptop running, and updates whenever the scheduled run pushes.

WHAT MAKES THE PUBLIC BUILD DIFFERENT
-------------------------------------
It carries no credentials and no controls that cannot work:

  * the Windy map key is stripped - a world-readable page must not embed one,
    and a repository scanner would flag it regardless of whether it works;
  * the Refresh button is suppressed, because a static host has no server
    behind it and a button that always fails is worse than no button.

WHAT IS NEVER PUSHED
--------------------
local_config.py (the API keys), logs/ (your verification record, which is
yours to publish deliberately rather than by default), .cache/ and forecasts/.
The push is refused outright if a secret is found staged - see _guard().
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import config as C
from . import web

DOCS = C.ROOT / "docs"

# Patterns that must never reach a public commit. Checked against the actual
# staged content, not the filenames - a key pasted into a page is the failure
# mode that matters, and .gitignore alone would not catch it.
SECRET_PATTERNS = (
    re.compile(r"WINDY_API_KEY\s*=\s*[\"'][A-Za-z0-9]{10,}"),
    re.compile(r"WINDY_MAP_KEY\s*=\s*[\"'][A-Za-z0-9]{10,}"),
    re.compile(r"windyMapKey\"\s*:\s*\"[A-Za-z0-9]{10,}\""),
)


class PublishError(RuntimeError):
    pass


def _run(args: list[str], *, check: bool = True) -> str:
    proc = subprocess.run(args, cwd=str(C.ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=180)
    if check and proc.returncode != 0:
        raise PublishError((proc.stderr or proc.stdout or "").strip()
                           or f"{' '.join(args)} failed")
    return (proc.stdout or "").strip()


def build_public() -> list[Path]:
    """Render the public copy into docs/."""
    from dataclasses import dataclass as _dc

    daily = web.load_payload(web.DAILY_CACHE)
    weekly = web.load_payload(web.WEEKLY_CACHE)
    links = web.load_payload(C.CACHE_DIR / "links.json") or {}
    if daily is None:
        raise PublishError("No cached forecast. Run `python -m wxagent daily` "
                           "first.")

    @_dc
    class _Link:
        title: str
        url: str
        why: str

    windy = [_Link(**d) for d in links.get("windy", [])]
    models = [_Link(**d) for d in links.get("models", [])]

    DOCS.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    p = DOCS / "index.html"
    p.write_text(web.render(daily, windy, models, public=True), encoding="utf-8")
    written.append(p)

    if weekly is not None:
        p = DOCS / "mmr.html"
        p.write_text(web.render(weekly, windy, [], public=True),
                     encoding="utf-8")
        written.append(p)

    # Stops GitHub running the output through Jekyll, which would mangle any
    # file or folder beginning with an underscore.
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    written.append(DOCS / ".nojekyll")
    return written


def _guard() -> None:
    """Refuse to publish if anything staged looks like a credential."""
    staged = _run(["git", "diff", "--cached", "--name-only"], check=False)
    for name in [n for n in staged.splitlines() if n.strip()]:
        path = C.ROOT / name
        if not path.exists() or path.stat().st_size > 4_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat in SECRET_PATTERNS:
            if pat.search(text):
                raise PublishError(
                    f"Refusing to publish: {name} contains what looks like an "
                    "API key. Nothing has been pushed. Remove it, or add the "
                    "file to .gitignore, and try again.")


def publish(*, message: str | None = None, quiet: bool = False) -> str:
    """Build, commit and push. Returns a short status line."""
    if not (C.ROOT / ".git").exists():
        raise PublishError(
            "This folder is not a git repository yet. Run:\n"
            "    python -m wxagent gh-setup\n"
            "for the one-time steps.")

    written = build_public()
    if not quiet:
        print(f"  built {len(written)} public file(s) into docs/")

    _run(["git", "add", "docs"], check=False)
    _guard()

    status = _run(["git", "status", "--porcelain", "docs"], check=False)
    if not status.strip():
        return "No change since the last publish — nothing to push."

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    _run(["git", "commit", "-m", message or f"Forecast update {stamp}"])

    try:
        _run(["git", "push"])
    except PublishError as exc:
        raise PublishError(
            f"Commit made, but the push failed:\n  {exc}\n\n"
            "If this is the first push, set the remote first — "
            "`python -m wxagent gh-setup` prints the exact commands."
        ) from exc

    return f"Published at {stamp}. GitHub Pages usually updates within a minute."


SETUP = """
GitHub Pages setup — the parts only you can do
==============================================

I can't create an account or sign in on your behalf, so these three steps are
yours. Everything else is already prepared.

1. CREATE THE REPOSITORY
   Go to https://github.com/new
     Name        : kalyan-weather   (anything you like)
     Visibility  : Public           (required for free GitHub Pages)
     Do NOT tick "Add a README" — this folder already has content.

2. CONNECT THIS FOLDER AND PUSH
   In this folder, run these with YOUR username in place of USERNAME:

     git init
     git branch -M main
     git add .
     git commit -m "Kalyan weather agent"
     git remote add origin https://github.com/USERNAME/kalyan-weather.git
     git push -u origin main

   Git will ask you to sign in the first time. Use the browser prompt if it
   offers one; otherwise GitHub asks for a Personal Access Token rather than
   your password — create one at https://github.com/settings/tokens with the
   `repo` scope.

3. TURN PAGES ON
   In the repository: Settings > Pages
     Source : Deploy from a branch
     Branch : main    Folder : /docs
   Save. Your permanent address appears there after a minute:

     https://USERNAME.github.io/kalyan-weather/

AFTER THAT
   Nothing more is manual. The daily 06:15 task rebuilds the forecast and
   pushes it, so the page stays current with your laptop off and the address
   never changes.

   To publish by hand at any time:
     python -m wxagent gh-publish

WHAT WILL BE PUBLIC
   The forecast pages, and the agent's source code. Not published: your API
   keys (local_config.py), your verification log (logs/), or any cache.
   The pages name Kalyan West and the MMR sites — inherent to a local
   forecast — and nothing that identifies you personally.
"""


def setup_text() -> str:
    return SETUP
