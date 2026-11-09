"""The rclone commands the jobs run. Remotes come from RCLONE_CONFIG_* variables, so no credential is on disk."""

import contextlib
import json
import subprocess
import tempfile
from collections.abc import Generator, Iterable

DIRECTORY_NOT_FOUND = 3  # rclone's exit code when the source directory doesn't exist


class RcloneError(Exception):
    pass


class Rclone:
    def __init__(self, exe: str = "rclone", transfers: int = 16) -> None:
        self.exe: str = exe
        self.transfers: int = transfers

    def _run(self, *args: str, capture: bool = False, missing_ok: bool = False) -> bytes | None:
        res = subprocess.run(
            [self.exe, *args],
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            check=False,
        )
        if missing_ok and res.returncode == DIRECTORY_NOT_FOUND:
            return None
        if res.returncode:
            detail = res.stderr.decode(errors="replace").strip() if capture else "see rclone's output above"
            raise RcloneError(
                f"rclone {args[0]} {' '.join(args[1:3])} failed with exit code {res.returncode}: {detail}"
            )
        return res.stdout or b""

    def files(self, path: str) -> dict[str, int]:
        out = self._run(
            "lsjson", "-R", "--files-only", "--no-modtime", "--no-mimetype", path, capture=True, missing_ok=True
        )
        return {e["Path"]: e["Size"] for e in json.loads(out or b"[]")}

    def dirs(self, path: str) -> list[str]:
        out = self._run("lsf", "--dirs-only", path, capture=True, missing_ok=True)
        return [line.rstrip("/") for line in (out or b"").decode().splitlines() if line]

    def cat(self, path: str) -> bytes:
        return self._run("cat", path, capture=True) or b""

    def copy(self, src: str, dst: str, files: Iterable[str] | None = None) -> bool:
        # --immutable: a file in both with a different size is an error to look at, never replaced.
        args = ["copy", src, dst, "--immutable", "--size-only", f"--transfers={self.transfers}"]
        # A progress line a minute in the job's log, which a long copy otherwise leaves silent.
        args += ["--stats=1m", "--stats-one-line", "--stats-log-level=NOTICE"]
        if files is None:
            return self._run(*args, missing_ok=True) is not None
        with _files_from(files) as listing:
            return self._run(*args, listing, "--no-traverse", missing_ok=True) is not None

    def sha256(self, path: str, files: Iterable[str]) -> dict[str, str]:
        # --download hashes each file as it streams, so nothing is written to disk.
        with _files_from(files) as listing:
            out = self._run(
                "hashsum", "sha256", "--download", f"--transfers={self.transfers}", listing, path, capture=True
            )
        return {rel: digest for digest, rel in (line.split("  ", 1) for line in (out or b"").decode().splitlines())}

    def copyto(self, src: str, dst: str) -> None:
        # Always upload, as a changed state.json can keep the same size.
        _ = self._run("copyto", src, dst, "--ignore-times")

    def check(self, src: str, dst: str) -> None:
        # R2's ETag is the MD5 of a one-part upload, as galaxy-sync makes, and Azure keeps the MD5 rclone sends,
        # so this compares hashes without downloading anything.
        _ = self._run("check", src, dst, "--one-way")


@contextlib.contextmanager
def _files_from(files: Iterable[str]) -> Generator[str]:
    with tempfile.NamedTemporaryFile("w", suffix=".txt") as listing:
        _ = listing.write("".join(f"{f}\n" for f in files))
        listing.flush()
        yield f"--files-from-raw={listing.name}"
