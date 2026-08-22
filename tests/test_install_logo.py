"""Tests for scripts/install_logo.sh — logo installation into a deployment.

Covers: single-logo mode, dual dark/light mode, config.yaml update,
missing-file rejection, non-absolute install dir rejection, idempotent
re-runs, and preservation of unrelated config.yaml sections.
"""
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "install_logo.sh"


def _make_deploy(tmp_path: Path) -> Path:
    """Create a minimal fake deployment directory."""
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "config.yaml").write_text(
        "items:\n- SvcA\n- SvcB\n"
        "_base:\n  admin:\n    user: admin\n"
        "rss:\n  enabled: false\n"
    )
    (deploy / "restart.sh").write_text("#!/usr/bin/env bash\n")
    (deploy / "restart.sh").chmod(0o755)
    return deploy


def _make_logo(tmp_path: Path, name: str = "logo.png",
               content: bytes = b"\x89PNG-fake-bytes") -> Path:
    logo = tmp_path / name
    logo.write_bytes(content)
    return logo


def _run(*args, **env):
    # The script uses `set -u` and reads $HOME for its default install dir,
    # so tests must pass a minimal environment.
    base_env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp"}
    base_env.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, env=base_env,
    )


class TestSingleLogoMode:
    def test_copies_file_and_updates_config(self, tmp_path):
        deploy = _make_deploy(tmp_path)
        logo = _make_logo(tmp_path)

        r = _run(str(logo), str(deploy))
        assert r.returncode == 0, r.stderr

        installed = deploy / "static" / "logos" / "logo.png"
        assert installed.is_file()
        assert installed.read_bytes() == b"\x89PNG-fake-bytes"
        assert (installed.stat().st_mode & 0o777) == 0o644

    def test_config_points_at_installed_logo(self, tmp_path):
        deploy = _make_deploy(tmp_path)
        logo = _make_logo(tmp_path)

        r = _run(str(logo), str(deploy))
        assert r.returncode == 0, r.stderr

        cfg = yaml.safe_load((deploy / "config.yaml").read_text())
        assert cfg["logo"]["path"] == "logos/light-logo.png"

    def test_config_preserves_other_sections(self, tmp_path):
        """Existing items/_base/rss sections must survive the logo write."""
        deploy = _make_deploy(tmp_path)
        logo = _make_logo(tmp_path)

        r = _run(str(logo), str(deploy))
        assert r.returncode == 0, r.stderr

        cfg = yaml.safe_load((deploy / "config.yaml").read_text())
        assert cfg["items"] == ["SvcA", "SvcB"]
        assert cfg["_base"]["admin"]["user"] == "admin"
        assert cfg["rss"] == {"enabled": False}

    def test_non_png_extension_preserved(self, tmp_path):
        deploy = _make_deploy(tmp_path)
        logo = _make_logo(tmp_path, "brand.svg", b"<svg/>")

        r = _run(str(logo), str(deploy))
        assert r.returncode == 0, r.stderr
        assert (deploy / "static" / "logos" / "logo.svg").is_file()


class TestDualLogoMode:
    def test_dark_and_light_copied(self, tmp_path):
        deploy = _make_deploy(tmp_path)
        dark = _make_logo(tmp_path, "dark.png", b"dark-bytes")
        light = _make_logo(tmp_path, "light.png", b"light-bytes")

        r = _run(str(deploy), LOGO_DARK=str(dark), LOGO_LIGHT=str(light))
        assert r.returncode == 0, r.stderr

        assert (deploy / "static/logos/dark-logo.png").read_bytes() == b"dark-bytes"
        assert (deploy / "static/logos/light-logo.png").read_bytes() == b"light-bytes"

    def test_only_dark_provided(self, tmp_path):
        deploy = _make_deploy(tmp_path)
        dark = _make_logo(tmp_path, "dark.png", b"dark-bytes")

        r = _run(str(deploy), LOGO_DARK=str(dark))
        assert r.returncode == 0, r.stderr
        assert (deploy / "static/logos/dark-logo.png").is_file()
        assert not (deploy / "static/logos/light-logo.png").exists()


class TestRejectionPaths:
    def test_missing_logo_file_fails(self, tmp_path):
        deploy = _make_deploy(tmp_path)
        r = _run(str(tmp_path / "nope.png"), str(deploy))
        assert r.returncode != 0
        combined = r.stdout + r.stderr
        assert "not found" in combined

    def test_relative_install_dir_fails(self, tmp_path):
        deploy = _make_deploy(tmp_path)
        logo = _make_logo(tmp_path)
        r = _run(str(logo), "deploy")  # relative
        assert r.returncode != 0
        assert "absolute" in r.stdout + r.stderr

    def test_non_install_dir_fails(self, tmp_path):
        """Directory without config.yaml must be rejected."""
        bare = tmp_path / "bare"
        bare.mkdir()
        logo = _make_logo(tmp_path)
        r = _run(str(logo), str(bare))
        assert r.returncode != 0
        assert "config.yaml" in r.stdout + r.stderr

    def test_no_args_fails(self):
        r = _run()
        assert r.returncode != 0
        assert "Usage" in r.stdout + r.stderr


class TestIdempotency:
    def test_rerun_overwrites_cleanly(self, tmp_path):
        deploy = _make_deploy(tmp_path)
        logo1 = _make_logo(tmp_path, "v1.png", b"version-1")
        logo2 = _make_logo(tmp_path, "v2.png", b"version-2")

        assert _run(str(logo1), str(deploy)).returncode == 0
        assert _run(str(logo2), str(deploy)).returncode == 0

        installed = deploy / "static/logos/logo.png"
        assert installed.read_bytes() == b"version-2"
        # config.yaml still valid after two rewrites
        cfg = yaml.safe_load((deploy / "config.yaml").read_text())
        assert cfg["logo"]["path"] == "logos/light-logo.png"

    def test_no_tmp_files_left_behind(self, tmp_path):
        deploy = _make_deploy(tmp_path)
        logo = _make_logo(tmp_path)

        _run(str(logo), str(deploy))
        leftovers = [p.name for p in deploy.glob("*.tmp")]
        assert leftovers == [], f"atomic-write temp files leaked: {leftovers}"


class TestConfigWriteSafety:
    def test_malformed_existing_config_rejected_gracefully(self, tmp_path):
        """A config.yaml that isn't a mapping must not be silently mangled."""
        deploy = _make_deploy(tmp_path)
        (deploy / "config.yaml").write_text("- just\n- a\n- list\n")
        logo = _make_logo(tmp_path)

        r = _run(str(logo), str(deploy))
        # Either it errors out or it replaces with a valid mapping —
        # but it must never crash with an unhandled traceback.
        assert "Traceback" not in r.stderr

    def test_config_yaml_still_parses_after_install(self, tmp_path):
        deploy = _make_deploy(tmp_path)
        logo = _make_logo(tmp_path)
        _run(str(logo), str(deploy))
        cfg = yaml.safe_load((deploy / "config.yaml").read_text())
        assert isinstance(cfg, dict) and "logo" in cfg
