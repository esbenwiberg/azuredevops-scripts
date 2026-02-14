#!/usr/bin/env python3
"""
Azure DevOps PR Report Generator

Fetches pull requests from Azure DevOps and generates a standalone HTML report
with links, details, file changes, and user comparison.

Usage:
    # Single user, single project
    python3 scripts/devops-pr-report.py --user EMAIL --project NAME [--days N]

    # All users, all projects
    python3 scripts/devops-pr-report.py --all [--days N]

    # All users, specific projects
    python3 scripts/devops-pr-report.py --all --project "Proj1,Proj2" [--days N]

Defaults:
    - user: current az account user
    - days: 30
    - org/project: from az devops defaults
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path


def run_az(args: list[str], timeout: int = 30) -> str:
    """Run an az CLI command and return stdout."""
    result = subprocess.run(
        ["az"] + args,
        capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        raise RuntimeError(f"az {' '.join(args[:4])}... failed: {result.stderr.strip()[:200]}")
    return result.stdout.strip()


def get_defaults() -> dict:
    output = run_az(["devops", "configure", "--list"])
    defaults = {}
    for line in output.splitlines():
        if "=" in line and not line.startswith("["):
            key, _, val = line.partition("=")
            defaults[key.strip()] = val.strip()
    return defaults


def get_token() -> str:
    return run_az([
        "account", "get-access-token",
        "--resource", "499b84ac-1321-427f-aa17-267ca6975798",
        "--query", "accessToken", "-o", "tsv"
    ])


def api_get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except (urllib.error.HTTPError, urllib.error.URLError):
        return {}


def list_projects(org: str) -> list[dict]:
    """List all projects in the organization."""
    output = run_az([
        "devops", "project", "list",
        "--org", org, "-o", "json",
        "--top", "500",
    ], timeout=30)
    data = json.loads(output) if output else {}
    return data.get("value", [])


def fetch_prs_for_project(org: str, project: str, status: str,
                          creator: str | None = None, top: int = 200) -> list[dict]:
    """Fetch PRs for a project/status, optionally filtered by creator."""
    args = [
        "repos", "pr", "list",
        "--status", status,
        "--top", str(top),
        "--org", org,
        "--project", project,
        "-o", "json",
    ]
    if creator:
        args += ["--creator", creator]
    try:
        output = run_az(args, timeout=30)
        return json.loads(output) if output else []
    except (RuntimeError, json.JSONDecodeError):
        return []


def fetch_pr_changes(org: str, project_id: str, repo_id: str, pr_id: int, token: str) -> list[dict]:
    url = f"{org}/{project_id}/_apis/git/repositories/{repo_id}/pullRequests/{pr_id}/iterations?api-version=7.1"
    iterations = api_get(url, token)
    if not iterations.get("value"):
        return []
    last_iter = iterations["value"][-1]["id"]
    url = f"{org}/{project_id}/_apis/git/repositories/{repo_id}/pullRequests/{pr_id}/iterations/{last_iter}/changes?api-version=7.1"
    changes = api_get(url, token)
    return changes.get("changeEntries", [])


def fetch_diff_stats(org: str, project_id: str, repo_id: str,
                     source_commit: str, target_commit: str, token: str) -> dict:
    if not source_commit or not target_commit:
        return {}
    url = (
        f"{org}/{project_id}/_apis/git/repositories/{repo_id}/diffs/commits"
        f"?baseVersionType=commit&baseVersion={target_commit}"
        f"&targetVersionType=commit&targetVersion={source_commit}"
        f"&api-version=7.1"
    )
    data = api_get(url, token)
    return data.get("changeCounts", {})


def format_date(iso_str: str | None) -> str:
    if not iso_str:
        return "\u2014"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y %H:%M")
    except (ValueError, AttributeError):
        return iso_str[:16] if iso_str else "\u2014"


def days_ago(iso_str: str | None) -> str:
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        if delta.days == 0:
            hours = delta.seconds // 3600
            return f"{hours}h ago" if hours > 0 else "just now"
        if delta.days == 1:
            return "yesterday"
        return f"{delta.days}d ago"
    except (ValueError, AttributeError):
        return ""


def branch_name(ref: str | None) -> str:
    if not ref:
        return "\u2014"
    return ref.replace("refs/heads/", "")


def pr_url(org: str, project: str, repo_name: str, pr_id: int) -> str:
    return f"{org}/{project}/_git/{repo_name}/pullrequest/{pr_id}"


def status_badge(status: str) -> str:
    colors = {"active": "#0078d4", "completed": "#107c10", "abandoned": "#a80000"}
    color = colors.get(status, "#666")
    return f'<span class="badge" style="background:{color}">{escape(status)}</span>'


# ── HTML generation ──────────────────────────────────────────────────────────


def build_pr_card(pr: dict) -> str:
    """Build a single PR card HTML."""
    # Files
    files_html = ""
    if pr.get("files"):
        file_rows = []
        for f in pr["files"]:
            icon = {"add": "+", "edit": "~", "delete": "\u2212"}.get(f["type"], "?")
            cls = {"add": "file-add", "edit": "file-edit", "delete": "file-delete"}.get(f["type"], "")
            file_rows.append(
                f'<tr><td class="file-change {cls}">{icon}</td>'
                f'<td class="file-path">{escape(f["path"])}</td></tr>'
            )
        n = len(pr["files"])
        files_html = (
            f'<details class="files-section">'
            f'<summary>{n} file{"s" if n != 1 else ""} changed</summary>'
            f'<table class="file-table">{"".join(file_rows)}</table></details>'
        )

    # Diff stats
    parts = []
    diff = pr.get("diff_stats", {})
    if diff.get("Add"):
        parts.append(f'<span class="stat-add">+{diff["Add"]} added</span>')
    if diff.get("Edit"):
        parts.append(f'<span class="stat-edit">~{diff["Edit"]} modified</span>')
    if diff.get("Delete"):
        parts.append(f'<span class="stat-del">\u2212{diff["Delete"]} deleted</span>')
    stats_html = " ".join(parts)

    # Reviewers
    reviewers_html = ""
    if pr.get("reviewers"):
        items = []
        for r in pr["reviewers"]:
            v = r.get("vote", 0)
            if v == 10:
                icon, cls = "&#10003;", "vote-approved"
            elif v == 5:
                icon, cls = "&#10003;", "vote-approved-suggest"
            elif v == -5:
                icon, cls = "&#8265;", "vote-wait"
            elif v == -10:
                icon, cls = "&#10007;", "vote-rejected"
            else:
                icon, cls = "&#8226;", "vote-none"
            items.append(f'<span class="reviewer {cls}" title="vote: {v}">{icon} {escape(r["name"])}</span>')
        reviewers_html = f'<div class="reviewers">{"".join(items)}</div>'

    # Description
    desc = pr.get("description", "") or ""
    if len(desc) > 400:
        desc = desc[:400] + "..."
    desc_html = f'<div class="pr-desc">{escape(desc)}</div>' if desc else ""

    # Work items
    wi_html = ""
    if pr.get("work_items"):
        wi_html = f'<div class="work-items">Work items: {", ".join("#" + w for w in pr["work_items"])}</div>'

    closed_span = ""
    if pr.get("closed") and pr["closed"] != "\u2014":
        closed_span = f'<span>Closed: {pr["closed"]} ({pr["closed_ago"]})</span>'

    creator_tag = ""
    if pr.get("creator_name"):
        creator_tag = f'<span class="pr-creator">{escape(pr["creator_name"])}</span>'

    return f"""
    <div class="pr-card" data-status="{escape(pr['status'])}"
         data-repo="{escape(pr['repo_name'])}"
         data-user="{escape(pr.get('creator_email', ''))}"
         data-project="{escape(pr.get('project_name', ''))}">
        <div class="pr-header">
            <a href="{escape(pr['url'])}" target="_blank" class="pr-title">{escape(pr['title'])}</a>
            <div class="pr-meta">
                {status_badge(pr['status'])}
                <span class="pr-id">#{pr['pr_id']}</span>
                <span class="pr-repo">{escape(pr['repo_name'])}</span>
                {creator_tag}
            </div>
        </div>
        <div class="pr-details">
            <div class="pr-branches">
                <code>{escape(pr['source_branch'])}</code> &rarr; <code>{escape(pr['target_branch'])}</code>
            </div>
            <div class="pr-dates">
                <span>Created: {pr['created']} ({pr['created_ago']})</span>
                {closed_span}
            </div>
            {reviewers_html}
            {desc_html}
            {wi_html}
            <div class="pr-stats">{stats_html}</div>
            {files_html}
        </div>
    </div>"""


def _anthropic_api_get(url: str, api_key: str) -> dict | None:
    """Fetch a single page from the Anthropic admin API."""
    req = urllib.request.Request(url, headers={
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        print(f"  Anthropic API error {e.code}: {body[:200]}", file=sys.stderr)
        return None


def fetch_api_keys(api_key: str) -> dict[str, str]:
    """Fetch API key list and return {key_id: key_name} mapping."""
    from urllib.parse import urlencode
    key_map = {}
    url = f"https://api.anthropic.com/v1/organizations/api_keys?limit=100"
    while url:
        data = _anthropic_api_get(url, api_key)
        if not data:
            break
        for k in data.get("data", []):
            key_map[k["id"]] = k["name"]
        if data.get("has_more") and data.get("last_id"):
            url = f"https://api.anthropic.com/v1/organizations/api_keys?limit=100&after_id={data['last_id']}"
        else:
            url = None
    return key_map


def extract_user_from_keyname(name: str) -> str:
    """Extract user initials from API key name.

    Patterns:
      claude_code_key_alice_xxxx  -> alice
      alice-my-org                -> alice
      bob-key                     -> bob
      carol-github-action         -> carol
      DaveM                       -> davem
    """
    name_lower = name.lower().strip()

    # Pattern: claude_code_key_{user}_{random}
    m = re.match(r"claude_code_key_([a-z]+)_", name_lower)
    if m:
        return m.group(1)

    # Pattern: {user}-key, {user}-something
    m = re.match(r"([a-z]{2,6})[-_]", name_lower)
    if m:
        return m.group(1)

    # Fallback: whole name if short enough
    if re.match(r"^[a-z]{2,6}$", name_lower):
        return name_lower

    return name_lower[:10]


def map_keys_to_people(key_map: dict[str, str], prs_data: list[dict]) -> dict:
    """Map API key IDs to people by matching initials to PR author emails.

    Returns: {
        user_initials: {
            "display_name": str,
            "key_ids": set[str],
            "email": str or None,
        }
    }
    """
    # Group keys by extracted user initials
    user_keys = {}  # initials -> set of key_ids
    for key_id, key_name in key_map.items():
        initials = extract_user_from_keyname(key_name)
        if initials not in user_keys:
            user_keys[initials] = set()
        user_keys[initials].add(key_id)

    # Build email prefix -> (email, display_name) from PR data
    email_map = {}  # prefix -> (email, display_name)
    for pr in prs_data:
        email = pr.get("creator_email", "")
        name = pr.get("creator_name", "")
        if email:
            prefix = email.split("@")[0].lower()
            email_map[prefix] = (email, name)

    # Match initials to PR authors
    people = {}
    for initials, key_ids in user_keys.items():
        display_name = initials.upper()
        email = None
        if initials in email_map:
            email, display_name = email_map[initials]
        people[initials] = {
            "display_name": display_name,
            "key_ids": key_ids,
            "email": email,
        }

    return people


def fetch_anthropic_usage(api_key: str, days: int) -> list[dict]:
    """Fetch daily token usage from Anthropic admin API, grouped by api_key_id and model."""
    from urllib.parse import urlencode

    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    end = datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:59Z")

    all_buckets = []
    base_url = "https://api.anthropic.com/v1/organizations/usage_report/messages"
    # Group by both api_key_id and model so we can calculate accurate per-model costs
    params_list = [
        ("starting_at", start),
        ("ending_at", end),
        ("bucket_width", "1d"),
        ("group_by[]", "api_key_id"),
        ("group_by[]", "model"),
        ("limit", "31"),
    ]
    url = f"{base_url}?{urlencode(params_list)}"

    while url:
        data = _anthropic_api_get(url, api_key)
        if not data:
            break
        all_buckets.extend(data.get("data", []))
        if data.get("has_more") and data.get("next_page"):
            params_list_next = [p for p in params_list if p[0] != "page"]
            params_list_next.append(("page", data["next_page"]))
            url = f"{base_url}?{urlencode(params_list_next)}"
        else:
            url = None

    return all_buckets


# Per-million token pricing (USD) by model family
_MODEL_PRICING = {
    # model_prefix: (input_per_M, cache_read_per_M, cache_write_per_M, output_per_M)
    "claude-opus-4":    (15.00,  1.50,  18.75,  75.00),
    "claude-sonnet-4":  ( 3.00,  0.30,   3.75,  15.00),
    "claude-haiku-4":   ( 0.80,  0.08,   1.00,   4.00),
    # Fallback for unknown models
    "_default":         ( 3.00,  0.30,   3.75,  15.00),
}


def _get_pricing(model: str) -> tuple:
    """Get pricing tuple for a model name."""
    model_lower = (model or "").lower()
    for prefix, pricing in _MODEL_PRICING.items():
        if prefix != "_default" and model_lower.startswith(prefix):
            return pricing
    return _MODEL_PRICING["_default"]


def _calc_cost(result: dict) -> float:
    """Calculate USD cost for a usage result based on model pricing."""
    model = result.get("model") or ""
    inp_price, cache_read_price, cache_write_price, out_price = _get_pricing(model)

    uncached_input = result.get("uncached_input_tokens", 0)
    cache_read = result.get("cache_read_input_tokens", 0)
    output = result.get("output_tokens", 0)
    cc = result.get("cache_creation", {})
    cache_write = cc.get("ephemeral_5m_input_tokens", 0) + cc.get("ephemeral_1h_input_tokens", 0)

    cost = (
        uncached_input * inp_price / 1_000_000
        + cache_read * cache_read_price / 1_000_000
        + cache_write * cache_write_price / 1_000_000
        + output * out_price / 1_000_000
    )
    return cost


def build_consumption_chart(prs_data: list[dict], usage_buckets: list[dict],
                            days: int, people: dict | None = None) -> str:
    """Build dual-axis chart: PR bars + cost line ($), filterable by person."""
    if not usage_buckets:
        return ""

    from collections import defaultdict

    # Build full date range
    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=days)
    dates = []
    d = start_date
    while d <= today:
        dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    labels = [datetime.strptime(d, "%Y-%m-%d").strftime("%b %d") for d in dates]

    # Build key_id -> person initials mapping
    key_to_person = {}
    if people:
        for initials, info in people.items():
            for kid in info["key_ids"]:
                key_to_person[kid] = initials

    # Build daily cost per person (in USD)
    person_daily = defaultdict(lambda: defaultdict(float))
    all_daily = defaultdict(float)

    for bucket in usage_buckets:
        date_str = bucket.get("starting_at", "")[:10]
        for result in bucket.get("results", []):
            cost = _calc_cost(result)
            if cost <= 0:
                continue
            all_daily[date_str] += cost
            key_id = result.get("api_key_id", "")
            person = key_to_person.get(key_id, "_other")
            person_daily[person][date_str] += cost

    # Build daily PR counts per person (matched by email prefix)
    pr_daily_all = defaultdict(int)
    pr_daily_person = defaultdict(lambda: defaultdict(int))

    for pr in prs_data:
        raw = pr.get("created", "")
        try:
            dt = datetime.strptime(raw, "%b %d, %Y %H:%M")
            date_key = dt.strftime("%Y-%m-%d")
        except (ValueError, AttributeError):
            continue
        pr_daily_all[date_key] += 1
        email = pr.get("creator_email", "")
        prefix = email.split("@")[0].lower() if email else ""
        if people and prefix in people:
            pr_daily_person[prefix][date_key] += 1
        else:
            pr_daily_person["_other"][date_key] += 1

    # Data arrays (costs in USD)
    pr_counts_all = [pr_daily_all.get(d, 0) for d in dates]
    cost_all = [round(all_daily.get(d, 0), 2) for d in dates]

    # Per-person data
    persons_data = {}
    persons_pr_data = {}
    if people:
        for initials, info in sorted(people.items(), key=lambda x: x[0]):
            cost_arr = [round(person_daily[initials].get(d, 0), 2) for d in dates]
            pr_arr = [pr_daily_person[initials].get(d, 0) for d in dates]
            if sum(cost_arr) > 0 or sum(pr_arr) > 0:
                persons_data[initials] = cost_arr
                persons_pr_data[initials] = pr_arr

    # Colors
    person_colors = [
        "#58a6ff", "#3fb950", "#d29922", "#f85149", "#bc8cff",
        "#f778ba", "#79c0ff", "#56d364", "#e3b341", "#ff7b72",
    ]
    sorted_persons = sorted(persons_data.keys())
    person_color_map = {p: person_colors[i % len(person_colors)] for i, p in enumerate(sorted_persons)}

    # Summary stats
    grand_total = sum(cost_all)

    # Per-person summary for stats table
    person_summaries = []
    for initials in sorted_persons:
        cost = sum(persons_data.get(initials, []))
        prs = sum(persons_pr_data.get(initials, []))
        name = people[initials]["display_name"] if people and initials in people else initials.upper()
        pct = (cost / grand_total * 100) if grand_total > 0 else 0
        cost_per_pr = cost / max(1, prs)
        person_summaries.append((initials, name, cost, prs, pct, cost_per_pr))

    # Sort by cost descending
    person_summaries.sort(key=lambda x: x[2], reverse=True)

    # Filter buttons
    filter_buttons = ['<button class="cons-filter active" onclick="consFilter(\'all\')">Everyone</button>']
    for initials, name, cost, prs, pct, _ in person_summaries:
        if cost > 0 or prs > 0:
            color = person_color_map.get(initials, "#8b949e")
            short = name.split()[0] if " " in name else name
            filter_buttons.append(
                f'<button class="cons-filter" onclick="consFilter(\'{escape(initials)}\')"'
                f' style="border-color:{color}">'
                f'{escape(short)} ({pct:.0f}%)</button>'
            )

    # Summary table rows
    table_rows = []
    for initials, name, cost, prs, pct, cost_per_pr in person_summaries:
        color = person_color_map.get(initials, "#8b949e")
        table_rows.append(
            f'<tr class="user-row" onclick="consFilter(\'{escape(initials)}\')" style="cursor:pointer">'
            f'<td><span class="user-dot" style="background:{color}"></span>{escape(name)}</td>'
            f'<td class="num">{prs}</td>'
            f'<td class="num">${cost:,.2f}</td>'
            f'<td class="num">{pct:.0f}%</td>'
            f'<td class="num">${cost_per_pr:,.2f}</td></tr>'
        )

    chart_data = json.dumps({
        "labels": labels,
        "prCountsAll": pr_counts_all,
        "costAll": cost_all,
        "persons": persons_data,
        "personsPR": persons_pr_data,
        "personColors": person_color_map,
    })

    return f"""
    <div class="consumption-section">
        <h2>AI Cost vs PR Output</h2>
        <div class="cons-stats">
            <div class="cons-stat">
                <span class="cons-num">${grand_total:,.2f}</span>
                <span class="cons-label">Total Spend</span>
            </div>
            <div class="cons-stat">
                <span class="cons-num">{len(prs_data)}</span>
                <span class="cons-label">PRs Created</span>
            </div>
            <div class="cons-stat">
                <span class="cons-num">${grand_total / max(1, len(prs_data)):,.2f}</span>
                <span class="cons-label">Cost / PR</span>
            </div>
            <div class="cons-stat">
                <span class="cons-num">{len(sorted_persons)}</span>
                <span class="cons-label">Users</span>
            </div>
        </div>
        <div class="cons-filters">
            {"".join(filter_buttons)}
        </div>
        <canvas id="consumptionChart" height="280"></canvas>
        <div class="cons-legend">
            <span class="cons-legend-item"><span class="cons-swatch" style="background:rgba(88,166,255,0.4)"></span> PRs (bars)</span>
            <span class="cons-legend-item"><span class="cons-swatch cons-swatch-line" style="background:#f0883e"></span> Cost $ (line)</span>
        </div>
        {"" if not table_rows else '''
        <details style="margin-top:1rem">
            <summary style="cursor:pointer;color:var(--text-muted);font-size:0.85rem">Per-user breakdown</summary>
            <table class="comparison-table" style="margin-top:0.5rem">
                <thead><tr>
                    <th>User</th><th>PRs</th><th>Spend</th><th>% of Total</th><th>Cost/PR</th>
                </tr></thead>
                <tbody>''' + "".join(table_rows) + '''</tbody>
            </table>
        </details>
        '''}
    </div>
    <script>
    (function() {{
        const data = {chart_data};
        const canvas = document.getElementById('consumptionChart');
        const ctx = canvas.getContext('2d');
        const dpr = window.devicePixelRatio || 1;
        let activePerson = 'all';

        function getCostData() {{
            if (activePerson === 'all') return data.costAll;
            return data.persons[activePerson] || data.costAll;
        }}
        function getPRData() {{
            if (activePerson === 'all') return data.prCountsAll;
            return data.personsPR[activePerson] || data.prCountsAll;
        }}
        function fmtDollar(val) {{
            if (val >= 1000) return '$' + (val/1000).toFixed(1) + 'K';
            if (val >= 1) return '$' + val.toFixed(0);
            return '$' + val.toFixed(2);
        }}

        function draw() {{
            const rect = canvas.parentElement.getBoundingClientRect();
            const W = rect.width;
            const H = 280;
            canvas.width = W * dpr;
            canvas.height = H * dpr;
            canvas.style.width = W + 'px';
            canvas.style.height = H + 'px';
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

            const pad = {{ top: 20, right: 65, bottom: 45, left: 45 }};
            const cW = W - pad.left - pad.right;
            const cH = H - pad.top - pad.bottom;
            const n = data.labels.length;
            const barW = Math.max(3, (cW / n) * 0.7);
            const costData = getCostData();
            const prData = getPRData();

            const maxPR = Math.max(1, ...prData);
            const maxCost = Math.max(1, ...costData);
            const niceMaxPR = Math.ceil(maxPR / Math.max(1, Math.ceil(maxPR / 5))) * Math.max(1, Math.ceil(maxPR / 5));
            const niceMaxCost = Math.ceil(maxCost / Math.max(1, Math.ceil(maxCost / 4))) * Math.max(1, Math.ceil(maxCost / 4));

            ctx.clearRect(0, 0, W, H);

            // Grid (left axis - PRs)
            ctx.strokeStyle = '#30363d';
            ctx.lineWidth = 0.5;
            const gridN = Math.min(5, niceMaxPR);
            for (let i = 0; i <= gridN; i++) {{
                const val = Math.round((niceMaxPR / gridN) * i);
                const y = pad.top + cH - (val / niceMaxPR) * cH;
                ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(W - pad.right, y); ctx.stroke();
                ctx.fillStyle = '#58a6ff'; ctx.font = '10px -apple-system, sans-serif'; ctx.textAlign = 'right';
                ctx.fillText(val, pad.left - 6, y + 3);
            }}

            // Right axis (cost $)
            const costGridN = 4;
            for (let i = 0; i <= costGridN; i++) {{
                const val = (niceMaxCost / costGridN) * i;
                const y = pad.top + cH - (val / niceMaxCost) * cH;
                ctx.fillStyle = '#f0883e'; ctx.font = '10px -apple-system, sans-serif'; ctx.textAlign = 'left';
                ctx.fillText(fmtDollar(val), W - pad.right + 6, y + 3);
            }}

            // PR bars
            const barColor = activePerson !== 'all' && data.personColors[activePerson]
                ? data.personColors[activePerson] + '66' : 'rgba(88,166,255,0.4)';
            ctx.fillStyle = barColor;
            for (let i = 0; i < n; i++) {{
                const x = pad.left + (i / n) * cW + ((cW / n) - barW) / 2;
                const h = (prData[i] / niceMaxPR) * cH;
                ctx.fillRect(x, pad.top + cH - h, barW, h);
            }}

            // Cost line
            ctx.beginPath();
            ctx.strokeStyle = '#f0883e'; ctx.lineWidth = 2.5; ctx.lineJoin = 'round';
            for (let i = 0; i < n; i++) {{
                const x = pad.left + (i / n) * cW + (cW / n) / 2;
                const y = pad.top + cH - (costData[i] / niceMaxCost) * cH;
                if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            }}
            ctx.stroke();

            // Area fill
            ctx.lineTo(pad.left + ((n-1)/n)*cW + (cW/n)/2, pad.top+cH);
            ctx.lineTo(pad.left + (cW/n)/2, pad.top+cH);
            ctx.closePath(); ctx.fillStyle = 'rgba(240,136,62,0.08)'; ctx.fill();

            // Dots
            ctx.fillStyle = '#f0883e';
            for (let i = 0; i < n; i++) {{
                const x = pad.left + (i/n)*cW + (cW/n)/2;
                const y = pad.top + cH - (costData[i]/niceMaxCost)*cH;
                ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI*2); ctx.fill();
            }}

            // X labels
            ctx.fillStyle = '#8b949e'; ctx.font = '10px -apple-system, sans-serif'; ctx.textAlign = 'center';
            const labelEvery = Math.max(1, Math.floor(n / 10));
            for (let i = 0; i < n; i += labelEvery) {{
                const x = pad.left + (i/n)*cW + (cW/n)/2;
                ctx.save(); ctx.translate(x, H - pad.bottom + 14); ctx.rotate(-0.5);
                ctx.fillText(data.labels[i], 0, 0); ctx.restore();
            }}

            // Axis titles
            ctx.save(); ctx.fillStyle='#58a6ff'; ctx.font='11px -apple-system, sans-serif';
            ctx.textAlign='center'; ctx.translate(14, pad.top+cH/2); ctx.rotate(-Math.PI/2);
            ctx.fillText('PRs', 0, 0); ctx.restore();

            ctx.save(); ctx.fillStyle='#f0883e'; ctx.font='11px -apple-system, sans-serif';
            ctx.textAlign='center'; ctx.translate(W-8, pad.top+cH/2); ctx.rotate(-Math.PI/2);
            ctx.fillText('Cost ($)', 0, 0); ctx.restore();
        }}

        window.consFilter = function(person) {{
            activePerson = person;
            document.querySelectorAll('.cons-filter').forEach(b => b.classList.remove('active'));
            event.target.classList.add('active');
            draw();
        }};

        draw();
        window.addEventListener('resize', draw);
    }})();
    </script>"""


def build_timeline_chart(prs_data: list[dict], days: int) -> str:
    """Build a PR-over-time timeline chart using canvas."""
    if not prs_data:
        return ""

    # Bucket PRs by date (created date)
    from collections import defaultdict
    daily = defaultdict(lambda: {"completed": 0, "active": 0, "abandoned": 0})

    for pr in prs_data:
        raw = pr.get("created", "")
        try:
            dt = datetime.strptime(raw, "%b %d, %Y %H:%M")
            key = dt.strftime("%Y-%m-%d")
            daily[key][pr["status"]] += 1
        except (ValueError, AttributeError):
            pass

    # Build full date range
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)
    dates = []
    d = start
    while d <= today:
        dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)

    # Build data arrays
    completed_data = [daily[d]["completed"] for d in dates]
    active_data = [daily[d]["active"] for d in dates]
    abandoned_data = [daily[d]["abandoned"] for d in dates]
    labels = [datetime.strptime(d, "%Y-%m-%d").strftime("%b %d") for d in dates]

    # JSON-encode for JS
    chart_data = json.dumps({
        "labels": labels,
        "completed": completed_data,
        "active": active_data,
        "abandoned": abandoned_data,
    })

    return f"""
    <div class="timeline-section">
        <h2>PR Activity Over Time</h2>
        <canvas id="timelineChart" height="200"></canvas>
    </div>
    <script>
    (function() {{
        const data = {chart_data};
        const canvas = document.getElementById('timelineChart');
        const ctx = canvas.getContext('2d');
        const dpr = window.devicePixelRatio || 1;

        function draw() {{
            const rect = canvas.parentElement.getBoundingClientRect();
            canvas.width = rect.width * dpr;
            canvas.height = 220 * dpr;
            canvas.style.width = rect.width + 'px';
            canvas.style.height = '220px';
            ctx.scale(dpr, dpr);

            const W = rect.width;
            const H = 220;
            const pad = {{ top: 20, right: 20, bottom: 40, left: 40 }};
            const chartW = W - pad.left - pad.right;
            const chartH = H - pad.top - pad.bottom;
            const n = data.labels.length;
            const barW = Math.max(2, (chartW / n) - 1);

            // Max value
            let maxVal = 0;
            for (let i = 0; i < n; i++) {{
                const total = data.completed[i] + data.active[i] + data.abandoned[i];
                if (total > maxVal) maxVal = total;
            }}
            if (maxVal === 0) maxVal = 1;
            // Round up to nice number
            const niceMax = Math.ceil(maxVal / Math.max(1, Math.ceil(maxVal / 5))) * Math.max(1, Math.ceil(maxVal / 5));

            ctx.clearRect(0, 0, W, H);

            // Grid lines
            ctx.strokeStyle = '#30363d';
            ctx.lineWidth = 0.5;
            ctx.fillStyle = '#8b949e';
            ctx.font = '11px -apple-system, sans-serif';
            ctx.textAlign = 'right';
            const gridLines = Math.min(5, niceMax);
            for (let i = 0; i <= gridLines; i++) {{
                const val = Math.round((niceMax / gridLines) * i);
                const y = pad.top + chartH - (val / niceMax) * chartH;
                ctx.beginPath();
                ctx.moveTo(pad.left, y);
                ctx.lineTo(W - pad.right, y);
                ctx.stroke();
                ctx.fillText(val, pad.left - 6, y + 4);
            }}

            // Bars (stacked)
            const colors = {{ completed: '#3fb950', active: '#58a6ff', abandoned: '#a80000' }};
            for (let i = 0; i < n; i++) {{
                const x = pad.left + (i / n) * chartW + 0.5;
                let yBase = pad.top + chartH;

                for (const status of ['completed', 'active', 'abandoned']) {{
                    const val = data[status][i];
                    if (val === 0) continue;
                    const barH = (val / niceMax) * chartH;
                    ctx.fillStyle = colors[status];
                    ctx.fillRect(x, yBase - barH, barW, barH);
                    yBase -= barH;
                }}
            }}

            // X-axis labels (show ~8-12 labels)
            ctx.fillStyle = '#8b949e';
            ctx.font = '10px -apple-system, sans-serif';
            ctx.textAlign = 'center';
            const labelEvery = Math.max(1, Math.floor(n / 10));
            for (let i = 0; i < n; i += labelEvery) {{
                const x = pad.left + (i / n) * chartW + barW / 2;
                ctx.save();
                ctx.translate(x, H - pad.bottom + 14);
                ctx.rotate(-0.5);
                ctx.fillText(data.labels[i], 0, 0);
                ctx.restore();
            }}

            // Legend
            const legendY = pad.top - 6;
            let legendX = pad.left;
            for (const [label, color] of [['Completed', '#3fb950'], ['Active', '#58a6ff'], ['Abandoned', '#a80000']]) {{
                ctx.fillStyle = color;
                ctx.fillRect(legendX, legendY - 8, 10, 10);
                ctx.fillStyle = '#8b949e';
                ctx.font = '11px -apple-system, sans-serif';
                ctx.textAlign = 'left';
                ctx.fillText(label, legendX + 14, legendY + 1);
                legendX += ctx.measureText(label).width + 28;
            }}
        }}

        draw();
        window.addEventListener('resize', draw);
    }})();
    </script>"""


def build_user_comparison(prs_data: list[dict]) -> str:
    """Build user comparison table + bar chart."""
    users = {}
    for pr in prs_data:
        email = pr.get("creator_email", "unknown")
        name = pr.get("creator_name", email)
        if email not in users:
            users[email] = {"name": name, "total": 0, "completed": 0, "active": 0,
                            "abandoned": 0, "files": 0, "projects": set(), "repos": set()}
        u = users[email]
        u["total"] += 1
        u[pr["status"]] = u.get(pr["status"], 0) + 1
        u["files"] += len(pr.get("files", []))
        u["projects"].add(pr.get("project_name", ""))
        u["repos"].add(pr.get("repo_name", ""))

    if len(users) < 2:
        return ""

    # Sort by total PRs descending
    sorted_users = sorted(users.items(), key=lambda x: x[1]["total"], reverse=True)
    max_total = max(u["total"] for _, u in sorted_users) if sorted_users else 1

    # Colors for bar chart
    bar_colors = ["#58a6ff", "#3fb950", "#d29922", "#f85149", "#bc8cff",
                  "#f778ba", "#79c0ff", "#56d364", "#e3b341", "#ff7b72"]

    rows = []
    bars = []
    for i, (email, u) in enumerate(sorted_users):
        color = bar_colors[i % len(bar_colors)]
        pct = (u["total"] / max_total) * 100
        rows.append(f"""
        <tr class="user-row" onclick="filterByUser('{escape(email)}')" style="cursor:pointer">
            <td><span class="user-dot" style="background:{color}"></span>{escape(u['name'])}</td>
            <td class="num">{u['total']}</td>
            <td class="num" style="color:var(--green)">{u['completed']}</td>
            <td class="num" style="color:var(--blue)">{u['active']}</td>
            <td class="num">{u['files']}</td>
            <td class="num">{len(u['projects'])}</td>
            <td class="num">{len(u['repos'])}</td>
        </tr>""")
        bars.append(f"""
        <div class="bar-row" onclick="filterByUser('{escape(email)}')" style="cursor:pointer">
            <div class="bar-label">{escape(u['name'].split()[0] if ' ' in u['name'] else u['name'])}</div>
            <div class="bar-track">
                <div class="bar-fill" style="width:{pct}%;background:{color}"></div>
                <span class="bar-value">{u['total']}</span>
            </div>
        </div>""")

    return f"""
    <div class="comparison-section">
        <h2>User Comparison</h2>
        <div class="comparison-grid">
            <div class="bar-chart">
                {"".join(bars)}
            </div>
            <div class="comparison-table-wrap">
                <table class="comparison-table">
                    <thead>
                        <tr>
                            <th>Author</th><th>Total</th><th>Merged</th>
                            <th>Active</th><th>Files</th><th>Projects</th><th>Repos</th>
                        </tr>
                    </thead>
                    <tbody>{"".join(rows)}</tbody>
                </table>
            </div>
        </div>
    </div>"""


def generate_html(prs_data: list[dict], title: str, subtitle: str, org: str,
                   days: int = 30, usage_buckets: list[dict] | None = None,
                   people: dict | None = None) -> str:
    """Generate the full HTML report."""
    now = datetime.now(timezone.utc).strftime("%b %d, %Y %H:%M UTC")

    total = len(prs_data)
    completed = sum(1 for p in prs_data if p["status"] == "completed")
    active = sum(1 for p in prs_data if p["status"] == "active")
    total_files = sum(len(p.get("files", [])) for p in prs_data)
    users = sorted(set((p.get("creator_email", ""), p.get("creator_name", "")) for p in prs_data))
    repos = sorted(set(p["repo_name"] for p in prs_data))
    projects = sorted(set(p.get("project_name", "") for p in prs_data))

    pr_cards = "".join(build_pr_card(pr) for pr in prs_data)
    if not pr_cards:
        pr_cards = '<div class="empty-state">No pull requests found for this period.</div>'

    consumption_html = build_consumption_chart(prs_data, usage_buckets or [], days, people=people)
    timeline_html = build_timeline_chart(prs_data, days)
    comparison_html = build_user_comparison(prs_data)

    # Build filter buttons
    def btn(label, onclick):
        return f'<button class="filter-btn" onclick="{onclick}">{label}</button>'

    filter_buttons = [
        '<button class="filter-btn active" onclick="filterPRs(\'all\')">All (' + str(total) + ')</button>',
        btn(f"Active ({active})", "filterPRs('status:active')"),
        btn(f"Completed ({completed})", "filterPRs('status:completed')"),
    ]
    if len(users) > 1:
        for email, name in users:
            short = name.split()[0] if " " in name else name
            count = sum(1 for p in prs_data if p.get("creator_email") == email)
            filter_buttons.append(btn(f"{escape(short)} ({count})", f"filterByUser('{escape(email)}')"))
    if len(projects) > 1:
        for proj in projects:
            count = sum(1 for p in prs_data if p.get("project_name") == proj)
            filter_buttons.append(btn(f"{escape(proj)} ({count})", f"filterPRs('project:{escape(proj)}')"))
    if len(repos) > 1 and len(repos) <= 10:
        for r in repos:
            count = sum(1 for p in prs_data if p["repo_name"] == r)
            filter_buttons.append(btn(f"{escape(r)} ({count})", f"filterPRs('repo:{escape(r)}')"))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>
    :root {{
        --bg: #0d1117; --surface: #161b22; --border: #30363d;
        --text: #e6edf3; --text-muted: #8b949e; --accent: #58a6ff;
        --green: #3fb950; --red: #f85149; --orange: #d29922; --blue: #58a6ff;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
        background: var(--bg); color: var(--text); line-height: 1.5; padding: 2rem;
    }}
    .container {{ max-width: 1060px; margin: 0 auto; }}
    h1 {{ font-size: 1.8rem; margin-bottom: 0.25rem; }}
    h2 {{ font-size: 1.3rem; margin-bottom: 1rem; color: var(--text); }}
    .subtitle {{ color: var(--text-muted); margin-bottom: 1.5rem; font-size: 0.9rem; }}
    .stats-grid {{
        display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
        gap: 1rem; margin-bottom: 2rem;
    }}
    .stat-card {{
        background: var(--surface); border: 1px solid var(--border);
        border-radius: 8px; padding: 1rem; text-align: center;
    }}
    .stat-card .number {{ font-size: 2rem; font-weight: 700; }}
    .stat-card .label {{
        color: var(--text-muted); font-size: 0.8rem;
        text-transform: uppercase; letter-spacing: 0.05em;
    }}

    /* Consumption */
    .consumption-section {{
        background: var(--surface); border: 1px solid var(--border);
        border-radius: 8px; padding: 1.25rem; margin-bottom: 2rem;
    }}
    .consumption-section canvas {{ width: 100%; }}
    .cons-stats {{
        display: flex; gap: 1.5rem; margin-bottom: 1rem; flex-wrap: wrap;
    }}
    .cons-stat {{
        display: flex; flex-direction: column; align-items: center;
    }}
    .cons-num {{ font-size: 1.4rem; font-weight: 700; color: var(--text); }}
    .cons-label {{ font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.04em; }}
    .cons-filters {{
        display: flex; gap: 0.4rem; margin-bottom: 1rem; flex-wrap: wrap;
    }}
    .cons-filter {{
        background: var(--bg); border: 1px solid var(--border); color: var(--text-muted);
        padding: 0.3rem 0.65rem; border-radius: 14px; cursor: pointer;
        font-size: 0.78rem; transition: all 0.15s;
    }}
    .cons-filter:hover {{ color: var(--text); border-color: var(--text-muted); }}
    .cons-filter.active {{ background: rgba(88,166,255,0.15); color: var(--accent); border-color: var(--accent); }}
    .cons-legend {{
        display: flex; gap: 1.5rem; justify-content: center; margin-top: 0.5rem;
        font-size: 0.8rem; color: var(--text-muted);
    }}
    .cons-legend-item {{ display: flex; align-items: center; gap: 0.35rem; }}
    .cons-swatch {{
        display: inline-block; width: 14px; height: 10px; border-radius: 2px;
    }}
    .cons-swatch-line {{
        height: 3px; border-radius: 2px;
    }}

    /* Timeline */
    .timeline-section {{
        background: var(--surface); border: 1px solid var(--border);
        border-radius: 8px; padding: 1.25rem; margin-bottom: 2rem;
    }}
    .timeline-section canvas {{ width: 100%; }}

    /* Comparison */
    .comparison-section {{
        background: var(--surface); border: 1px solid var(--border);
        border-radius: 8px; padding: 1.25rem; margin-bottom: 2rem;
    }}
    .comparison-grid {{ display: grid; grid-template-columns: 280px 1fr; gap: 1.5rem; align-items: start; }}
    .bar-chart {{ display: flex; flex-direction: column; gap: 0.5rem; }}
    .bar-row {{ display: flex; align-items: center; gap: 0.5rem; }}
    .bar-row:hover .bar-fill {{ opacity: 0.8; }}
    .bar-label {{ width: 70px; font-size: 0.82rem; text-align: right; color: var(--text-muted); flex-shrink: 0; }}
    .bar-track {{ flex: 1; background: var(--bg); border-radius: 4px; height: 22px; position: relative; overflow: hidden; }}
    .bar-fill {{ height: 100%; border-radius: 4px; transition: width 0.3s; min-width: 2px; }}
    .bar-value {{ position: absolute; right: 6px; top: 1px; font-size: 0.75rem; font-weight: 600; }}
    .comparison-table-wrap {{ overflow-x: auto; }}
    .comparison-table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
    .comparison-table th {{
        text-align: left; padding: 0.5rem 0.6rem; border-bottom: 1px solid var(--border);
        color: var(--text-muted); font-weight: 600; font-size: 0.75rem; text-transform: uppercase;
    }}
    .comparison-table td {{ padding: 0.5rem 0.6rem; border-bottom: 1px solid rgba(48,54,61,0.5); }}
    .comparison-table .num {{ text-align: center; }}
    .user-row:hover {{ background: rgba(88,166,255,0.05); }}
    .user-dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }}

    /* Filters */
    .filter-bar {{ display: flex; gap: 0.5rem; margin-bottom: 1.5rem; flex-wrap: wrap; }}
    .filter-btn {{
        background: var(--surface); border: 1px solid var(--border); color: var(--text);
        padding: 0.4rem 0.8rem; border-radius: 20px; cursor: pointer;
        font-size: 0.82rem; transition: all 0.15s;
    }}
    .filter-btn:hover, .filter-btn.active {{
        background: var(--accent); color: var(--bg); border-color: var(--accent);
    }}

    /* PR Cards */
    .pr-card {{
        background: var(--surface); border: 1px solid var(--border);
        border-radius: 8px; margin-bottom: 0.75rem; overflow: hidden; transition: border-color 0.15s;
    }}
    .pr-card:hover {{ border-color: var(--accent); }}
    .pr-header {{
        padding: 0.85rem 1.1rem; display: flex; justify-content: space-between;
        align-items: flex-start; gap: 0.75rem;
    }}
    .pr-title {{
        color: var(--accent); text-decoration: none; font-weight: 600;
        font-size: 1rem; line-height: 1.3;
    }}
    .pr-title:hover {{ text-decoration: underline; }}
    .pr-meta {{ display: flex; align-items: center; gap: 0.4rem; flex-shrink: 0; flex-wrap: wrap; }}
    .badge {{
        color: #fff; padding: 0.12rem 0.45rem; border-radius: 12px;
        font-size: 0.72rem; font-weight: 600; text-transform: uppercase;
    }}
    .pr-id {{ color: var(--text-muted); font-size: 0.82rem; }}
    .pr-repo {{
        color: var(--text-muted); font-size: 0.78rem;
        background: var(--bg); padding: 0.1rem 0.4rem; border-radius: 4px;
    }}
    .pr-creator {{
        color: var(--accent); font-size: 0.78rem; font-weight: 500;
        background: rgba(88,166,255,0.1); padding: 0.1rem 0.4rem; border-radius: 4px;
    }}
    .pr-details {{ padding: 0 1.1rem 0.85rem; display: flex; flex-direction: column; gap: 0.4rem; }}
    .pr-branches code {{
        background: rgba(88,166,255,0.15); color: var(--accent);
        padding: 0.1rem 0.4rem; border-radius: 4px; font-size: 0.8rem;
    }}
    .pr-dates {{ display: flex; gap: 1.5rem; color: var(--text-muted); font-size: 0.8rem; }}
    .pr-desc {{
        color: var(--text-muted); font-size: 0.82rem; white-space: pre-line;
        max-height: 100px; overflow-y: auto; padding: 0.4rem;
        background: var(--bg); border-radius: 6px; border: 1px solid var(--border);
    }}
    .reviewers {{ display: flex; flex-wrap: wrap; gap: 0.35rem; }}
    .reviewer {{
        font-size: 0.8rem; padding: 0.12rem 0.45rem; border-radius: 12px;
        background: var(--bg); border: 1px solid var(--border);
    }}
    .vote-approved {{ color: var(--green); border-color: var(--green); }}
    .vote-approved-suggest {{ color: #3fb950; border-color: #2ea04366; }}
    .vote-wait {{ color: var(--orange); border-color: var(--orange); }}
    .vote-rejected {{ color: var(--red); border-color: var(--red); }}
    .vote-none {{ color: var(--text-muted); }}
    .work-items {{ font-size: 0.8rem; color: var(--text-muted); }}
    .pr-stats {{ display: flex; gap: 0.75rem; font-size: 0.8rem; }}
    .stat-add {{ color: var(--green); }}
    .stat-edit {{ color: var(--orange); }}
    .stat-del {{ color: var(--red); }}
    .files-section {{ font-size: 0.8rem; }}
    .files-section summary {{ cursor: pointer; color: var(--text-muted); padding: 0.25rem 0; }}
    .files-section summary:hover {{ color: var(--accent); }}
    .file-table {{ width: 100%; border-collapse: collapse; margin-top: 0.25rem; }}
    .file-table tr:hover {{ background: rgba(255,255,255,0.03); }}
    .file-change {{ width: 24px; text-align: center; font-weight: 700; padding: 0.15rem 0.35rem; }}
    .file-add {{ color: var(--green); }}
    .file-edit {{ color: var(--orange); }}
    .file-delete {{ color: var(--red); }}
    .file-path {{
        padding: 0.15rem 0.35rem;
        font-family: 'SF Mono', SFMono-Regular, Consolas, 'Liberation Mono', Menlo, monospace;
        font-size: 0.75rem; color: var(--text-muted);
    }}
    .empty-state {{ text-align: center; padding: 3rem; color: var(--text-muted); }}
    .footer {{
        text-align: center; color: var(--text-muted); font-size: 0.75rem;
        margin-top: 2rem; padding-top: 1rem; border-top: 1px solid var(--border);
    }}
    #pr-count {{ color: var(--text-muted); font-size: 0.85rem; margin-bottom: 0.75rem; }}
    @media (max-width: 768px) {{
        body {{ padding: 1rem; }}
        .pr-header {{ flex-direction: column; }}
        .pr-dates {{ flex-direction: column; gap: 0.2rem; }}
        .stats-grid {{ grid-template-columns: repeat(2, 1fr); }}
        .comparison-grid {{ grid-template-columns: 1fr; }}
    }}
</style>
</head>
<body>
<div class="container">
    <h1>{escape(title)}</h1>
    <p class="subtitle">{subtitle} &middot; generated {now}</p>

    <div class="stats-grid">
        <div class="stat-card">
            <div class="number">{total}</div>
            <div class="label">Total PRs</div>
        </div>
        <div class="stat-card">
            <div class="number" style="color:var(--green)">{completed}</div>
            <div class="label">Completed</div>
        </div>
        <div class="stat-card">
            <div class="number" style="color:var(--blue)">{active}</div>
            <div class="label">Active</div>
        </div>
        <div class="stat-card">
            <div class="number">{total_files}</div>
            <div class="label">Files Changed</div>
        </div>
        <div class="stat-card">
            <div class="number">{len(users)}</div>
            <div class="label">Contributors</div>
        </div>
        <div class="stat-card">
            <div class="number">{len(projects)}</div>
            <div class="label">Projects</div>
        </div>
    </div>

    {consumption_html}

    {timeline_html}

    {comparison_html}

    <div class="filter-bar">
        {"".join(filter_buttons)}
    </div>

    <div id="pr-count"></div>
    <div id="pr-list">
        {pr_cards}
    </div>

    <div class="footer">
        Generated by devops-pr-report.py &middot; Azure DevOps &middot; {escape(org)}
    </div>
</div>
<script>
function filterPRs(filter) {{
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    if (event && event.target) event.target.classList.add('active');
    let shown = 0;
    document.querySelectorAll('.pr-card').forEach(card => {{
        let show = true;
        if (filter === 'all') {{
            show = true;
        }} else if (filter.startsWith('status:')) {{
            show = card.dataset.status === filter.slice(7);
        }} else if (filter.startsWith('repo:')) {{
            show = card.dataset.repo === filter.slice(5);
        }} else if (filter.startsWith('user:')) {{
            show = card.dataset.user === filter.slice(5);
        }} else if (filter.startsWith('project:')) {{
            show = card.dataset.project === filter.slice(8);
        }}
        card.style.display = show ? '' : 'none';
        if (show) shown++;
    }});
    document.getElementById('pr-count').textContent = 'Showing ' + shown + ' of {total} PRs';
}}
function filterByUser(email) {{
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    filterPRs('user:' + email);
}}
</script>
</body>
</html>"""


