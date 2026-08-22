"""Tests for shell-safe .env writing: values containing $ must survive sourcing.

Root cause being tested: werkzeug scrypt hashes contain `$` separators
(`scrypt:n:r:p$SALT$HASH`). If install.sh / change_password.sh write the value
UNQUOTED, `start.sh` sources the file with bash and bash expands `$SALT` /
`$HASH` as (undefined) variables — silently truncating the hash at the first
`$`. The server then verifies logins against a 16-char garbage hash and every
login fails.

The fix: writers single-quote values; start.sh sources as before.
"""
import subprocess
from pathlib import Path

SCRYPT_LIKE = "scrypt:32768:8:1$EmsPhEW2VImVtnUx$d7e039115c8700843581a4e"


def _source_and_capture(env_file: Path, var: str) -> tuple[int, str]:
    """Source env_file in bash, return (len, value) of var."""
    cmd = (
        "set -a; source '%s'; set +a; printf '%%s' \"${%s}\"" % (env_file, var)
    )
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    return len(r.stdout), r.stdout


class TestShellSafeEnvWriting:
    def test_unquoted_value_is_truncated_by_source(self, tmp_path):
        """Documents the bug: unquoted $ in .env gets expanded on source."""
        env = tmp_path / ".env"
        env.write_text(f"STATUS_ADMIN_PASS_HASH={SCRYPT_LIKE}\n")
        length, val = _source_and_capture(env, "STATUS_ADMIN_PASS_HASH")
        assert "$" not in val  # dollars eaten
        assert length < len(SCRYPT_LIKE)  # truncated at first $

    def test_single_quoted_value_survives_source(self, tmp_path):
        """The fix: writers must single-quote values containing $."""
        env = tmp_path / ".env"
        env.write_text("STATUS_ADMIN_PASS_HASH='%s'\n" % SCRYPT_LIKE)
        length, val = _source_and_capture(env, "STATUS_ADMIN_PASS_HASH")
        assert length == len(SCRYPT_LIKE)
        assert val == SCRYPT_LIKE
        assert "$" in val

    def test_install_sh_writes_quoted_values(self):
        """install.sh must quote every credential it writes to the env file."""
        script = (Path(__file__).resolve().parent.parent / "install.sh").read_text()
        checked = 0
        for line in script.splitlines():
            stripped = line.strip()
            if stripped.startswith('printf') and (
                "STATUS_ADMIN_PASS_HASH" in stripped or "STATUS_SECRET_KEY" in stripped
            ):
                checked += 1
                # Extract the format string (first quoted segment after printf)
                fmt = stripped.split('"')[1]
                assert fmt == "%s='%s'\\n", (
                    f"env value not single-quoted: {stripped}"
                )
        assert checked >= 3, f"expected >=3 credential writes, found {checked}"

    def test_change_password_py_writes_quoted_value(self):
        """change_password.sh's inline python must quote the hash it writes."""
        script = (
            Path(__file__).resolve().parent.parent / "change_password.sh"
        ).read_text()
        assert 'STATUS_ADMIN_PASS_HASH={new_hash}\\n"' not in script.replace(
            "f\"", "\""
        ) or "'{new_hash}'" in script, (
            "change_password.sh writes an unquoted hash — $ will be eaten by source"
        )
