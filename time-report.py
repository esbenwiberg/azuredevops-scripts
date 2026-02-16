#!/usr/bin/env python3
"""
Time Registration Report

Gathers work activity from multiple sources and outputs a per-day summary
for time registration.

Sources:
  1. Azure DevOps PRs (created/completed per day)
  2. Claude Code session history (projects worked on per day)
  3. MS365 Calendar events (optional, requires Calendars.Read consent)

Usage:
    # Last 2 weeks (default)
    python3 scripts/time-report.py

    # Specific date range
    python3 scripts/time-report.py --from 2026-02-01 --to 2026-02-15

    # Single day
    python3 scripts/time-report.py --date 2026-02-11

    # With calendar (requires prior: az login --scope "https://graph.microsoft.com/Calendars.Read")
    python3 scripts/time-report.py --calendar

    # Specific DevOps project(s)
    python3 scripts/time-report.py --project "TeamPlanner - V3"

    # All DevOps projects
    python3 scripts/time-report.py --all-projects

    # Output as JSON
    python3 scripts/time-report.py --json
"""

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ── Azure DevOps ──────────────────────────────────────────────────────────────


def run_az(args: list[str], timeout: int = 30) -> str:
    result = subprocess.run(
        ["az"] + args, capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:200])
    return result.stdout.strip()


def get_devops_defaults() -> dict:
    output = run_az(["devops", "configure", "--list"])
    defaults = {}
    for line in output.splitlines():
        if "=" in line and not line.startswith("["):
            key, _, val = line.partition("=")
            defaults[key.strip()] = val.strip()
    return defaults


def fetch_devops_prs(org: str, project: str, status: str,
                     creator: str | None = None) -> list[dict]:
    args = [
        "repos", "pr", "list", "--status", status,
        "--top", "200", "--org", org, "--project", project, "-o", "json",
    ]
    if creator:
        args += ["--creator", creator]
    try:
        output = run_az(args, timeout=30)
        return json.loads(output) if output else []
    except (RuntimeError, json.JSONDecodeError):
        return []


def list_devops_projects(org: str) -> list[str]:
    output = run_az(["devops", "project", "list", "--org", org,
                      "-o", "json", "--top", "500"], timeout=30)
    data = json.loads(output) if output else {}
    return [p["name"] for p in data.get("value", [])]


def get_devops_activity(org: str, projects: list[str], creator: str,
                        start: datetime, end: datetime) -> dict[str, list[dict]]:
    """Returns {date_str: [pr_summaries]} for PRs created or closed in range."""
    daily = defaultdict(list)

    for project in projects:
        for status in ["completed", "active", "abandoned"]:
            prs = fetch_devops_prs(org, project, status, creator=creator)
            for pr in prs:
                repo_name = pr.get("repository", {}).get("name", "?")
                pr_id = pr["pullRequestId"]
                title = pr.get("title", "Untitled")
                pr_status = pr.get("status", "?")
                target = (pr.get("targetRefName") or "").replace("refs/heads/", "")

                # Check created date
                created_str = pr.get("creationDate", "")
                created_dt = _parse_iso(created_str)
                if created_dt and start <= created_dt <= end:
                    date_key = created_dt.strftime("%Y-%m-%d")
                    daily[date_key].append({
                        "type": "pr_created",
                        "project": project,
                        "repo": repo_name,
                        "pr_id": pr_id,
                        "title": title,
                        "status": pr_status,
                        "target": target,
                    })

                # Check closed date (for completed PRs merged on a different day)
                closed_str = pr.get("closedDate", "")
                closed_dt = _parse_iso(closed_str)
                if closed_dt and start <= closed_dt <= end:
                    closed_key = closed_dt.strftime("%Y-%m-%d")
                    if closed_key != (created_dt.strftime("%Y-%m-%d") if created_dt else ""):
                        daily[closed_key].append({
                            "type": "pr_completed",
                            "project": project,
                            "repo": repo_name,
                            "pr_id": pr_id,
                            "title": title,
                            "target": target,
                        })

    return dict(daily)


# ── Claude Code History ───────────────────────────────────────────────────────


