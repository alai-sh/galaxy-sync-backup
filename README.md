# galaxy-sync-backup

Backs up the R2 buckets that galaxy-sync writes (`alai-index`, `alai-dl` and
`alai-state`) to Azure Blob Storage, and regularly checks that the backup can
be restored. The jobs run as scheduled Azure Container Apps Jobs, from a
public image on ghcr.io that Azure runs by digest.

## What's backed up

| R2 | Azure Blob | Kept |
|---|---|---|
| `alai-dl/sha256/` | `backup/alai-dl/sha256/` | until deleted by hand |
| `alai-index/index/` | `backup/alai-index/index/` | until unread for 180 days |
| `alai-state/conflicts/` | `backup/alai-state/conflicts/` | until deleted by hand |
| `alai-index/config.json`, `alai-index/collections.json`, `alai-state/state.json` | `snapshots/<date>/<bucket>/<file>` | 90 days |

`plugins.jsonl` and the plugin cache aren't backed up, as alai-index makes
both again from the artifacts. Deleted blobs can be recovered for 14 days.

## The jobs

| Job | When (UTC) | What it does |
|---|---|---|
| `copy` | daily, 03:17 | Snapshots the files that change, copies new files to the backup, and checks every R2 file is there with the same size and MD5. |
| `verify` | Sundays, 08:17 | Checks the newest snapshot is recent, every current index file is there and matches its sha256, and every artifact they list is there at the right size. Hashes 500 random artifacts and every conflict artifact. |
| `restore-test` | 1 Jan, Apr, Jul and Oct, 09:17 | Restores the index from the newest snapshot, installs 5 random collections from it with `alai`, and extracts their plugins with `alai-index generate-plugins`. |

A job exits 1 if it finds a problem, and pings its heartbeat URL only when
it finds none. `copy` writes as `galaxy-sync-backup-writer`, which can't
delete. `verify` and `restore-test` run as `galaxy-sync-backup-reader`, which
can only read.

## Setting it up

### 1. Cloudflare

1. Create an R2 API token with **Object Read only**, scoped to
   `alai-index`, `alai-dl` and `alai-state`.
2. Note the R2 buckets' location, so Azure can go in another region.

### 2. Heartbeats

Create a check for each job with a heartbeat service such as
healthchecks.io:

| Check | Schedule (UTC) | Grace |
|---|---|---|
| copy | `17 3 * * *` | 6 hours |
| verify | `17 8 * * 0` | 6 hours |
| restore-test | `17 9 1 1,4,7,10 *` | 6 hours |

### 3. Azure

Use a subscription just for this. Setting it up is in two parts:

- **The base**, which you deploy by hand as Owner of the subscription, which
  its creator is. That's the storage, the jobs' identities and roles, a Key
  Vault holding the R2 token and heartbeat URLs, and the identity GitHub
  deploys the jobs as.
- **The jobs**, which each `v*` tag deploys once you approve it. GitHub signs
  in to Azure with OIDC, as an identity that can only deploy the jobs, and
  never sees the secrets.

Any recent `az` works. It downloads Bicep on the first deploy, and asks to
install the `containerapp` extension the first time a command needs it. To
use `az` in a container instead, start one from this repo's directory and
run the steps below inside it. The `.azure` volume keeps the login between
runs.

```bash
podman run --rm -it -v "$PWD:/work:Z" -v galaxy-sync-backup-az:/root/.azure -w /work \
    mcr.microsoft.com/azure-cli:2.83.0
az login --use-device-code
scripts/deploy.sh base
```

1. `python3 scripts/configure.py deploy`, which asks for each setting and
   writes `.env`.
2. `az login`, then `scripts/deploy.sh base`. It prints the variables GitHub
   needs. Run it again after changing `infra/base.bicep` or a secret.
3. In the GitHub repo's settings, under Environments, create `azure`:
   - **Required reviewers**: you, so nothing deploys without your approval.
   - **Deployment branches and tags**: selected tags only, matching `v*`.
   - **Environment variables**: the five `deploy.sh base` printed,
     `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`,
     `AZURE_RESOURCE_GROUP` and `AZURE_STORAGE_ACCOUNT`.
