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

### devops-prs.sh

Quick terminal check for your active PRs and pending reviews.

```bash
bash devops-prs.sh
```

## License

MIT
