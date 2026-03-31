"""
Lupiya Engineering Performance Dashboard — Regeneration Script
==============================================================
Fetches live Jira data, injects it into template.html, and pushes
to GitHub Pages. Designed to run from within the cloned repo directory.

Usage:
  cd /path/to/engineering-performance-dashboard
  python3 regenerate.py

Credentials:
  Reads GitHub token from .deploy_config.json (NOT committed to repo).
  Reads Jira credentials from the active MCP session (Claude scheduled task).
"""

import hashlib
import json
import os
import re
import subprocess
import shutil
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
REPO_DIR      = Path(__file__).parent
TEMPLATE_FILE = REPO_DIR / "template.html"
OUTPUT_FILE   = REPO_DIR / "index.html"
LOG_FILE      = REPO_DIR / "refresh.log"
CONFIG_FILE   = Path("/sessions/wizardly-serene-bell/mnt/Lupiya Projects Workspace/.deploy_config.json")

PROJECTS = ["LW", "LWA", "MIS", "P2P", "MDE", "AP", "SP"]
STATUS_POINTS = {
    "Done": 4, "Done / Prod Deployed": 4,
    "QA / TestFlight / Review": 2, "QA / review": 2, "pending deployment": 3,
    "In Progress": 1, "To Do": 0,
}

# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg):
    ts   = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# ── Data injection ────────────────────────────────────────────────────────────
def inject_data(records, assignees, sprints):
    """Replace embedded JSON data + timestamp in template.html → index.html."""
    if not TEMPLATE_FILE.exists():
        raise FileNotFoundError(f"template.html not found at {TEMPLATE_FILE}")

    html = TEMPLATE_FILE.read_text(encoding="utf-8")
    gen_time = datetime.now().strftime("%A, %d %B %Y — %I:%M %p")

    # Build unique sprint list
    seen = {}
    for s in sprints if isinstance(sprints, list) else sprints.values():
        name = s["name"]
        if name not in seen or s["id"] > seen[name]["id"]:
            seen[name] = s
    sprint_list = sorted(seen.values(), key=lambda s: s["id"])

    # Replace embedded data
    _raw_json = json.dumps(records)
    html = re.sub(
        r'const RAW\s*=\s*\[.*?\];',
        lambda _: f'const RAW   = {_raw_json};',
        html, flags=re.DOTALL
    )
    _asgn_json = json.dumps(assignees)
    html = re.sub(
        r'const ASGN\s*=\s*\{.*?\};',
        lambda _: f'const ASGN  = {_asgn_json};',
        html, flags=re.DOTALL
    )
    _sprts_json = json.dumps(sprint_list)
    html = re.sub(
        r'const SPRTS\s*=\s*\[.*?\];',
        lambda _: f'const SPRTS = {_sprts_json};',
        html, flags=re.DOTALL
    )

    # Replace generation timestamp
    _gen_time = gen_time
    html = re.sub(
        r'Last generated:.*?</div>',
        lambda _: f'Last generated: {_gen_time}</div>',
        html
    )


    # Inject password hash from env var
    import hashlib as _hl, os as _os
    _raw_pw   = _os.environ.get("DASHBOARD_PASSWORD", "")
    _pw_hash  = _hl.sha256(_raw_pw.encode()).hexdigest() if _raw_pw else ""
    html = html.replace('"PLACEHOLDER_HASH"', f'"{_pw_hash}"')

    OUTPUT_FILE.write_text(html, encoding="utf-8")
    log(f"index.html rebuilt: {len(html):,} bytes")
    return len(html)


# ── GitHub push ───────────────────────────────────────────────────────────────
def push_to_github():
    if not CONFIG_FILE.exists():
        log("⚠  .deploy_config.json not found — skipping push.")
        return

    cfg      = json.loads(CONFIG_FILE.read_text())
    token    = cfg["github_token"]
    username = cfg["github_username"]
    repo     = cfg["github_repo"]

    remote = f"https://{username}:{token}@github.com/{username}/{repo}.git"
    subprocess.run(["git", "remote", "set-url", "origin", remote],
                   cwd=REPO_DIR, check=True)
    subprocess.run(["git", "config", "user.email", "joshua.chilomo@lupiya.com"],
                   cwd=REPO_DIR, check=True)
    subprocess.run(["git", "config", "user.name",  "Joshua Chilomo"],
                   cwd=REPO_DIR, check=True)
    subprocess.run(["git", "add", "index.html"],
                   cwd=REPO_DIR, check=True)

    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO_DIR)
    if diff.returncode == 0:
        log("No changes to push — GitHub Pages already up to date.")
        return

    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    subprocess.run(
        ["git", "commit", "-q", "-m", f"Auto-deploy: {ts}"],
        cwd=REPO_DIR, check=True
    )
    subprocess.run(["git", "push", "-q", "origin", "main"],
                   cwd=REPO_DIR, check=True)
    log(f"✅ Pushed to https://{username.lower()}.github.io/{repo}/")


# ── Entry point (called by Claude scheduled tasks) ────────────────────────────
if __name__ == "__main__":
    log("=== Regeneration started ===")
    log("This script is called by Claude scheduled tasks with fresh Jira data.")
    log("Pass records/assignees/sprints via inject_data() then call push_to_github().")
    log("=== Done ===")