4. Release `v0.1.0`, as [Releasing](#releasing) says.
5. Start the first copy, then run `backup-verify` and `backup-restore-test`
   the same way once it's done:

   ```bash
   az containerapp job start --resource-group alai-galaxy-sync-backup --name backup-copy
   ```

6. Add a budget alert on the resource group under Cost Management.

The resource group has a `CanNotDelete` lock. Remove it before deleting
anything in the group.

To run a job or a restore from your own machine, give yourself
`Storage Blob Data Reader` on the storage account:

```bash
az role assignment create --role "Storage Blob Data Reader" --assignee "$(az ad signed-in-user show --query id --output tsv)" \
    --scope "$(az storage account show --name "$RCLONE_CONFIG_AZ_ACCOUNT" --query id --output tsv)"
```

### Watching the jobs

```bash
az containerapp job execution list --resource-group alai-galaxy-sync-backup --name backup-verify --output table
```

A run's output is in the Log Analytics workspace, in
`ContainerAppConsoleLogs_CL`, filtered on `ContainerJobName_s`.

### Releasing

1. Set `version` in `pyproject.toml`, run `uv lock`, and merge that.
2. Push a matching tag, such as `v0.1.0` for `0.1.0`. The `Image` workflow
   checks the tag matches, runs CI, and pushes the image to
   `ghcr.io/alai-sh/galaxy-sync-backup`.
3. The first time only, make the package public in its settings on GitHub,
   as the jobs pull without credentials.
4. Approve the workflow's `deploy` job, which deploys the jobs with the image
   by digest.

If a job fails for a passing reason, use **Re-run failed jobs** on the
workflow run, rather than tagging again.

To deploy an image by hand instead, run
`scripts/deploy.sh jobs ghcr.io/alai-sh/galaxy-sync-backup@sha256:<digest>`.

## Restoring

You need rclone, and `az login` with the reader role above. Run
`python3 scripts/configure.py restore`, which asks for an R2 token that can
write and sets it up as the `r2w` remote below. Delete that token afterwards.

1. Stop the sync: `gh workflow disable sync.yml --repo alai-sh/galaxy-sync`.
2. Pick the newest snapshot from before the damage. Each one is a directory
   named by its date:

   ```console
   $ rclone lsf az:snapshots/
   2026-11-06/
   2026-11-07/
   2026-11-08/
   ```

   Set `D` to the one to restore, without the trailing `/`:

   ```bash
   D=2026-11-07
   ```
3. Copy back the content-addressed files. `--checksum` compares each file's
   MD5 in the backup and R2, so it uploads the ones missing from R2 and
   replaces the ones that differ, and skips the rest:

   ```bash
   immutable="Cache-Control: public, max-age=31536000, immutable"
   rclone copy az:backup/alai-dl/sha256 r2w:alai-dl/sha256 --checksum \
       --header-upload "$immutable" --header-upload "Content-Type: application/gzip"
   rclone copy az:backup/alai-index/index r2w:alai-index/index --checksum \
       --header-upload "$immutable" --header-upload "Content-Type: text/plain; charset=utf-8"
   rclone copy az:backup/alai-state/conflicts r2w:alai-state/conflicts --checksum \
       --header-upload "Cache-Control: private, no-store" --header-upload "Content-Type: application/gzip"
   ```

4. Put back the files that change, from snapshot `D`:

   ```bash
   for f in config.json collections.json; do
       rclone copyto "az:snapshots/$D/alai-index/$f" "r2w:alai-index/$f" \
           --header-upload "Cache-Control: public, max-age=600" --header-upload "Content-Type: application/json"
   done
   rclone copyto "az:snapshots/$D/alai-state/state.json" r2w:alai-state/state.json \
       --header-upload "Cache-Control: private, no-store" --header-upload "Content-Type: application/json"
   ```

5. Ask galaxy-sync to rebuild the plugin index, which the next sync does:
   `gh workflow run request.yml --repo alai-sh/galaxy-sync -f action=generate-plugins`.
6. Start the sync again: `gh workflow enable sync.yml --repo alai-sh/galaxy-sync`.
7. Check R2 by hashing every artifact. The hashes are worked out as the files
   stream, so nothing is saved to disk:

   ```bash
   uv run --env-file .env galaxy-sync-backup --dest r2w: verify --sample 1000000
   ```

   Each problem names the file at fault. Copy those again with
   `rclone copyto --ignore-times`.

If R2 is gone altogether, create the buckets again before step 3.

## Taking a file down

Remove the file from R2 first, or the next `copy` puts it back. Then, with
`Storage Blob Data Contributor` on the storage account, delete it from the
backup:

```bash
az storage blob delete --auth-mode login --account-name "$RCLONE_CONFIG_AZ_ACCOUNT" \
    --container-name backup --name "alai-dl/sha256/<aa>/<sha256>.tar.gz"
```

It's gone for good 14 days later.

## Development

```bash
uv run pytest
TEST_RCLONE=$(command -v rclone) uv run pytest    # the same tests with a real rclone
uv run ruff check .
uv run ruff format . --check
uv run basedpyright --warnings
```

With rclone installed, the reader role above, and
`python3 scripts/configure.py local`, a job can run against the real storage:

```bash
uv run --env-file .env galaxy-sync-backup verify --sample 20
```

| Pinned | Where |
|---|---|
| rclone | `Dockerfile` |
| uv | `Dockerfile` and `.github/workflows/ci.yml` |
| Python | `Dockerfile`, `.python-version` and `pyproject.toml` |
| alai-sh, alai-index | `pyproject.toml`, with alai-index at galaxy-sync's version |