# ── Main ─────────────────────────────────────────────────────────────────────


def enrich_pr(pr: dict, org: str, token: str | None, fetch_files: bool) -> dict:
    """Convert raw PR to display dict, optionally fetching files."""
    repo = pr.get("repository", {})
    repo_id = repo.get("id", "")
    repo_name = repo.get("name", "unknown")
    project_info = repo.get("project", {})
    project_id = project_info.get("id", "")
    project_name = project_info.get("name", "")
    pr_id = pr["pullRequestId"]

    files = []
    diff_stats = {}
    if token and fetch_files:
        try:
            changes = fetch_pr_changes(org, project_id, repo_id, pr_id, token)
            for c in changes:
                path = c.get("item", {}).get("path", "")
                if path and not path.endswith("/"):
                    files.append({"path": path, "type": c.get("changeType", "edit")})
        except Exception:
            pass
        try:
            source = pr.get("lastMergeSourceCommit", {}).get("commitId", "")
            target = pr.get("lastMergeTargetCommit", {}).get("commitId", "")
            diff_stats = fetch_diff_stats(org, project_id, repo_id, source, target, token)
        except Exception:
            pass

    reviewers = [{"name": r.get("displayName", "?"), "vote": r.get("vote", 0)}
                 for r in pr.get("reviewers", [])]

    desc = pr.get("description", "") or ""
    merge_msg = ""
    if pr.get("completionOptions"):
        merge_msg = pr["completionOptions"].get("mergeCommitMessage", "") or ""
    work_items = list(set(re.findall(r"#(\d{5,})", f"{desc} {merge_msg}")))

    created_by = pr.get("createdBy", {})

    return {
        "pr_id": pr_id,
        "title": pr.get("title", "Untitled"),
        "status": pr.get("status", "unknown"),
        "url": pr_url(org, project_name, repo_name, pr_id),
        "repo_name": repo_name,
        "project_name": project_name,
        "creator_name": created_by.get("displayName", ""),
        "creator_email": created_by.get("uniqueName", ""),
        "source_branch": branch_name(pr.get("sourceRefName")),
        "target_branch": branch_name(pr.get("targetRefName")),
        "created": format_date(pr.get("creationDate")),
        "created_ago": days_ago(pr.get("creationDate")),
        "closed": format_date(pr.get("closedDate")),
        "closed_ago": days_ago(pr.get("closedDate")),
        "description": desc[:500],
        "reviewers": reviewers,
        "files": files,
        "diff_stats": diff_stats,
        "work_items": work_items,
    }


