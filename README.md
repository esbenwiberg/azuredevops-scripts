# Azure DevOps Scripts

Utility scripts for Azure DevOps pull request reporting and status checks.

## Prerequisites

- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) with the DevOps extension
- Python 3.10+
- Authenticated via `az login` with DevOps defaults configured:

```bash
az devops configure --defaults organization=https://dev.azure.com/YOUR_ORG project=YOUR_PROJECT
```

## Scripts

### devops-pr-report.py

Generates a standalone HTML report with PR details, file changes, timeline charts, and user comparison.

**Usage:**

```bash
# Your PRs in the default project (last 30 days)
python3 devops-pr-report.py

# Specific user, specific project
python3 devops-pr-report.py --user user@example.com --project MyProject

# All users across all projects (last 60 days)
python3 devops-pr-report.py --all --days 60

# All users, specific projects
python3 devops-pr-report.py --all --project "ProjectA,ProjectB"

# Include Anthropic API cost tracking (requires admin API key)
python3 devops-pr-report.py --all --anthropic-key $ANTHROPIC_ADMIN_KEY

# Skip file-level details for faster generation
python3 devops-pr-report.py --all --no-files
```

**Options:**

| Flag | Description | Default |
|------|-------------|---------|
| `--user` | Filter by user email | Current `az` account |
| `--project` | Project name(s), comma-separated | Default from `az devops configure` |
| `--days` | Look-back period in days | 30 |
| `--all` | Fetch all users across all projects | Off |
| `--org` | Azure DevOps org URL | Default from `az devops configure` |
| `--output`, `-o` | Output HTML file path | `reports/pr-report.html` |
| `--no-files` | Skip per-PR file change fetching (faster) | Off |
| `--workers` | Concurrent API workers | 6 |
| `--anthropic-key` | Anthropic admin API key for cost tracking | Off |

**Report includes:**

- Summary stats (total PRs, completed, active, files changed, contributors)
- AI cost vs PR output chart (when `--anthropic-key` is provided)
- PR activity timeline (stacked bar chart by status)
- User comparison table and bar chart
- Filterable PR cards with branches, reviewers, file changes, and diff stats

### time-report.py

Generates a per-day work activity summary for time registration. Combines Azure DevOps PRs, Claude Code session history, local git commits, and optionally MS365 calendar events.

**Usage:**

```bash
# Last 2 weeks (default)
python3 time-report.py

# Specific date range
python3 time-report.py --from 2026-02-01 --to 2026-02-15

# Single day
python3 time-report.py --date 2026-02-11

# Specific DevOps project(s)
python3 time-report.py --project "TeamPlanner - V3"

# All DevOps projects
python3 time-report.py --all-projects

# Include MS365 calendar events
python3 time-report.py --calendar

# Output as JSON (for piping to other tools)
python3 time-report.py --json

# Skip specific sources
python3 time-report.py --no-devops --no-git
```

**Options:**

| Flag | Description | Default |
|------|-------------|---------|
| `--from` | Start date (YYYY-MM-DD) | 14 days ago |
| `--to` | End date (YYYY-MM-DD) | Today |
| `--date` | Single date (YYYY-MM-DD) | — |
| `--days` | Look-back period in days | 14 |
| `--project` | DevOps project(s), comma-separated | Default from `az devops configure` |
| `--all-projects` | Scan all DevOps projects | Off |
| `--calendar` | Include MS365 calendar events | Off |
| `--no-devops` | Skip Azure DevOps PRs | Off |
| `--no-claude` | Skip Claude Code history | Off |
| `--no-git` | Skip git commit history | Off |
| `--json` | Output JSON instead of text | Off |

**Data sources:**

| Tag | Source | What it shows |
|-----|--------|---------------|
| `PR` | Azure DevOps | PRs created or merged on that day |
| `CODE` | Claude Code `~/.claude/history.jsonl` | Projects with active sessions |
| `GIT` | Local git repos | Commit counts per repo |
| `CAL` | MS365 Calendar (Graph API) | Meetings and events |

**Calendar setup:** Requires a one-time browser login with Graph scope:

```bash
az login --scope "https://graph.microsoft.com/Calendars.Read"
```

### devops-prs.sh

Quick terminal check for your active PRs and pending reviews.

```bash
bash devops-prs.sh
```

## License

MIT
