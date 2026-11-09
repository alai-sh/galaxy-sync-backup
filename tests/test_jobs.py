import datetime
import json
import random
from pathlib import Path

import pytest
from conftest import LocalRclone

from galaxy_sync_backup.cli import copy, restore_test, verify
from galaxy_sync_backup.rclone import RcloneError

TODAY = datetime.date(2026, 11, 8)


@pytest.fixture
def backup(r2: Path, tmp_path: Path) -> tuple[str, str]:
    dest, snapshots = f"{tmp_path}/azure/backup/", f"{tmp_path}/azure/snapshots/"
    assert copy(LocalRclone(), f"{r2}/", dest, snapshots, TODAY) == []
    return dest, snapshots


def run_verify(backup: tuple[str, str], tmp_path: Path, today: datetime.date = TODAY) -> list[str]:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return verify(LocalRclone(), *backup, today, work, 1000, random.Random(0))


def artifacts(dest: str) -> list[Path]:
    return sorted(Path(dest, "alai-dl").rglob("*.tar.gz"))


def test_copy_backs_up_everything(r2: Path, backup: tuple[str, str]) -> None:
    dest, snapshots = backup
    assert len(artifacts(dest)) == 3
    assert len(list(Path(dest, "alai-index/index").rglob("*.jsonl"))) == 2
    assert len(list(Path(dest, "alai-state/conflicts").rglob("*.tar.gz"))) == 1
    assert not Path(dest, "alai-index/collections.json").exists()  # only in the snapshots
    for key in ("alai-index/config.json", "alai-index/collections.json", "alai-state/state.json"):
        assert Path(snapshots, str(TODAY), key).read_bytes() == (r2 / key).read_bytes()


def test_copy_again_copies_only_new_files(r2: Path, backup: tuple[str, str]) -> None:
    dest, snapshots = backup
    (r2 / "alai-dl/sha256/ff").mkdir()
    _ = (r2 / "alai-dl/sha256/ff/ff00.tar.gz").write_bytes(b"new")
    assert copy(LocalRclone(), f"{r2}/", dest, snapshots, TODAY + datetime.timedelta(days=1)) == []
    assert Path(dest, "alai-dl/sha256/ff/ff00.tar.gz").read_bytes() == b"new"
    assert Path(snapshots, str(TODAY + datetime.timedelta(days=1))).is_dir()


def test_copy_refuses_a_changed_file(r2: Path, backup: tuple[str, str]) -> None:
    dest, snapshots = backup
    victim = min((r2 / "alai-dl").rglob("*.tar.gz"))
    _ = victim.write_bytes(b"tampered with in R2")
    with pytest.raises(RcloneError):
        _ = copy(LocalRclone(), f"{r2}/", dest, snapshots, TODAY)
    assert artifacts(dest)[0].read_bytes() != b"tampered with in R2"


def test_verify_passes(backup: tuple[str, str], tmp_path: Path) -> None:
    assert run_verify(backup, tmp_path) == []


def test_verify_finds_a_corrupted_artifact(backup: tuple[str, str], tmp_path: Path) -> None:
    victim = artifacts(backup[0])[0]
    data = victim.read_bytes()
    _ = victim.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    [problem] = run_verify(backup, tmp_path)
    assert "has sha256" in problem


def test_verify_finds_a_missing_artifact(backup: tuple[str, str], tmp_path: Path) -> None:
    artifacts(backup[0])[0].unlink()
    [problem] = run_verify(backup, tmp_path)
    assert "artifact sha256/" in problem
    assert problem.endswith(f"isn't in {backup[0]}")


def test_verify_finds_a_missing_conflict(backup: tuple[str, str], tmp_path: Path) -> None:
    next(Path(backup[0], "alai-state").rglob("*.tar.gz")).unlink()
    [problem] = run_verify(backup, tmp_path)
    assert problem.startswith("conflict artifact alai-state/conflicts/")


def test_verify_finds_an_old_snapshot(backup: tuple[str, str], tmp_path: Path) -> None:
    [problem] = run_verify(backup, tmp_path, TODAY + datetime.timedelta(days=3))
    assert f"the newest snapshot is from {TODAY}" in problem


def test_verify_finds_a_missing_index_file(backup: tuple[str, str], tmp_path: Path) -> None:
    listing = json.loads(Path(backup[1], str(TODAY), "alai-index/collections.json").read_bytes())
    sha256 = listing["namespaces"]["ns"]["app"]["sha256"]
    Path(backup[0], "alai-index/index", sha256[:2], f"{sha256}.jsonl").unlink()
    [problem] = run_verify(backup, tmp_path)
    assert problem.startswith("ns.app's index file")


def run_restore(backup: tuple[str, str], tmp_path: Path, wanted: list[str]) -> list[str]:
    work = tmp_path / "restore"
    work.mkdir()
    return restore_test(
        LocalRclone(),
        *backup,
        TODAY,
        work,
        2,
        wanted,
        50 * 1024 * 1024,
        random.Random(0),
        alai=["alai"],
        alai_index=["alai-index"],
    )


def test_restore_test_installs_from_the_backup(backup: tuple[str, str], tmp_path: Path) -> None:
    assert run_restore(backup, tmp_path, []) == []
    installed = tmp_path / "restore/project/collections/ansible_collections/ns"
    assert sorted(p.name for p in installed.iterdir()) == ["app", "base"]


def test_restore_test_fails_on_a_missing_artifact(backup: tuple[str, str], tmp_path: Path) -> None:
    for artifact in artifacts(backup[0]):
        artifact.unlink()
    problems = run_restore(backup, tmp_path, ["ns.app"])
    assert problems
    assert all("couldn't be downloaded" in p for p in problems)


def test_verify_runs_against_r2_itself(r2: Path, backup: tuple[str, str], tmp_path: Path) -> None:
    # R2's buckets also hold config.json, collections.json and state.json, which aren't named by a sha256.
    assert run_verify((f"{r2}/", backup[1]), tmp_path) == []


def test_verify_skips_stale_index_files(backup: tuple[str, str], tmp_path: Path) -> None:
    stale = Path(backup[0], "alai-index/index/00/00stale.jsonl")
    stale.parent.mkdir(parents=True)
    _ = stale.write_bytes(b"doesn't hash to its name, and nothing names it\n")
    assert run_verify(backup, tmp_path) == []
    assert not (tmp_path / "work/alai-index/index/00").exists()