def get_claude_activity(start: datetime, end: datetime) -> dict[str, list[dict]]:
    """Parse ~/.claude/history.jsonl for session activity per day."""
    history_file = Path.home() / ".claude" / "history.jsonl"
    if not history_file.exists():
        return {}

    daily = defaultdict(list)
    sessions_by_day = defaultdict(lambda: defaultdict(set))

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    for line in history_file.read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        ts = entry.get("timestamp")
        if not isinstance(ts, (int, float)):
            continue
        if ts < start_ms or ts > end_ms:
            continue

        project = entry.get("project", "")
        display = entry.get("display", "")
        session_id = entry.get("sessionId", "")
        date_key = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

        # Extract project short name from path
        # Worktree paths like /home/ewi/.orcha/worktrees/TeamPlanner/session-1-xxxx
        # should resolve to "TeamPlanner", not the session dir name
        if project:
            p = Path(project)
            project_name = p.name
            # If it looks like a worktree session, use the parent dir name
            if (project_name.startswith("session-") or
                    project_name.startswith("pl-") or
                    project_name.startswith("HIVE-")):
                project_name = p.parent.name
            sessions_by_day[date_key][project_name].add(session_id)

    # Summarize per day
    for date_key, projects in sessions_by_day.items():
        for project_name, session_ids in projects.items():
            daily[date_key].append({
                "type": "claude_session",
                "project": project_name,
                "sessions": len(session_ids),
            })

    return dict(daily)


# ── MS365 Calendar ────────────────────────────────────────────────────────────


def get_calendar_activity(start: datetime, end: datetime) -> dict[str, list[dict]]:
    """Fetch calendar events via Microsoft Graph (requires Calendars.Read)."""
    start_str = start.strftime("%Y-%m-%dT00:00:00Z")
    end_str = end.strftime("%Y-%m-%dT23:59:59Z")

    try:
        url = (
            f"https://graph.microsoft.com/v1.0/me/calendarView"
            f"?startDateTime={start_str}&endDateTime={end_str}"
            f"&$select=subject,start,end,isAllDay,organizer,showAs,isCancelled"
            f"&$orderby=start/dateTime&$top=200"
        )
        output = run_az(["rest", "--method", "GET", "--url", url,
                          "--headers", "Content-Type=application/json"], timeout=30)
        data = json.loads(output)
    except (RuntimeError, json.JSONDecodeError) as e:
        print(f"  Calendar fetch failed: {e}", file=sys.stderr)
        print("  Tip: run 'az login --scope \"https://graph.microsoft.com/Calendars.Read\"'",
              file=sys.stderr)
        return {}

    daily = defaultdict(list)
    for event in data.get("value", []):
        if event.get("isCancelled"):
            continue
        subject = event.get("subject", "(no subject)")
        show_as = event.get("showAs", "busy")
        if show_as == "free":
            continue

        start_info = event.get("start", {})
        end_info = event.get("end", {})
        event_start = start_info.get("dateTime", "")[:16]
        event_end = end_info.get("dateTime", "")[:16]
        is_all_day = event.get("isAllDay", False)

        # Determine the date
        date_key = event_start[:10]

        organizer = ""
        org_info = event.get("organizer", {})
        if org_info:
            organizer = org_info.get("emailAddress", {}).get("name", "")

        daily[date_key].append({
            "type": "calendar",
            "subject": subject,
            "start": event_start[11:] if not is_all_day else "all-day",
            "end": event_end[11:] if not is_all_day else "",
            "organizer": organizer,
        })

    return dict(daily)


# ── Git Commits ───────────────────────────────────────────────────────────────


