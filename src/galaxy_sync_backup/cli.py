"""Back up alai's R2 buckets to Azure Blob Storage, and check that the backup restores."""

import argparse
import datetime
import hashlib
import json
import os
import random
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .rclone import Rclone

SNAPSHOT_FILES = ("alai-index/config.json", "alai-index/collections.json", "alai-state/state.json")
# config.json's artifact and index file templates must stay inside these.
IMMUTABLE = ("alai-index/index", "alai-dl/sha256", "alai-state/conflicts")
# copy runs daily, plus the time a copy takes.
MAX_SNAPSHOT_AGE = datetime.timedelta(days=2)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="galaxy-sync-backup", description=__doc__)
    _ = parser.add_argument(
        "--source",
        default=os.environ.get("ALAI_BACKUP_SOURCE", "r2:"),
        help="rclone path holding the R2 buckets (env: ALAI_BACKUP_SOURCE, default: r2:)",
    )
    _ = parser.add_argument(
        "--dest",
        default=os.environ.get("ALAI_BACKUP_DEST", "az:backup/"),
        help="rclone path of the backup (env: ALAI_BACKUP_DEST, default: az:backup/)",
    )
    _ = parser.add_argument(
        "--snapshots",
        default=os.environ.get("ALAI_BACKUP_SNAPSHOTS", "az:snapshots/"),
        help="rclone path of the dated snapshots (env: ALAI_BACKUP_SNAPSHOTS, default: az:snapshots/)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    _ = sub.add_parser("copy", help="copy new files to the backup, and today's snapshot")
    verify_parser = sub.add_parser("verify", help="check the backup is complete, and hash a sample of artifacts")
    _ = verify_parser.add_argument(
        "--sample", type=int, default=500, help="artifacts to download and hash (default: 500)"
    )
    restore_parser = sub.add_parser("restore-test", help="restore a sample from the backup and install it with alai")
    _ = restore_parser.add_argument("--count", type=int, default=5, help="collections to restore (default: 5)")
    _ = restore_parser.add_argument(
        "--collection",
        action="append",
        default=[],
        metavar="NS.NAME",
        help="restore this collection rather than a random one (can be repeated)",
    )
    _ = restore_parser.add_argument(
        "--max-mb",
        type=int,
        default=50,
        help="only pick collections whose artifacts are at most this many MB (default: 50)",
    )
    args = parser.parse_args(argv)

    rclone = Rclone(os.environ.get("RCLONE", "rclone"))
    today = datetime.datetime.now(datetime.UTC).date()
    with tempfile.TemporaryDirectory(prefix="galaxy-sync-backup-") as tmp:
        work = Path(tmp)
        if args.command == "copy":
            problems = copy(rclone, args.source, args.dest, args.snapshots, today)
        elif args.command == "verify":
            problems = verify(rclone, args.dest, args.snapshots, today, work, args.sample, random.Random())
        else:
            problems = restore_test(
                rclone,
                args.dest,
                args.snapshots,
                today,
                work,
                args.count,
                args.collection,
                args.max_mb * 1024 * 1024,
                random.Random(),
                alai=shlex.split(os.environ.get("ALAI", "alai")),
                alai_index=shlex.split(os.environ.get("ALAI_INDEX", "alai-index")),
            )
    for problem in problems:
        print(f"problem: {problem}", file=sys.stderr)
    if problems:
        sys.exit(1)
    print(f"{args.command}: no problems found")
    if url := os.environ.get("HEARTBEAT_URL"):
        ping(url)


def log(message: str) -> None:
    print(message, flush=True)


def copy(rclone: Rclone, source: str, dest: str, snapshots: str, today: datetime.date) -> list[str]:
    # Snapshot first: every index file its collections.json names is already in R2, and stays 7 days after it's
    # retired, so the copy below always gets it.
    for key in SNAPSHOT_FILES:
        log(f"snapshot {key}")
        rclone.copyto(source + key, f"{snapshots}{today}/{key}")
    for prefix in IMMUTABLE:
        log(f"copy {prefix}")
        if rclone.copy(source + prefix, dest + prefix):
            rclone.check(source + prefix, dest + prefix)
        else:
            log(f"{source}{prefix} is empty")
    return []


def verify(
    rclone: Rclone, dest: str, snapshots: str, today: datetime.date, work: Path, sample: int, rng: random.Random
) -> list[str]:
    problems: list[str] = []
    day, config, listing, state = _latest_snapshot(rclone, snapshots, today, problems)

    # Only the content-addressed prefixes, so this also checks R2 itself (--dest r2:).
    have: dict[str, dict[str, int]] = {}
    for prefix in IMMUTABLE:
        bucket, sub = prefix.split("/", 1)
        have[bucket] = {f"{sub}/{key}": size for key, size in rclone.files(dest + prefix).items()}
    log(
        f"backup has {len(have['alai-index'])} index files, {len(have['alai-dl'])} artifacts, "
        + f"{len(have['alai-state'])} conflict artifacts"
    )

    # Only the current index files, as stale ones pile up. Reading every current one also keeps it from the
    # lifecycle rule that deletes index files unread for 180 days.
    current = {_fill(config["formats"]["1"]["index"], sha256): name for name, sha256 in _collections(listing)}
    present = {k: have["alai-index"][k] for k in current if k in have["alai-index"]}
    log(f"downloading the {len(present)} current index files")
    _ = rclone.copy(dest + "alai-index", str(work / "alai-index"), present)
    problems += _check_hashes(work / "alai-index", present, dest + "alai-index/")

    versions = 0
    for key, name in current.items():
        if key not in have["alai-index"]:
            problems.append(f"{name}'s index file {key}, named by the {day} collections.json, isn't in {dest}")
            continue
        for line in _lines((work / "alai-index" / key).read_bytes()):
            versions += 1
            artifact = _artifact_key(config, line["sha256"])
            size = have["alai-dl"].get(artifact)
            if size is None:
                problems.append(f"{name} {line['vers']}'s artifact {artifact} isn't in {dest}")
            elif size != line["size"]:
                problems.append(f"{name} {line['vers']}'s artifact {artifact} is {size} bytes, not {line['size']}")
    log(f"checked the artifacts of the {versions} versions in the current index files are in {dest} at their size")

    conflicts = list(_saved_conflicts(state))
    for key in conflicts:
        if key not in have["alai-state"]:
            problems.append(f"conflict artifact alai-state/{key}, named by the {day} state.json, isn't in {dest}")
    log(f"checked the {len(conflicts)} conflict artifacts the {day} state.json names are in {dest}")

    picked = rng.sample(sorted(have["alai-dl"]), min(sample, len(have["alai-dl"])))
    log(f"hashing {len(picked)} random artifacts and every conflict artifact")
    for bucket, keys in (("alai-dl", picked), ("alai-state", sorted(have["alai-state"]))):
        if keys:
            digests = rclone.sha256(dest + bucket, keys)
            for k in keys:
                if k in digests:
                    problems += _check_digest(f"{dest}{bucket}/", k, digests[k])
                else:
                    problems.append(f"{dest}{bucket}/{k} couldn't be read")
    return problems


def restore_test(
    rclone: Rclone,
    dest: str,
    snapshots: str,
    today: datetime.date,
    work: Path,
    count: int,
    wanted: list[str],
    max_bytes: int,
    rng: random.Random,
    alai: list[str],
    alai_index: list[str],
) -> list[str]:
    problems: list[str] = []
    day, config, listing, _ = _latest_snapshot(rclone, snapshots, today, problems)
    if problems:
        return problems

    restored = work / "restored"
    for key in SNAPSHOT_FILES[:2]:
        rclone.copyto(f"{snapshots}{day}/{key}", str(restored / key.removeprefix("alai-index/")))
    # Resolving a collection can need any index file, so all of them are restored.
    current = {_fill(config["formats"]["1"]["index"], sha256): name for name, sha256 in _collections(listing)}
    log(f"restoring the {len(current)} index files the {day} collections.json names")
    _ = rclone.copy(dest + "alai-index", str(restored), current)
    lines: dict[str, list[dict[str, Any]]] = {}
    for key, name in current.items():
        path = restored / key
        if not path.exists():
            problems.append(f"{name}'s index file {key} wasn't restored")
        elif hashlib.sha256(data := path.read_bytes()).hexdigest() != _sha256_of(key):
            problems.append(f"{name}'s index file {key} doesn't match its sha256")
        else:
            lines[name] = _lines(data)
    if problems:
        return problems
    log(f"each of the {len(current)} index files restored matches the sha256 in its name")

    # alai gets a copy of the index whose dl template points at the restored artifacts on disk.
    dl = work / "dl"
    client = work / "client"
    client.mkdir()
    for entry in restored.iterdir():
        if entry.name != "config.json":
            (client / entry.name).symlink_to(entry)
    client_config = dict(config, dl=dl.as_uri() + urlsplit(config["dl"]).path)
    _ = (client / "config.json").write_text(json.dumps(client_config))
    project = work / "project"
    project.mkdir()
    alai_args = [
        "--no-config",
        "--no-progress",
        "--cache-dir",
        str(work / "cache"),
        "--project",
        str(project),
        "--default-index",
        client.as_uri() + "/",
    ]

    def lock(names: list[str]) -> subprocess.CompletedProcess[str]:
        _ = (project / "alai.toml").write_text(f"[project]\ncollections = {json.dumps(names)}\n")
        (project / "alai.lock").unlink(missing_ok=True)
        return _run([*alai, "lock", *alai_args])

    if wanted:
        candidates = wanted
        count = len(wanted)
    else:
        # Some collections can't lock anywhere, as their dependencies were never met on Galaxy, so try others.
        candidates = [
            n
            for n, ls in lines.items()
            if ls and any(not line["yanked"] for line in ls) and sum(line["size"] for line in ls) <= max_bytes
        ]
        rng.shuffle(candidates)
        candidates = candidates[: count * 4]
    picked: list[str] = []
    for name in candidates:
        if len(picked) == count:
            break
        log(f"trying {name}")
        res = lock([name])
        if res.returncode == 0:
            picked.append(name)
        elif wanted:
            problems.append(f"locking {name} from the restored index failed: {res.stderr.strip()}")
        else:
            log(f"skipping {name}, which doesn't lock: {res.stderr.strip()}")
    if len(picked) < count and not problems:
        problems.append(f"only {len(picked)} of {count} collections locked from the restored index: {picked}")
    if problems:
        return problems
    log(f"restoring {', '.join(picked)}")
    res = lock(picked)
    if res.returncode:
        return [f"locking {picked} from the restored index failed: {res.stderr.strip()}"]
    locked = tomllib.loads((project / "alai.lock").read_text())["collection"]
    for c in locked:
        log(f"locked {c['name']} {c['version']} sha256 {c['sha256']}")

    artifacts = {_artifact_key(config, c["sha256"]) for c in locked}
    artifacts |= {_artifact_key(config, line["sha256"]) for name in picked for line in lines[name]}
    log(f"restoring {len(artifacts)} artifacts")
    _ = rclone.copy(dest + "alai-dl", str(dl), artifacts)
    problems += _check_hashes(dl, dict.fromkeys(artifacts, -1), dest + "alai-dl/")
    if problems:
        return problems

    res = _run([*alai, "sync", "--frozen", *alai_args])
    if res.returncode:
        return [f"installing {picked} from the restored index failed: {res.stderr.strip()}"]
    for c in locked:
        ns, name = c["name"].split(".", 1)
        if (project / "collections" / "ansible_collections" / ns / name / "MANIFEST.json").is_file():
            log(f"installed {c['name']} {c['version']}")
        else:
            problems.append(f"{c['name']} {c['version']} wasn't installed")
    log(f"installed {len(locked)} collections")

    # Without targets, generate-plugins makes plugins.jsonl, which isn't backed up. So it gets a tree that only
    # lists the collections picked.
    plugins = work / "plugins"
    plugins.mkdir()
    for entry in restored.iterdir():
        if entry.name != "collections.json":
            (plugins / entry.name).symlink_to(entry)
    sample: dict[str, Any] = {"v": listing["v"], "namespaces": {}}
    for name in picked:
        ns, coll = name.split(".", 1)
        sample["namespaces"].setdefault(ns, {})[coll] = listing["namespaces"][ns][coll]
    _ = (plugins / "collections.json").write_text(json.dumps(sample))
    res = _run(
        [
            *alai_index,
            "generate-plugins",
            "--quiet",
            "--output-format",
            "json",
            "--root",
            str(plugins),
            "--dl",
            str(dl),
            "--plugin-cache",
            str(work / "plugin-cache"),
        ],
        show_stdout=False,  # JSON, logged a version a line below
    )
    if res.returncode:
        return [*problems, f"alai-index generate-plugins failed: {res.stderr.strip()}"]
    out = json.loads(res.stdout)
    for v in out["versions"]:
        if v["result"] == "extracted":
            log(f"extracted the plugins of {v['name']} {v['version']}")
        else:
            problems.append(f"extracting {v['name']} {v['version']}'s plugins: {v['result']} {v.get('details')}")
    if out["outdated"]:
        problems.append(f"alai-index left versions outdated: {out['outdated']}")
    if not (plugins / "plugins.jsonl").is_file():
        problems.append("alai-index didn't write plugins.jsonl")
    log(f"extracted the plugins of {len(out['versions'])} versions")
    return problems


def _run(cmd: list[str], show_stdout: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command, logging it and its output."""
    log(f"$ {shlex.join(cmd)}")
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    for line in ((res.stdout if show_stdout else "") + res.stderr).splitlines():
        log(f"    {line}")
    if res.returncode:
        log(f"    exit code {res.returncode}")
    return res


def _latest_snapshot(
    rclone: Rclone, snapshots: str, today: datetime.date, problems: list[str]
) -> tuple[datetime.date, dict[str, Any], dict[str, Any], dict[str, Any]]:
    days = []
    for d in rclone.dirs(snapshots):
        try:
            days.append(datetime.date.fromisoformat(d))
        except ValueError:
            continue
    if not days:
        raise SystemExit(f"no snapshots in {snapshots}")
    day = max(days)
    if today - day > MAX_SNAPSHOT_AGE:
        problems.append(f"the newest snapshot is from {day}, more than {MAX_SNAPSHOT_AGE.days} days ago")
    config, listing, state = (json.loads(rclone.cat(f"{snapshots}{day}/{key}")) for key in SNAPSHOT_FILES)
    log(f"newest snapshot: {day}")
    return day, config, listing, state


def _collections(listing: dict[str, Any]) -> Iterator[tuple[str, str]]:
    for ns, names in listing["namespaces"].items():
        for name, entry in names.items():
            yield f"{ns}.{name}", entry["sha256"]


def _lines(data: bytes) -> list[dict[str, Any]]:
    return [json.loads(line) for line in data.splitlines() if line.strip()]


def _fill(template: str, sha256: str) -> str:
    return template.replace("{sha256-prefix}", sha256[:2]).replace("{sha256-checksum}", sha256)


def _artifact_key(config: dict[str, Any], sha256: str) -> str:
    # dl.alai.sh serves the bucket's root, so the URL's path is the key.
    return _fill(urlsplit(config["dl"]).path.lstrip("/"), sha256)


def _sha256_of(key: str) -> str:
    return key.rsplit("/", 1)[-1].split(".", 1)[0]


def _check_hashes(root: Path, files: dict[str, int], where: str) -> list[str]:
    """A size of -1 skips the size check."""
    problems = []
    for key, size in sorted(files.items()):
        path = root / key
        if not path.is_file():
            problems.append(f"{where}{key} couldn't be downloaded")
            continue
        with path.open("rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
        if size not in (-1, path.stat().st_size):
            problems.append(f"{where}{key} is {path.stat().st_size} bytes, but was listed at {size}")
        else:
            problems += _check_digest(where, key, digest)
    return problems


def _check_digest(where: str, key: str, digest: str) -> list[str]:
    """A file's key ends in the sha256 it should have."""
    if digest != (expected := _sha256_of(key)):
        return [f"{where}{key} has sha256 {digest}, not {expected} as its name says"]
    log(f"ok {where}{key} sha256 {digest}")
    return []


def _saved_conflicts(state: dict[str, Any]) -> Iterator[str]:
    for conflict in state.get("conflicts", []):
        for observed in conflict.get("observed", []):
            if saved := observed.get("saved"):
                yield saved


def ping(url: str) -> None:
    # A failed ping only warns, as the heartbeat service alerts once pings stop.
    request = urllib.request.Request(url, headers={"User-Agent": "galaxy-sync-backup"})  # noqa: S310
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=10):  # noqa: S310
                return
        except OSError as e:
            if attempt == 2:
                print(f"warning: heartbeat ping failed: {e}", file=sys.stderr)
            else:
                time.sleep(5)
