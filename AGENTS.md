# galaxy-sync-backup

Backs up galaxy-sync's R2 buckets to Azure Blob Storage and checks the backup restores. The design, setup and restore
runbook are in [README.md](README.md).

- `src/galaxy_sync_backup/cli.py` holds the three jobs (`copy`, `verify` and `restore-test`), and `rclone.py` holds
  the only rclone commands they run. The Azure side is `infra/base.bicep`, deployed by hand as Owner, and
  `infra/jobs.bicep`, deployed by `.github/workflows/image.yml` on a `v*` tag. Azure runs the image by digest,
  never by tag.
- The deployer identity GitHub signs in as can only deploy the jobs. Anything that needs more, such as a role,
  a secret or the storage account, belongs in `base.bicep`, and secrets go in its Key Vault, never in GitHub.
- This repo is public. Secrets, heartbeat URLs and the R2 endpoint (which holds the Cloudflare account ID) live
  only in `.env` and Azure, never in a file here.
- Never delete or overwrite anything in the backup. `copy` only adds files, and `--immutable` makes a changed
  file an error. The writer identity's role has no delete permission, so anything that tries fails there too.
  There are no immutability policies, so a takedown can still be removed from the backup by hand.
- `verify` must download every current index file. A lifecycle rule deletes index files unread for 180 days, so
  reading them is what keeps them.
- A job reports problems and exits 1. It only pings the heartbeat after a run that found no problems.
- Tests never reach the network. `LocalRclone` in `tests/conftest.py` stands in for rclone. The fixture index is
  built with the real alai-index, and `restore-test` runs the real alai and alai-index.
- Checks: `uv run pytest`, `uv run ruff check .`, `uv run ruff format . --check` and
  `uv run basedpyright --warnings`. All must pass.