def get_git_activity(start: datetime, end: datetime) -> dict[str, list[dict]]:
    """Scan known repo locations for git commits in the date range."""
    daily = defaultdict(list)
    commits_by_day = defaultdict(lambda: defaultdict(int))

    # Find repos
    repos_dir = Path.home() / "repos"
    repo_paths = []

    if repos_dir.exists():
        for d in repos_dir.iterdir():
            if d.is_dir() and (d / ".git").exists():
                repo_paths.append(d)
            # Check for nested repos (e.g. orcha-clones/)
            if d.is_dir():
                for sub in d.iterdir():
                    if sub.is_dir() and (sub / ".git").exists():
                        repo_paths.append(sub)

    # Also check common worktree locations
    for wt_parent in [Path.home() / "orcha-worktrees", Path.home() / "hive-repos"]:
        if wt_parent.exists():
            for d in wt_parent.iterdir():
                if d.is_dir() and ((d / ".git").exists() or (d / ".git").is_file()):
                    repo_paths.append(d)

    start_str = start.strftime("%Y-%m-%d")
    end_str = (end + timedelta(days=1)).strftime("%Y-%m-%d")

    for repo in repo_paths:
        repo_name = repo.name
        try:
            result = subprocess.run(
                ["git", "-C", str(repo), "log", "--all",
                 f"--after={start_str}", f"--before={end_str}",
                 "--format=%aI|%s", "--author-date-order"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                continue
            for line in result.stdout.strip().splitlines():
                if "|" not in line:
                    continue
                date_part, _, msg = line.partition("|")
                date_key = date_part[:10]
                commits_by_day[date_key][repo_name] += 1
        except (subprocess.TimeoutExpired, Exception):
            continue

    for date_key, repos in commits_by_day.items():
        for repo_name, count in repos.items():
            daily[date_key].append({
                "type": "git_commit",
                "repo": repo_name,
                "commits": count,
            })

    return dict(daily)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def merge_daily(sources: list[dict[str, list[dict]]]) -> dict[str, list[dict]]:
    merged = defaultdict(list)
    for source in sources:
        for date_key, items in source.items():
            merged[date_key].extend(items)
    return dict(merged)


# ── Output Formatters ─────────────────────────────────────────────────────────


def format_text(daily: dict[str, list[dict]], start: datetime, end: datetime) -> str:
    lines = []
    lines.append(f"Time Report: {start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')}")
    lines.append("=" * 70)

    d = start.date() if hasattr(start, 'date') else start
    end_d = end.date() if hasattr(end, 'date') else end

    while d <= end_d:
        date_str = d.strftime("%Y-%m-%d")
        day_name = WEEKDAYS[d.weekday()]
        items = daily.get(date_str, [])

        if not items:
            lines.append(f"\n{date_str} ({day_name})  --")
            d += timedelta(days=1)
            continue

        lines.append(f"\n{date_str} ({day_name})")

        # Group by type
        prs_created = [i for i in items if i["type"] == "pr_created"]
        prs_completed = [i for i in items if i["type"] == "pr_completed"]
        calendar = [i for i in items if i["type"] == "calendar"]
        claude = [i for i in items if i["type"] == "claude_session"]
        git = [i for i in items if i["type"] == "git_commit"]

        # Calendar events
        for evt in calendar:
            time_str = evt["start"]
            if evt["end"]:
                time_str += f"-{evt['end']}"
            lines.append(f"  CAL  {time_str:16s} {evt['subject']}")

        # PRs
        for pr in prs_created:
            status_tag = f"[{pr['status']}]" if pr["status"] != "completed" else ""
            target_tag = f" -> {pr['target']}" if pr.get("target") and pr["target"] != "main" else ""
            lines.append(f"  PR   #{pr['pr_id']:<6d} {pr['title'][:60]}{target_tag} {status_tag}")

        for pr in prs_completed:
            lines.append(f"  PR   #{pr['pr_id']:<6d} (merged) {pr['title'][:55]}")

        # Claude Code sessions
        for session in sorted(claude, key=lambda s: s["sessions"], reverse=True):
            lines.append(f"  CODE {session['project']:30s} ({session['sessions']} session{'s' if session['sessions'] != 1 else ''})")

        # Git commits (only show if no Claude session for same project)
        claude_projects = {s["project"] for s in claude}
        for g in sorted(git, key=lambda x: x["commits"], reverse=True):
            if g["repo"] not in claude_projects:
                lines.append(f"  GIT  {g['repo']:30s} ({g['commits']} commit{'s' if g['commits'] != 1 else ''})")

        d += timedelta(days=1)

    # Summary
    lines.append("\n" + "=" * 70)
    all_prs = [i for items in daily.values() for i in items if i["type"] == "pr_created"]
    all_projects = sorted(set(i.get("project", "") for i in all_prs if i.get("project")))
    lines.append(f"Total PRs created: {len(all_prs)}")
    if all_projects:
        lines.append(f"Projects: {', '.join(all_projects)}")

    all_claude = [i for items in daily.values() for i in items if i["type"] == "claude_session"]
    claude_projects = sorted(set(i["project"] for i in all_claude))
    if claude_projects:
        lines.append(f"Claude Code projects: {', '.join(claude_projects)}")

    working_days = sum(1 for d_str, items in daily.items()
                       if items and datetime.strptime(d_str, "%Y-%m-%d").weekday() < 5)
    lines.append(f"Working days with activity: {working_days}")

    return "\n".join(lines)


def format_json(daily: dict[str, list[dict]], start: datetime, end: datetime) -> str:
    return json.dumps({
        "range": {"from": start.strftime("%Y-%m-%d"), "to": end.strftime("%Y-%m-%d")},
        "days": daily,
    }, indent=2, default=str)


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Time Registration Report")
    parser.add_argument("--from", dest="from_date", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="to_date", help="End date (YYYY-MM-DD)")
    parser.add_argument("--date", help="Single date (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, default=14, help="Look back N days (default: 14)")
    parser.add_argument("--project", help="DevOps project(s), comma-separated")
    parser.add_argument("--all-projects", action="store_true", help="Scan all DevOps projects")
    parser.add_argument("--calendar", action="store_true", help="Include MS365 calendar")
    parser.add_argument("--no-devops", action="store_true", help="Skip Azure DevOps PRs")
    parser.add_argument("--no-claude", action="store_true", help="Skip Claude Code history")
    parser.add_argument("--no-git", action="store_true", help="Skip git commit history")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of text")
    args = parser.parse_args()

    # Determine date range
    now = datetime.now(timezone.utc)
    if args.date:
        start = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = start.replace(hour=23, minute=59, second=59)
    elif args.from_date:
        start = datetime.strptime(args.from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if args.to_date:
            end = datetime.strptime(args.to_date, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc)
        else:
            end = now
    else:
        start = now - timedelta(days=args.days)
        end = now

    print(f"Gathering activity: {start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')}",
          file=sys.stderr)

    sources = []

    # 1. Azure DevOps PRs
    if not args.no_devops:
        try:
            defaults = get_devops_defaults()
            org = defaults.get("organization", "")
            if not org:
                print("  DevOps: no org configured, skipping", file=sys.stderr)
            else:
                creator = run_az(["account", "show", "--query", "user.name", "-o", "tsv"])
                if args.project:
                    projects = [p.strip() for p in args.project.split(",")]
                elif args.all_projects:
                    projects = list_devops_projects(org)
                    print(f"  DevOps: found {len(projects)} projects", file=sys.stderr)
                else:
                    proj = defaults.get("project", "")
                    projects = [proj] if proj else []

                if projects:
                    print(f"  DevOps: fetching PRs by {creator}...", file=sys.stderr)
                    devops = get_devops_activity(org, projects, creator, start, end)
                    pr_count = sum(len(v) for v in devops.values())
                    print(f"  DevOps: {pr_count} PR events", file=sys.stderr)
                    sources.append(devops)
        except Exception as e:
            print(f"  DevOps: error - {e}", file=sys.stderr)

    # 2. Claude Code history
    if not args.no_claude:
        print("  Claude: scanning history...", file=sys.stderr)
        claude = get_claude_activity(start, end)
        session_count = sum(len(v) for v in claude.values())
        print(f"  Claude: {session_count} project-days", file=sys.stderr)
        sources.append(claude)

    # 3. MS365 Calendar
    if args.calendar:
        print("  Calendar: fetching events...", file=sys.stderr)
        cal = get_calendar_activity(start, end)
        event_count = sum(len(v) for v in cal.values())
        print(f"  Calendar: {event_count} events", file=sys.stderr)
        sources.append(cal)

    # 4. Git commits
    if not args.no_git:
        print("  Git: scanning repos...", file=sys.stderr)
        git = get_git_activity(start, end)
        commit_count = sum(len(v) for v in git.values())
        print(f"  Git: {commit_count} repo-days", file=sys.stderr)
        sources.append(git)

    # Merge and output
    daily = merge_daily(sources)

    if args.json:
        print(format_json(daily, start, end))
    else:
        print(format_text(daily, start, end))


if __name__ == "__main__":
    main()
