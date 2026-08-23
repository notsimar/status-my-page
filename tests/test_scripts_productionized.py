#!/usr/bin/env python3
"""Tests for the productionized install.sh / dev-setup.sh flag interfaces.

Verifies non-interactive modes, upgrade idempotency, --force-env, port/host
options, and the new lib.sh helpers (dotenv_key, creds_in_env).
"""
import subprocess
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BASE_ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp"}


@pytest.fixture(scope="module")
def deploy_copy(tmp_path_factory):
    """Full repo copy without runtime artifacts."""
    src = tmp_path_factory.mktemp("repo") / "copy"
    shutil.copytree(REPO, src, ignore=shutil.ignore_patterns(
        ".venv", "__pycache__", ".git", "instance", "logs", "archives",
        ".env.local", ".coverage", "dist", "dogfood-output"))
    return src


def _install(deploy: Path, target: Path, *args, timeout=600):
    return subprocess.run(
        ["bash", str(deploy / "install.sh"), *args, str(target)],
        capture_output=True, text=True,
        env={**BASE_ENV}, cwd=str(deploy), timeout=timeout)


class TestLibHelpers:
    def test_dotenv_key_reads_single_quoted_value(self, tmp_path):
        env = tmp_path / "e.env"
        env.write_text("STATUS_ADMIN_PASS_HASH='scrypt:32768:8:1$a$b'\n")
        r = subprocess.run(
            ["bash", "-c",
             f'source "{REPO}/lib.sh"; dotenv_key "{env}" STATUS_ADMIN_PASS_HASH'],
            capture_output=True, text=True, env=BASE_ENV)
        assert r.stdout == "scrypt:32768:8:1$a$b"

    def test_dotenv_key_handles_dollars(self, tmp_path):
        """The whole point: $ must survive without sourcing."""
        env = tmp_path / "e.env"
        env.write_text("K='a$1$b'\n")  # written by Python: literal dollars
        r = subprocess.run(
            ["bash", "-c",
             f'source "{REPO}/lib.sh"; dotenv_key "{env}" K'],
            capture_output=True, text=True, env=BASE_ENV)
        assert r.stdout == "a$1$b", r.stderr

    def test_creds_in_env_true_and_false(self, tmp_path):
        env = tmp_path / "e.env"
        script = f'source "{REPO}/lib.sh"; ENV_FILE="{env}"; creds_in_env'
        r = subprocess.run(["bash", "-c", script + "; echo rc=$?"],
                           capture_output=True, text=True, env=BASE_ENV)
        assert "rc=1" in r.stdout  # missing file -> false
        env.write_text("STATUS_ADMIN_PASS_HASH='scrypt:1$x$y'\n")
        r = subprocess.run(["bash", "-c", script + "; echo rc=$?"],
                           capture_output=True, text=True, env=BASE_ENV)
        assert "rc=0" in r.stdout


class TestInstallNonInteractive:
    def test_rejects_unknown_option(self, deploy_copy, tmp_path):
        r = _install(deploy_copy, tmp_path / "t", "--bogus-flag")
        assert r.returncode == 1
        assert "Unknown option" in r.stderr
        # Must fail BEFORE creating anything
        assert not (tmp_path / "t" / "config.yaml").exists()

    def test_rejects_invalid_port(self, deploy_copy, tmp_path):
        r = _install(deploy_copy, tmp_path / "t", "--port", "99999")
        assert r.returncode == 1
        assert "Port" in r.stderr or "port" in r.stderr

    def test_help_exits_zero_without_installing(self, deploy_copy, tmp_path):
        r = subprocess.run(["bash", str(deploy_copy / "install.sh"), "--help"],
                           capture_output=True, text=True, env=BASE_ENV)
        assert r.returncode == 0
        assert "--admin-pass" in r.stdout + r.stderr
        assert not list(tmp_path.iterdir())  # nothing created


