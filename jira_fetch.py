"""
Lupiya Engineering Performance Dashboard — Jira Data Fetcher
=============================================================
Called by the GitHub Actions workflow (refresh.yml).
Reads JIRA_EMAIL + JIRA_API_TOKEN from environment variables,
fetches all relevant issues per project, then calls
inject_data() + push_to_github() from regenerate.py.

Usage (in workflow):
  env:
    JIRA_EMAIL:     ${{ secrets.JIRA_EMAIL }}
    JIRA_API_TOKEN: ${{ secrets.JIRA_API_TOKEN }}
  run: python jira_fetch.py
"""

import base64
import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

# ── Import helpers from regenerate.py ────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from regenerate import inject_data, log, STATUS_POINTS

# ── Jira credentials (injected by GitHub Actions secrets) ─────────────────────
JIRA_BASE  = "https://lupiya.atlassian.net"
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_TOKEN = os.environ.get("JIRA_API_TOKEN", "")

if not JIRA_EMAIL or not JIRA_TOKEN:
    print("ERROR: JIRA_EMAIL and JIRA_API_TOKEN must be set as environment variables.")
    sys.exit(1)

_AUTH_HEADER = "Basic " + base64.b64encode(
    f"{JIRA_EMAIL}:{JIRA_TOKEN}".encode()
).decode()

# Projects and sprint range to include
PROJECTS    = ["LW", "LWA", "MIS", "P2P", "MDE", "AP", "SP"]
SPRINT_LOOKBACK = 10   # fetch issues from the last N sprints per project


# ── Jira REST helpers ─────────────────────────────────────────────────────────
def jira_get(path: str) -> dict:
    url = f"{JIRA_BASE}/rest/api/3/{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": _AUTH_HEADER,
        "Accept":        "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def search_jira(jql: str, fields: list[str], max_total: int = 2000) -> list:
    """Page through Jira search results and return all issues."""
    all_issues = []
    start_at   = 0
    page_size  = 100
    encoded_fields = ",".join(fields)

    while True:
        encoded_jql = urllib.parse.quote(jql)
        path = (
            f"search?jql={encoded_jql}"
            f"&fields={encoded_fields}"
            f"&startAt={start_at}"
            f"&maxResults={page_size}"
        )
        data   = jira_get(path)
        issues = data.get("issues", [])
        all_issues.extend(issues)

        total     = data.get("total", 0)
        start_at += len(issues)

        log(f"  fetched {start_at}/{total} issues …")

        if start_at >= total or not issues or start_at >= max_total:
            break

    return all_issues


# ── Sprint field detection ────────────────────────────────────────────────────
def find_sprint_field(sample_issue: dict) -> str:
    """Return the custom field key that holds sprint data (usually customfield_10020)."""
    for key, val in sample_issue.get("fields", {}).items():
        if isinstance(val, list) and val:
            item = val[0]
            if isinstance(item, dict) and "sprintId" in item:
                return key
            if isinstance(item, dict) and "name" in item and "state" in item:
                return key
    return "customfield_10020"   # safe default


# ── Parse a single Jira issue ─────────────────────────────────────────────────
def parse_issue(issue: dict, sprint_field: str) -> dict | None:
    fields  = issue.get("fields", {})
    key     = issue["key"]
    project_key  = key.split("-")[0]

    # Assignee
    assignee     = fields.get("assignee") or {}
    assignee_id  = assignee.get("accountId", "unassigned")
    assignee_name = (
        assignee.get("displayName")
        or assignee.get("emailAddress", "Unassigned")
    )

    # Status → points
    status_name = (fields.get("status") or {}).get("name", "")
    points      = STATUS_POINTS.get(status_name, 0)

    # Project name
    project_obj  = fields.get("project") or {}
    project_name = project_obj.get("name", project_key)

    # Sprint — pick the most recent active/closed sprint
    sprints_raw = fields.get(sprint_field) or []
    if not isinstance(sprints_raw, list):
        sprints_raw = []

    best_sprint = None
    for s in sprints_raw:
        if not isinstance(s, dict):
            continue
        if best_sprint is None:
            best_sprint = s
        else:
            # prefer active > closed > future, then higher id
            state_rank = {"active": 2, "closed": 1, "future": 0}
            cur_rank   = state_rank.get(best_sprint.get("state", ""), -1)
            new_rank   = state_rank.get(s.get("state", ""), -1)
            if new_rank > cur_rank or (
                new_rank == cur_rank
                and s.get("id", 0) > best_sprint.get("id", 0)
            ):
                best_sprint = s

    if best_sprint is None:
        return None   # skip unsprinted issues

    sprint_id    = best_sprint.get("id") or best_sprint.get("sprintId", 0)
    sprint_name  = best_sprint.get("name", "")
    sprint_state = best_sprint.get("state", "")

    # Due date
    due_date = fields.get("duedate")   # "2025-12-04" or None

    return {
        "key":           key,
        "summary":       fields.get("summary", ""),
        "status":        status_name,
        "assignee_id":   assignee_id,
        "assignee_name": assignee_name,
        "project_key":   project_key,
        "project_name":  project_name,
        "issue_type":    (fields.get("issuetype") or {}).get("name", "Task"),
        "sprint_id":     sprint_id,
        "sprint_name":   sprint_name,
        "sprint_state":  sprint_state,
        "points":        points,
        "due_date":      due_date,
    }


# ── Main fetch ────────────────────────────────────────────────────────────────
def main():
    log("=== Jira fetch started ===")

    all_records  = []
    all_assignees = {}
    all_sprints  = {}   # name -> {id, name, state}
    sprint_field = "customfield_10020"

    for project in PROJECTS:
        log(f"Fetching project: {project}")
        jql = (
            f'project = "{project}" '
            f'AND sprint IS NOT EMPTY '
            f'AND sprint in (openSprints(), closedSprints()) '
            f'ORDER BY updated DESC'
        )
        fields = [
            "summary", "status", "assignee", "project",
            "issuetype", "duedate", sprint_field,
        ]

        issues = search_jira(jql, fields)
        log(f"  → {len(issues)} raw issues for {project}")

        # Auto-detect sprint field from first issue
        if issues and sprint_field == "customfield_10020":
            sprint_field = find_sprint_field(issues[0])
            # Re-request with the correct field name if different
            if sprint_field != "customfield_10020":
                fields = [
                    "summary", "status", "assignee", "project",
                    "issuetype", "duedate", sprint_field,
                ]
                issues = search_jira(jql, fields)

        for raw in issues:
            record = parse_issue(raw, sprint_field)
            if record is None:
                continue

            all_records.append(record)

            # Accumulate assignees
            aid = record["assignee_id"]
            if aid and aid != "unassigned":
                all_assignees[aid] = record["assignee_name"]

            # Accumulate unique sprints (keep highest-id entry per name)
            sname = record["sprint_name"]
            sid   = record["sprint_id"]
            if sname:
                if sname not in all_sprints or sid > all_sprints[sname]["id"]:
                    all_sprints[sname] = {
                        "id":    sid,
                        "name":  sname,
                        "state": record["sprint_state"],
                    }

    log(f"Total records: {len(all_records)}")
    log(f"Total assignees: {len(all_assignees)}")
    log(f"Total sprints: {len(all_sprints)}")

    if not all_records:
        log("ERROR: No records fetched. Aborting to avoid wiping dashboard.")
        sys.exit(1)

    # inject_data expects sprints as a list or dict
    sprint_list = list(all_sprints.values())

    inject_data(all_records, all_assignees, sprint_list)
    log("=== Jira fetch complete ===")


if __name__ == "__main__":
    main()
