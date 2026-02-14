#!/bin/bash
# Quick PR status check

echo "=== PRs Created by Me ==="
az repos pr list --creator "$(az account show --query user.name -o tsv)" --status active

echo ""
echo "=== PRs Awaiting My Review ==="
az repos pr list --reviewer "$(az account show --query user.name -o tsv)" --status active
