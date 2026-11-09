import importlib.util
import subprocess
from pathlib import Path

spec = importlib.util.spec_from_file_location("configure", Path(__file__).parent.parent / "scripts/configure.py")
assert spec and spec.loader
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


def test_env_round_trips_through_bash(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    values = {"HEARTBEAT_COPY_URL": "https://hc-ping.com/abc?x=1&y=$HOME", "IMAGE": "ghcr.io/a/b@sha256:00"}
    configure.dump(path, values)
    assert configure.load(path) == values
    assert path.stat().st_mode & 0o777 == 0o600
    out = subprocess.run(
        ["bash", "-c", f'set -a; source {path}; echo "$HEARTBEAT_COPY_URL"'], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == values["HEARTBEAT_COPY_URL"]


def test_restore_never_touches_the_read_only_remote(tmp_path: Path, monkeypatch) -> None:
    env = {
        "RCLONE_CONFIG_R2_ACCESS_KEY_ID": "r" * 32,
        "RCLONE_CONFIG_R2_ENDPOINT": f"https://{'a' * 32}.r2.cloudflarestorage.com",
    }
    answers = iter(["store1", "", "b" * 32])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(configure.getpass, "getpass", lambda _: "c" * 64)
    out = configure.restore(env)
    assert not any(k.startswith("RCLONE_CONFIG_R2_") for k in out)
    assert out["RCLONE_CONFIG_R2W_ENDPOINT"] == f"https://{'a' * 32}.r2.cloudflarestorage.com"
    assert out["RCLONE_CONFIG_R2W_ACCESS_KEY_ID"] == "b" * 32