def main():
    parser = argparse.ArgumentParser(description="Azure DevOps PR Report Generator")
    parser.add_argument("--user", help="User email (default: current az account)")
    parser.add_argument("--days", type=int, default=30, help="Look back N days (default: 30)")
    parser.add_argument("--org", help="Azure DevOps organization URL")
    parser.add_argument("--project", help="Project name(s), comma-separated")
    parser.add_argument("--output", "-o", help="Output HTML file")
    parser.add_argument("--no-files", action="store_true", help="Skip fetching per-PR file changes (faster)")
    parser.add_argument("--all", action="store_true", help="Fetch all users across all (or specified) projects")
    parser.add_argument("--workers", type=int, default=6, help="Concurrent workers for fetching (default: 6)")
    parser.add_argument("--anthropic-key", help="Anthropic admin API key for consumption data")
    args = parser.parse_args()

    defaults = get_defaults()
    org = args.org or defaults.get("organization", "")
    if not org:
        print("ERROR: Organization required. Set via --org or az devops configure.", file=sys.stderr)
        sys.exit(1)

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    # Determine which projects to scan
    if args.project:
        project_names = [p.strip() for p in args.project.split(",")]
    elif args.all:
        print(f"Discovering projects in {org}...")
        all_projects = list_projects(org)
        project_names = [p["name"] for p in all_projects]
        print(f"  Found {len(project_names)} projects")
    else:
        proj = defaults.get("project", "")
        if not proj:
            print("ERROR: Project required. Use --project, --all, or az devops configure.", file=sys.stderr)
            sys.exit(1)
        project_names = [proj]

    # Determine creator filter
    creator = None
    if not args.all:
        creator = args.user
        if not creator:
            creator = run_az(["account", "show", "--query", "user.name", "-o", "tsv"])

    mode_desc = "all users" if args.all else creator
    print(f"Fetching PRs for {mode_desc} across {len(project_names)} project(s) (last {args.days} days)...")

    # Fetch PRs from all projects concurrently
    all_prs = []

    def fetch_project_prs(project_name: str) -> list[dict]:
        prs = []
        for status in ["completed", "active", "abandoned"]:
            try:
                batch = fetch_prs_for_project(org, project_name, status, creator=creator)
                for pr in batch:
                    created = pr.get("creationDate", "")
                    try:
                        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        if dt >= cutoff:
                            prs.append(pr)
                    except (ValueError, AttributeError):
                        pass
            except Exception:
                pass
        return prs

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_project_prs, name): name for name in project_names}
        done_count = 0
        for future in as_completed(futures):
            done_count += 1
            name = futures[future]
            try:
                prs = future.result()
                if prs:
                    print(f"  [{done_count}/{len(project_names)}] {name}: {len(prs)} PRs")
                    all_prs.extend(prs)
                elif done_count % 20 == 0:
                    print(f"  [{done_count}/{len(project_names)}] scanning...")
            except Exception as e:
                print(f"  [{done_count}/{len(project_names)}] {name}: error - {e}", file=sys.stderr)

    print(f"\n  Total: {len(all_prs)} PRs from {len(set(pr.get('repository',{}).get('project',{}).get('name','') for pr in all_prs))} projects")

    # Deduplicate by PR ID (same PR could appear in multiple queries)
    seen = set()
    unique_prs = []
    for pr in all_prs:
        pid = pr["pullRequestId"]
        if pid not in seen:
            seen.add(pid)
            unique_prs.append(pr)
    all_prs = unique_prs

    # Sort by creation date descending
    all_prs.sort(key=lambda p: p.get("creationDate", ""), reverse=True)

    # Enrich PRs with file changes (concurrent)
    token = get_token() if not args.no_files and all_prs else None
    fetch_files = not args.no_files

    print(f"  Enriching {len(all_prs)} PRs{'  (fetching files)' if fetch_files else ''}...")
    prs_data = []

    def enrich(pr):
        return enrich_pr(pr, org, token, fetch_files)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(enrich, pr): i for i, pr in enumerate(all_prs)}
        results = [None] * len(all_prs)
        done = 0
        for future in as_completed(futures):
            idx = futures[future]
            done += 1
            try:
                results[idx] = future.result()
            except Exception as e:
                print(f"    Warning: PR enrichment failed: {e}", file=sys.stderr)
            if done % 10 == 0 or done == len(all_prs):
                print(f"    [{done}/{len(all_prs)}]")

    prs_data = [r for r in results if r is not None]

    # Generate report
    if args.all:
        title = "PR Report \u2014 All Users"
        subtitle = f"All projects &middot; last {args.days} days"
        default_output = "reports/pr-report-all.html"
    elif len(project_names) > 1:
        title = f"PR Report \u2014 {creator}"
        subtitle = f"{', '.join(project_names)} &middot; last {args.days} days"
        default_output = "reports/pr-report.html"
    else:
        title = f"PR Report \u2014 {creator}"
        subtitle = f"{project_names[0]} &middot; last {args.days} days"
        default_output = "reports/pr-report.html"

    # Fetch Anthropic usage if key provided
    usage_buckets = None
    people = None
    if args.anthropic_key:
        print("\n  Fetching Anthropic API key list...")
        key_map = fetch_api_keys(args.anthropic_key)
        print(f"  Found {len(key_map)} API keys")

        if key_map:
            people = map_keys_to_people(key_map, prs_data)
            matched = sum(1 for p in people.values() if p["email"])
            print(f"  Mapped to {len(people)} users ({matched} matched to PR authors)")
            for initials, info in sorted(people.items()):
                status = f"-> {info['display_name']}" if info["email"] else "(no PR match)"
                print(f"    {initials}: {len(info['key_ids'])} keys {status}")

        print("  Fetching Anthropic API usage data...")
        usage_buckets = fetch_anthropic_usage(args.anthropic_key, args.days)
        if usage_buckets:
            total_cost = sum(
                _calc_cost(r)
                for b in usage_buckets for r in b.get("results", [])
            )
            print(f"  Got {len(usage_buckets)} daily buckets, ${total_cost:,.2f} total estimated cost")
        else:
            print("  No usage data returned (check key permissions)")

    html = generate_html(prs_data, title, subtitle, org, days=args.days,
                         usage_buckets=usage_buckets, people=people)

    output_path = args.output or default_output
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    print(f"\nReport saved to {output.resolve()}")


if __name__ == "__main__":
    main()
