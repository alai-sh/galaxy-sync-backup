import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from collections.abc import Iterable
from pathlib import Path

import pytest

from galaxy_sync_backup.rclone import Rclone, RcloneError


class LocalRclone(Rclone):
    def files(self, path: str) -> dict[str, int]:
        root = Path(path)
        return {p.relative_to(root).as_posix(): p.stat().st_size for p in root.rglob("*") if p.is_file()}

    def dirs(self, path: str) -> list[str]:
        return [p.name for p in Path(path).iterdir() if p.is_dir()] if Path(path).is_dir() else []

    def cat(self, path: str) -> bytes:
        return Path(path).read_bytes()

    def copy(self, src: str, dst: str, files: Iterable[str] | None = None) -> bool:
        if not Path(src).is_dir():
            return False
        for rel in self.files(src) if files is None else files:
            source, target = Path(src) / rel, Path(dst) / rel
            if not source.is_file():
                continue  # rclone skips a --files-from entry that isn't there
            if target.exists():
                if target.stat().st_size != source.stat().st_size:
                    raise RcloneError(f"{rel}: immutable file modified")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = shutil.copyfile(source, target)
        return True

    def sha256(self, path: str, files: Iterable[str]) -> dict[str, str]:
        return {
            f: hashlib.sha256((Path(path) / f).read_bytes()).hexdigest() for f in files if (Path(path) / f).is_file()
        }

    def copyto(self, src: str, dst: str) -> None:
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.copyfile(src, dst)

    def check(self, src: str, dst: str) -> None:
        theirs = self.files(dst)
        for rel, size in self.files(src).items():
            if theirs.get(rel) != size:
                raise RcloneError(f"{rel} differs")


if exe := os.environ.get("TEST_RCLONE"):
    # Runs the tests with a real rclone instead, which also checks LocalRclone behaves like it.
    def LocalRclone() -> Rclone:
        return Rclone(exe)


def build_collection(path: Path, name: str, version: str, dependencies: dict[str, str] | None = None) -> Path:
    namespace, short = name.split(".")
    files = {
        "meta/runtime.yml": b"requires_ansible: '>=2.15.0'\n",
        "plugins/modules/ping.py": b'DOCUMENTATION = """\nmodule: ping\nshort_description: Ping\n"""\n',
    }
    dirs = ["meta", "plugins", "plugins/modules"]
    entries = [{"name": d, "ftype": "dir", "chksum_type": None, "chksum_sha256": None} for d in [".", *dirs]]
    entries += [
        {"name": f, "ftype": "file", "chksum_type": "sha256", "chksum_sha256": hashlib.sha256(data).hexdigest()}
        for f, data in files.items()
    ]
    files_json = json.dumps({"files": entries, "format": 1}).encode()
    manifest = {
        "collection_info": {
            "namespace": namespace,
            "name": short,
            "version": version,
            "dependencies": dependencies or {},
        },
        "file_manifest_file": {
            "name": "FILES.json",
            "ftype": "file",
            "chksum_type": "sha256",
            "chksum_sha256": hashlib.sha256(files_json).hexdigest(),
            "format": 1,
        },
        "format": 1,
    }
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        for d in dirs:
            info = tarfile.TarInfo(d)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)
        for member, data in [
            ("MANIFEST.json", json.dumps(manifest).encode()),
            ("FILES.json", files_json),
            *files.items(),
        ]:
            info = tarfile.TarInfo(member)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    path.write_bytes(buffer.getvalue())
    return path


@pytest.fixture
def r2(tmp_path: Path) -> Path:
    root = tmp_path / "r2"
    index, dl, state = root / "alai-index", root / "alai-dl", root / "alai-state"
    tarballs = tmp_path / "tarballs"
    tarballs.mkdir()
    common = ["--root", str(index), "--dl", str(dl), "--plugin-cache", str(tmp_path / "plugin-cache"), "--quiet"]
    _ = subprocess.run(
        [
            "alai-index",
            "config",
            *common,
            "--index",
            "index/{sha256-prefix}/{sha256-checksum}.jsonl",
            "--dl-template",
            "https://dl.alai.sh/sha256/{sha256-prefix}/{sha256-checksum}.tar.gz",
        ],
        check=True,
    )
    added = [
        build_collection(tarballs / "base-1.0.0.tar.gz", "ns.base", "1.0.0"),
        build_collection(tarballs / "base-1.1.0.tar.gz", "ns.base", "1.1.0"),
        build_collection(tarballs / "app-1.0.0.tar.gz", "ns.app", "1.0.0", {"ns.base": ">=1.0.0"}),
    ]
    _ = subprocess.run(["alai-index", "add", *common, *map(str, added)], check=True, stdout=subprocess.DEVNULL)
    (index / "plugins.jsonl").unlink()  # not backed up, so nothing should need it

    conflict = b"another artifact Galaxy served"
    sha256 = hashlib.sha256(conflict).hexdigest()
    saved = f"conflicts/{sha256[:2]}/{sha256}.tar.gz"
    (state / saved).parent.mkdir(parents=True)
    _ = (state / saved).write_bytes(conflict)
    _ = (state / "state.json").write_text(
        json.dumps(
            {
                # galaxy-sync's SyncState: conflicts is a list of Conflict records.
                "conflicts": [
                    {"collection": "ns.base", "version": "1.0.0", "observed": [{"sha256": sha256, "saved": saved}]}
                ],
            }
        )
    )
    return root
