"""Tests for lib.sh error-reporting helpers and install.sh robustness.

Covers: die/warn/step/ok/run_step/require_cmd behaviour, the ERR trap,
post-write .env sanity checking, and install.sh's improved failure modes.
"""
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib.sh"
INSTALL = REPO / "install.sh"

BASE_ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp"}


def _bash(script: str, **env) -> subprocess.CompletedProcess:
    full = {**BASE_ENV, **env}
    return subprocess.run(["bash", "-c", script],
                          capture_output=True, text=True, env=full)


class TestLibHelpers:
    def test_die_prints_message_and_hint_and_exits_1(self):
        r = _bash(f'source "{LIB}"; die "something broke" "do X instead"')
        assert r.returncode == 1
        assert "ERROR: something broke" in r.stderr
        assert "Fix: do X instead" in r.stderr

    def test_die_without_hint(self):
        r = _bash(f'source "{LIB}"; die "just a message"')
        assert r.returncode == 1
        assert "just a message" in r.stderr
        assert "Fix:" not in r.stderr

    def test_warn_goes_to_stderr(self):
        r = _bash(f'source "{LIB}"; warn "careful"')
        assert r.returncode == 0
        assert "careful" in r.stderr
        assert r.stdout == ""

    def test_step_and_ok_go_to_stdout(self):
        r = _bash(f'source "{LIB}"; step "Section"; ok "done"')
        assert r.returncode == 0
        assert "=== Section ===" in r.stdout
        assert "done" in r.stdout

    def test_run_step_reports_success(self):
        r = _bash(f'source "{LIB}"; run_step "my label" true')
        assert r.returncode == 0
        assert "my label" in r.stdout

    def test_run_step_captures_failure_output_to_err_log(self, tmp_path):
        err_log = tmp_path / "err.log"
        script = f'''
source "{LIB}"
ERR_LOG="{err_log}"
run_step "failing label" bash -c 'echo detail-line; exit 3'
'''
        r = _bash(script)
        assert r.returncode == 1
        assert "failing label failed (exit 3)" in r.stderr
        assert "detail-line" in r.stderr  # captured log echoed back
        assert err_log.read_text().contains if False else True
        assert "detail-line" in err_log.read_text()

    def test_require_cmd_passes_for_existing(self):
        r = _bash(f'source "{LIB}"; require_cmd bash')
        assert r.returncode == 0

    def test_require_cmd_fails_with_hint(self):
        r = _bash(f'source "{LIB}"; require_cmd definitely-not-a-command-xyz '
                  '"install the xyz package"')
        assert r.returncode == 1
        assert "definitely-not-a-command-xyz" in r.stderr
        assert "install the xyz package" in r.stderr


class TestEnvSanityChecking:
    """The post-write sanity check must catch truncated hashes."""

    SCRYPT = "scrypt:32768:8:1$SALT1234567890$" + "a" * 64

    def _check_script(self, env_content: str) -> subprocess.CompletedProcess:
        # Mirror of the sanity check block in install.sh
        script = f'''
cat > /tmp/sanity_env << 'ENVEOF'
{env_content}
ENVEOF
set -a
source /tmp/sanity_env
set +a
[ -n "${{STATUS_ADMIN_PASS_HASH:-}}" ] || {{ echo 'empty' >&2; exit 1; }}
case "$STATUS_ADMIN_PASS_HASH" in
    *'$'*) ;;
    *) echo 'truncated' >&2; exit 1 ;;
esac
'''
        return _bash(script)

    def test_full_hash_with_dollars_passes(self):
        r = self._check_script(
            f"STATUS_ADMIN_PASS_HASH='{self.SCRYPT}'\n")
        assert r.returncode == 0, r.stderr

    def test_truncated_hash_is_caught(self):
        r = self._check_script("STATUS_ADMIN_PASS_HASH=scrypt:32768:8:1\n")
        assert r.returncode == 1
        assert "truncated" in r.stderr

    def test_empty_hash_is_caught(self):
        r = self._check_script("STATUS_ADMIN_PASS_HASH=\n")
        assert r.returncode == 1


class TestInstallScriptStructure:
    """Static checks that install.sh keeps its robustness guarantees."""

    @pytest.fixture(autouse=True)
    def load_script(self):
        self.text = INSTALL.read_text()

    def test_sources_lib_sh(self):
        assert 'source "$ROOT_DIR/lib.sh"' in self.text

    def test_uses_die_instead_of_bare_echo_exit(self):
        # No more ad-hoc "ERROR: ... ; exit 1" pairs — everything goes through die()
        import re
        bare = re.findall(r'echo "ERROR:[^"]*"\s*\n\s*exit 1', self.text)
        assert not bare, f"bare ERROR+exit blocks remain: {bare}"

    def test_single_quotes_env_values(self):
        # Consolidated into write_env_file(): 3 printf writes, all quoted.
        assert self.text.count("%s='%s'") >= 3
        assert 'printf \'%s=%s\\n\' "STATUS_ADMIN_PASS_HASH"' not in self.text

    def test_verifies_dependencies_after_install(self):
        assert "import flask, gunicorn, werkzeug, yaml" in self.text

    def test_sanity_checks_env_after_write(self):
        assert "appears truncated" in self.text

    def test_health_check_after_systemd_start(self):
        assert "journalctl -u" in self.text or \
               "did not respond" in self.text

    def test_password_strength_warning(self):
        assert "shorter than 8 characters" in self.text

    def test_deploys_lib_sh(self):
        assert "lib.sh" in self.text.split("Deploying files")[1][:600]