class TestInstallUpgrade:
    """Full end-to-end runs (slow; marked so they can be selected)."""

    pytestmark = pytest.mark.slow

    def test_upgrade_preserves_credentials(self, deploy_copy, tmp_path):
        target = tmp_path / "target"
        r1 = _install(deploy_copy, target,
                      "--admin-user", "ops", "--admin-pass", "supersecret9")
        assert r1.returncode == 0, r1.stderr[-500:]
        hash1 = [l for l in (target / ".env.local").read_text().splitlines()
                 if l.startswith("STATUS_ADMIN_PASS_HASH")][0]

        r2 = _install(deploy_copy, target,
                      "--admin-user", "someoneelse",
                      "--admin-pass", "differentpass1")
        assert r2.returncode == 0, r2.stderr[-500:]
        hash2 = [l for l in (target / ".env.local").read_text().splitlines()
                 if l.startswith("STATUS_ADMIN_PASS_HASH")][0]
        assert hash1 == hash2, "upgrade must keep existing hash"

    def test_force_env_replaces_credentials(self, deploy_copy, tmp_path):
        target = tmp_path / "target"
        assert _install(deploy_copy, target, "--admin-user", "ops",
                        "--admin-pass", "supersecret9").returncode == 0
        r = _install(deploy_copy, target, "--admin-user", "newadmin",
                     "--admin-pass", "anotherpass99", "--force-env")
        assert r.returncode == 0, r.stderr[-500:]
        cfg = (target / "config.yaml").read_text()
        assert "user: newadmin" in cfg

    def test_installed_app_boots_and_logs_in(self, deploy_copy, tmp_path):
        import os
        target = tmp_path / "target"
        assert _install(deploy_copy, target, "--admin-user", "ops",
                        "--admin-pass", "supersecret9").returncode == 0
        r = subprocess.run(
            [str(target / ".venv" / "bin" / "python"), "-c", f"""
import sys; sys.path.insert(0, {str(target)!r})
from dotenv import load_dotenv
load_dotenv({str(target / '.env.local')!r})
import app
c = app.app.test_client()
assert c.get('/').status_code == 200
r = c.post('/login', json={{'user': 'ops', 'pass': 'supersecret9'}})
assert r.status_code == 200, r.get_json()
print('OK')
"""],
            capture_output=True, text=True, cwd=str(target), timeout=120,
            env={"PATH": BASE_ENV["PATH"], "HOME": "/tmp"})
        assert "OK" in r.stdout, r.stderr[-400:]


class TestDevSetupNonInteractive:
    def test_ci_mode_uses_env_credentials(self, deploy_copy, tmp_path):
        dev = tmp_path / "dev"
        shutil.copytree(deploy_copy, dev, ignore=shutil.ignore_patterns(
            ".venv", "__pycache__", "instance", "logs", "archives",
            ".env.local"))
        r = subprocess.run(
            ["bash", str(dev / "dev-setup.sh"), "--port", "3000"],
            capture_output=True, text=True,
            env={**BASE_ENV, "CI": "1", "SP_ADMIN_USER": "devops",
                 "SP_ADMIN_PASS": "devpassword1"},
            cwd=str(dev), timeout=600)
        assert r.returncode == 0, r.stderr[-800:]
        denv = (dev / ".env.local").read_text()
        assert "DEV_ADMIN_USER=devops" in denv
        assert "DEV_PORT=3000" in denv
        assert "STATUS_DISABLE_HEALTHCHECKS=1" in denv  # dev default: off

    def test_enable_hc_flag_flips_healthchecks_on(self, deploy_copy, tmp_path):
        dev = tmp_path / "dev"
        shutil.copytree(deploy_copy, dev, ignore=shutil.ignore_patterns(
            ".venv", "__pycache__", "instance", "logs", "archives",
            ".env.local"))
        r = subprocess.run(
            ["bash", str(dev / "dev-setup.sh"), "--enable-hc"],
            capture_output=True, text=True,
            env={**BASE_ENV, "CI": "1", "SP_ADMIN_PASS": "devpassword1"},
            cwd=str(dev), timeout=600)
        assert r.returncode == 0, r.stderr[-800:]
        assert "STATUS_DISABLE_HEALTHCHECKS=0" in \
            (dev / ".env.local").read_text()

    def test_no_pull_flag_skips_git_step(self, deploy_copy, tmp_path):
        dev = tmp_path / "dev"
        shutil.copytree(deploy_copy, dev, ignore=shutil.ignore_patterns(
            ".venv", "__pycache__", "instance", "logs", "archives",
            ".env.local"))
        r = subprocess.run(
            ["bash", str(dev / "dev-setup.sh"), "--no-pull"],
            capture_output=True, text=True,
            env={**BASE_ENV, "CI": "1", "SP_ADMIN_PASS": "devpassword1"},
            cwd=str(dev), timeout=600)
        assert r.returncode == 0, r.stderr[-800:]
        assert "Pull skipped" in r.stdout

    def test_dev_setup_help(self, deploy_copy):
        r = subprocess.run(["bash", str(deploy_copy / "dev-setup.sh"),
                            "--help"],
                           capture_output=True, text=True, env=BASE_ENV)
        assert r.returncode == 0
        assert "--enable-hc" in r.stdout + r.stderr
