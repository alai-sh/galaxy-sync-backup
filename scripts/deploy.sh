#!/usr/bin/env bash
# Usage: scripts/deploy.sh base         as Owner, once and after changing infra/base.bicep or the secrets in .env
#        scripts/deploy.sh jobs IMAGE   what a v* tag deploys, by hand, with IMAGE as <image>@sha256:<digest>
set -euo pipefail
cd "$(dirname "$0")/.."

stage=${1:-}
if [[ ! ($stage == base && $# == 1) && ! ($stage == jobs && $# == 2) ]]; then
    sed -n '2,3p' "$0" | sed 's/^# //' >&2
    exit 1
fi

set -a
# shellcheck source=/dev/null
source .env
set +a
need=(AZURE_SUBSCRIPTION AZURE_RESOURCE_GROUP RCLONE_CONFIG_AZ_ACCOUNT)
if [[ $stage == base ]]; then
    need+=(AZURE_LOCATION GITHUB_OWNER_ID GITHUB_REPOSITORY_ID
        RCLONE_CONFIG_R2_ENDPOINT RCLONE_CONFIG_R2_ACCESS_KEY_ID RCLONE_CONFIG_R2_SECRET_ACCESS_KEY
        HEARTBEAT_COPY_URL HEARTBEAT_VERIFY_URL HEARTBEAT_RESTORE_URL)
fi
for var in "${need[@]}"; do
    [[ -n "${!var:-}" ]] || { echo "$var isn't set in .env, see scripts/configure.py" >&2; exit 1; }
done
az account set --subscription "$AZURE_SUBSCRIPTION"

if [[ $stage == jobs ]]; then
    # By digest, so whoever can push to the GitHub package can't change what the jobs run.
    [[ $2 =~ @sha256:[0-9a-f]{64}$ ]] || { echo "IMAGE must be <image>@sha256:<digest>, not a tag: $2" >&2; exit 1; }
    az deployment group create --resource-group "$AZURE_RESOURCE_GROUP" --name jobs --template-file infra/jobs.bicep \
        --parameters image="$2" storageAccountName="$RCLONE_CONFIG_AZ_ACCOUNT" --output none
    echo "deployed the jobs with $2"
    exit
fi

# A new subscription has none of these registered, and registering an existing one does nothing.
for ns in Microsoft.App Microsoft.KeyVault Microsoft.ManagedIdentity Microsoft.OperationalInsights Microsoft.Storage; do
    az provider register --namespace "$ns" --wait --output none
done
az group create --name "$AZURE_RESOURCE_GROUP" --location "$AZURE_LOCATION" --output none

# Secrets go in a parameters file only this user can read, rather than on the command line.
params=$(mktemp)
trap 'rm -f "$params"' EXIT
chmod 600 "$params"
python3 - > "$params" <<'EOF'
import json, os
values = {
    "storageAccountName": os.environ["RCLONE_CONFIG_AZ_ACCOUNT"],
    "githubOwnerId": os.environ["GITHUB_OWNER_ID"],
    "githubRepositoryId": os.environ["GITHUB_REPOSITORY_ID"],
    "r2Endpoint": os.environ["RCLONE_CONFIG_R2_ENDPOINT"],
    "r2AccessKeyId": os.environ["RCLONE_CONFIG_R2_ACCESS_KEY_ID"],
    "r2SecretAccessKey": os.environ["RCLONE_CONFIG_R2_SECRET_ACCESS_KEY"],
    "heartbeatCopyUrl": os.environ["HEARTBEAT_COPY_URL"],
    "heartbeatVerifyUrl": os.environ["HEARTBEAT_VERIFY_URL"],
    "heartbeatRestoreUrl": os.environ["HEARTBEAT_RESTORE_URL"],
}
print(json.dumps({
    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
    "contentVersion": "1.0.0.0",
    "parameters": {k: {"value": v} for k, v in values.items()},
}))
EOF

az deployment group create --resource-group "$AZURE_RESOURCE_GROUP" --name base --template-file infra/base.bicep \
    --parameters location="$AZURE_LOCATION" @"$params" --query properties.outputs --output json |
    python3 -c '
import json, os, sys
out = {k: v["value"] for k, v in json.load(sys.stdin).items()}
print("Deployed the base. Set these as variables on the azure environment in GitHub:")
for k, v in [("AZURE_CLIENT_ID", out["deployerClientId"]), ("AZURE_TENANT_ID", out["tenantId"]),
             ("AZURE_SUBSCRIPTION_ID", out["subscriptionId"]), ("AZURE_RESOURCE_GROUP", os.environ["AZURE_RESOURCE_GROUP"]),
             ("AZURE_STORAGE_ACCOUNT", os.environ["RCLONE_CONFIG_AZ_ACCOUNT"])]:
    print(f"  {k}={v}")
'
