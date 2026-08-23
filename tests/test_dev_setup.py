"""Tests for dev-setup.sh interactive configuration.

Drives the script with piped stdin simulating user input and asserts the
resulting .env.local content, defaults handling, and validation rejections.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "dev-setup.sh"

BASE_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "HOME": "/tmp",
}


def _run(tmp_path: Path, repo_copy: Path, stdin: str) -> subprocess.CompletedProcess:
    """Copy a minimal repo skeleton to tmp, run dev-setup.sh with piped stdin."""
    deploy = tmp_path / "deploy"
    shutil.copytree(repo_copy, deploy,
                    ignore=shutil.ignore_patterns(
                        ".venv", "__pycache__", ".git", "instance",
                        "logs", "archives", ".env.local", ".coverage",
                        "dist", "dogfood-output"))
    (deploy / "requirements.txt").write_text("flask\npyyaml\nwerkzeug\n")
    env = {**BASE_ENV}
    r = subprocess.run(
        ["bash", str(deploy / "dev-setup.sh")],
        input=stdin, capture_output=True, text=True,
        cwd=str(deploy), env=env, timeout=120,
    )
    return r


def _run_in_place(target: Path, stdin: str) -> subprocess.CompletedProcess:
    """Run dev-setup.sh directly inside an existing deploy dir."""
    return subprocess.run(
        ["bash", str(target / "dev-setup.sh")],
        input=stdin, capture_output=True, text=True,
        cwd=str(target), env={**BASE_ENV, "HOME": "/tmp"}, timeout=120,
    )


@pytest.fixture(scope="module")
def repo_skeleton(tmp_path_factory):
    """A lightweight stand-in for the real repo (real scripts + modules)."""
    src = tmp_path_factory.mktemp("repo")
    for f in ("dev-setup.sh", "lib.sh", "app.py", "constants.py",
              "healthcheck.py", "input_filter.py", "config.yaml",
              "requirements.txt"):
        src_f = REPO / f
        if src_f.exists():
            shutil.copy(src_f, src / f)
    # statuspage package
    sp = src / "statuspage"
    sp.mkdir(exist_ok=True)
    for f in (REPO / "statuspage").glob("*.py"):
        shutil.copy(f, sp / f.name)
    (sp / "__init__.py").touch()
    sc = src / "scripts"
    sc.mkdir(exist_ok=True)
    for f in (REPO / "scripts").glob("*.sh"):
        shutil.copy(f, sc / f.name)
    return src


GOOD_INPUT = "admin\ndevpassword\ndevpassword\n\n\n\n"


class TestInteractivePrompts:
    def test_defaults_accepted_with_blank_input(self, tmp_path, repo_skeleton):
        """Piping newlines accepts all defaults."""
        r = _run(tmp_path, repo_skeleton,
                  "admin\ndevpassword\ndevpassword\n\n\n\n")
        assert r.returncode == 0, r.stderr[-500:]
        env_file = tmp_path / "deploy" / ".env.local"
        assert env_file.exists()

        content = env_file.read_text()
        assert "STATUS_DISABLE_HEALTHCHECKS=1" in content      # default Y
        assert "DEV_ADMIN_USER=admin" in content
        assert "DEV_PORT=8920" in content
        # single-quoted hash (shell-safe)
        assert "STATUS_ADMIN_PASS_HASH='" in content

    def test_custom_values_recorded(self, tmp_path, repo_skeleton):
        r = _run(tmp_path, repo_skeleton,
                 "opsuser\nsecret123\nsecret123\n3000\nn\n\n")
        assert r.returncode == 0, r.stderr[-500:]
        env_file = tmp_path / "deploy" / ".env.local"
        content = env_file.read_text()
        assert "DEV_ADMIN_USER=opsuser" in content
        assert "DEV_PORT=3000" in content
        assert "STATUS_DISABLE_HEALTHCHECKS=0" in content

    def test_password_mismatch_reprompts(self, tmp_path, repo_skeleton):
        # mismatch once, then matching pair
        stdin = "admin\npw1\npw2\ndevpassword\ndevpassword\n\n\n\n"
        r = _run(tmp_path, repo_skeleton, stdin)
        assert r.returncode == 0, r.stderr[-800:]
        assert "do not match" in (r.stdout + r.stderr).lower()

    def test_invalid_port_reprompts(self, tmp_path, repo_skeleton):
        stdin = "admin\ndevpassword\ndevpassword\n99999\n3000\n\n\n"
        r = _run(tmp_path, repo_skeleton, stdin)
        assert r.returncode == 0, r.stderr[-800:]
        env_file = tmp_path / "deploy" / ".env.local"
        assert "DEV_PORT=3000" in env_file.read_text()

    def test_existing_password_kept_when_declined(self, tmp_path, repo_skeleton):
        # First run establishes credentials
        r = _run(
            tmp_path, repo_skeleton,
            "admin\ndevpassword\ndevpassword\n\n\n\n")
        assert r.returncode == 0, r.stderr[-500:]
        deploy = tmp_path / "deploy"
        before = (deploy / ".env.local").read_text()

        # Second run declines the reset — run in-place, existing hash kept
        r = _run_in_place(deploy, "\n\n\n\n\n")  # user, reset-declined, port, hc, logo
        assert r.returncode == 0, r.stderr[-500:]
        after = (deploy / ".env.local").read_text()
        hash_before = [l for l in before.splitlines()
                       if l.startswith("STATUS_ADMIN_PASS_HASH=")][0]
        hash_after = [l for l in after.splitlines()
                      if l.startswith("STATUS_ADMIN_PASS_HASH=")][0]
        assert hash_before == hash_after, "declined reset should keep the hash"


class TestEnvFileSafety:
    def test_hash_is_single_quoted(self, tmp_path, repo_skeleton):
        _run(tmp_path, repo_skeleton, GOOD_INPUT)
        env = (tmp_path / "deploy" / ".env.local").read_text()
        assert "STATUS_ADMIN_PASS_HASH='scrypt:" in env

    def test_env_file_permissions_600(self, tmp_path, repo_skeleton):
        _run(tmp_path, repo_skeleton, GOOD_INPUT)
        env = tmp_path / "deploy" / ".env.local"
        assert (env.stat().st_mode & 0o777) == 0o600

    def test_values_survive_bash_source(self, tmp_path, repo_skeleton):
        """The whole point of the quoting fix: source must not truncate $."""
        _run(tmp_path, repo_skeleton, GOOD_INPUT)
        env = tmp_path / "deploy" / ".env.local"
        r = subprocess.run(
            ["bash", "-c",
             f'set -a; source "{env}"; set +a; '
             'printf "%s" "$STATUS_ADMIN_PASS_HASH"'],
            capture_output=True, text=True)
        sourced = r.stdout
        assert "$" in sourced                      # dollars survived sourcing
        assert len(sourced) >= 100                 # full-length scrypt hash
