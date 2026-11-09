"""Write .env by asking for each setting, for one of:

  deploy   scripts/deploy.sh, which sets up Azure
  local    running a job, such as verify, from this machine
  restore  restoring R2 from the backup, as the README says

Settings already in .env are offered as the defaults, and the others are kept.

Usage: python3 scripts/configure.py {deploy,local,restore}
"""

import argparse
import getpass
import re
import sys
from pathlib import Path

ENV = Path(__file__).resolve().parent.parent / ".env"


def load(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.startswith("#"):
            values[key] = value.removeprefix("'").removesuffix("'")
    return values


def dump(path: Path, values: dict[str, str]) -> None:
    # Single quotes, so neither bash's `source` nor uv's --env-file reads $ or & in a value.
    text = "".join(f"{k}='{v}'\n" for k, v in values.items())
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    _ = path.write_text(text)


def ask(
    env: dict[str, str], key: str, about: str, *, pattern: str = r"\S+", default: str = "", secret: bool = False
) -> str:
    current = env.get(key) or default
    print(f"\n{about}")
    shown = "keep the current one" if secret and current else current
    while True:
        prompt = f"{key}" + (f" [{shown}]" if shown else "") + ": "
        value = (getpass.getpass(prompt) if secret else input(prompt)).strip() or current
        if re.fullmatch(pattern, value) and "'" not in value:
            return value
        print("That doesn't look right, try again.")


def r2_remote(env: dict[str, str], remote: str, about: str) -> dict[str, str]:
    prefix = f"RCLONE_CONFIG_{remote}_"
    endpoint = env.get("RCLONE_CONFIG_R2_ENDPOINT") or env.get("RCLONE_CONFIG_R2W_ENDPOINT", "")
    current = re.search(r"//([0-9a-f]{32})\.", endpoint)
    account = ask(
        {"R2_ACCOUNT_ID": current.group(1) if current else ""},
        "R2_ACCOUNT_ID",
        "The Cloudflare account ID, shown on the R2 overview page in the dashboard.",
        pattern=r"[0-9a-f]{32}",
    )
    print(f"\n{about}\nCreate it under R2 > Manage API tokens, and copy its S3 credentials.")
    return {
        f"{prefix}TYPE": "s3",
        f"{prefix}PROVIDER": "Cloudflare",
        f"{prefix}ENDPOINT": f"https://{account}.r2.cloudflarestorage.com",
        f"{prefix}ACCESS_KEY_ID": ask(
            env, f"{prefix}ACCESS_KEY_ID", "The token's Access Key ID.", pattern=r"[0-9a-f]{32}"
        ),
        f"{prefix}SECRET_ACCESS_KEY": ask(
            env, f"{prefix}SECRET_ACCESS_KEY", "The token's Secret Access Key.", pattern=r"[0-9a-f]{64}", secret=True
        ),
    }


def storage(env: dict[str, str]) -> dict[str, str]:
    account = ask(
        env,
        "RCLONE_CONFIG_AZ_ACCOUNT",
        "The backup's storage account name, unique across Azure: 3-24 lowercase letters and digits. Locally, rclone\n"
        + "signs in as you through `az login`, which needs a Storage Blob Data role on it (see the README).",
        pattern=r"[a-z0-9]{3,24}",
    )
    return {
        "RCLONE_CONFIG_AZ_TYPE": "azureblob",
        "RCLONE_CONFIG_AZ_ACCOUNT": account,
        "RCLONE_CONFIG_AZ_ENV_AUTH": "true",
        "RCLONE_CONFIG_AZ_NO_CHECK_CONTAINER": "true",
    }


def deploy(env: dict[str, str]) -> dict[str, str]:
    values = {
        "AZURE_SUBSCRIPTION": ask(
            env, "AZURE_SUBSCRIPTION", "The subscription ID to deploy into: `az account show --query id --output tsv`."
        ),
        "AZURE_RESOURCE_GROUP": ask(
            env,
            "AZURE_RESOURCE_GROUP",
            "The resource group, made if it doesn't exist.",
            default="alai-galaxy-sync-backup",
        ),
        "AZURE_LOCATION": ask(
            env,
            "AZURE_LOCATION",
            "The Azure region, away from the R2 buckets' location: `az account list-locations --output table`.",
        ),
    }
    ids = "`gh api repos/alai-sh/galaxy-sync-backup --jq '.owner.id, .id'` prints the owner's ID, then the repo's."
    values |= {
        "GITHUB_OWNER_ID": ask(
            env, "GITHUB_OWNER_ID", f"The GitHub org's numeric ID, which the deploy trusts. {ids}", pattern=r"[0-9]+"
        ),
        "GITHUB_REPOSITORY_ID": ask(env, "GITHUB_REPOSITORY_ID", "The repo's numeric ID.", pattern=r"[0-9]+"),
    }
    values |= storage(env)
    values |= r2_remote(env, "R2", "An R2 API token with Object Read only, on alai-index, alai-dl and alai-state.")
    for job in ("COPY", "VERIFY", "RESTORE"):
        key = f"HEARTBEAT_{job}_URL"
        values[key] = ask(
            env, key, f"The ping URL of the heartbeat check for {job.lower()}.", pattern=r"https://\S+", secret=True
        )
    return values


def local(env: dict[str, str]) -> dict[str, str]:
    values = storage(env)
    if input("\nAlso set up R2, to run copy or check R2 itself? [y/N]: ").strip().lower() == "y":
        values |= r2_remote(env, "R2", "An R2 API token with Object Read only, on alai-index, alai-dl and alai-state.")
    return values


def restore(env: dict[str, str]) -> dict[str, str]:
    # A separate remote, so the write token never replaces the read-only one deploy.sh gives the jobs.
    return storage(env) | r2_remote(
        env,
        "R2W",
        "An R2 API token with Object Read & Write, on alai-index, alai-dl and alai-state. Once the restore is\n"
        + "done, delete the token, and the RCLONE_CONFIG_R2W_ lines in .env.",
    )


ACTIONS = {"deploy": deploy, "local": local, "restore": restore}
NEXT = {
    "deploy": "Next: `az login`, then `scripts/deploy.sh base`.",
    "local": "Next: `uv run --env-file .env galaxy-sync-backup verify --sample 20`.",
    "restore": "Next: follow Restoring in the README.",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _ = parser.add_argument("action", choices=ACTIONS)
    action = parser.parse_args().action
    env = load(ENV)
    try:
        env |= ACTIONS[action](env)
    except KeyboardInterrupt, EOFError:
        sys.exit("\nNothing written.")
    dump(ENV, env)
    print(f"\nWrote {ENV}.\n{NEXT[action]}")


if __name__ == "__main__":
    main()
