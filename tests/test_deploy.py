"""The deployment files, which no other test touches.

These exist because of a real failure. `deploy/stockroom.env.example` shipped
with an unquoted value:

    STOCKROOM_ORG=Carlson Center for Imaging Science — RIT

which is valid to systemd's EnvironmentFile= parser (it takes everything after
the first `=` literally) and a syntax error to bash, which reads `Center` as a
command. `setup-pi.sh` sourced the file, so a fresh install died with
"Center: command not found" part-way through -- after creating the service
account and the data directory, before configuring TLS, nginx or systemd.

Nothing in the Python test suite could have caught that, because none of it is
Python. These checks are cheap and they cover the seam where the two parsers
disagree.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_DEPLOY = _ROOT / "deploy"
_ENV_EXAMPLE = _DEPLOY / "stockroom.env.example"
_SHELL_SCRIPTS = sorted(_DEPLOY.glob("*.sh"))

# `KEY=value`, live or commented out, ignoring the `# --- section ---` rules.
_ASSIGNMENT = re.compile(r"^(#\s*)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _assignments(text: str) -> list[tuple[str, str, bool]]:
    """Every assignment in an env file: (key, raw value, is_commented)."""
    found = []
    for line in text.splitlines():
        if line.lstrip().startswith("# ---"):
            continue
        match = _ASSIGNMENT.match(line.strip())
        if match:
            found.append((match.group(2), match.group(3), bool(match.group(1))))
    return found


# ---------------------------------------------------------------------------
# the bug that broke a real install
# ---------------------------------------------------------------------------


def test_the_env_example_is_valid_shell():
    """The exact failure: bash must be able to read this file.

    systemd is more forgiving than bash here, so a file that works in
    production can still break the installer. Sourcing it in a subshell is the
    cheapest possible reproduction of what setup-pi.sh used to do.
    """
    # `set -e` matters: without it the source keeps going after a failed line
    # and the subshell still exits 0, so this test would pass while the real
    # installer died. Checking stderr as well, because a `command not found`
    # inside a sourced file is reported there whatever the exit status.
    result = subprocess.run(
        ["bash", "-c", f'set -eu; set -a; . "{_ENV_EXAMPLE}"; set +a'],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0 and not result.stderr.strip(), (
        "deploy/stockroom.env.example is not valid shell:\n"
        + (result.stderr.strip() or f"exit status {result.returncode}")
        + "\n\nQuote the value. systemd strips double quotes, so quoting is "
        "correct for both parsers."
    )


@pytest.mark.parametrize(
    "key,value",
    [(k, v) for k, v, _ in _assignments(_ENV_EXAMPLE.read_text())],
    ids=[k for k, _, _ in _assignments(_ENV_EXAMPLE.read_text())],
)
def test_every_value_is_quoted(key, value):
    """Including the commented-out ones -- an operator uncomments them."""
    assert value.startswith('"') and value.endswith('"'), (
        f"{key} is unquoted. An operator editing this to something containing "
        "a space would produce a file bash cannot read."
    )


def test_the_org_name_survives_both_parsers_intact():
    """Quoting must not leave literal quotes in the value.

    systemd strips one layer; if it did not, the public page heading would
    render with quote marks around it.
    """
    result = subprocess.run(
        ["bash", "-c", f'set -a; . "{_ENV_EXAMPLE}"; set +a; printf "%s" "$STOCKROOM_ORG"'],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    assert not result.stdout.startswith('"')
    assert "Carlson" in result.stdout


# ---------------------------------------------------------------------------
# the installer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", _SHELL_SCRIPTS, ids=lambda p: p.name)
def test_shell_scripts_parse(script):
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_setup_does_not_source_the_env_file():
    """Sourcing a file in /etc as root runs whatever is in it.

    setup-pi.sh parses instead, which is both safer and more forgiving than
    bash about the syntax systemd accepts.
    """
    body = (_DEPLOY / "setup-pi.sh").read_text()
    assert "load_env_file" in body, "the safe parser is gone"
    for pattern in (r'^\s*\.\s+"\$ENV_FILE"', r'^\s*source\s+"\$ENV_FILE"'):
        assert not re.search(pattern, body, re.M), (
            "setup-pi.sh sources the env file again; use load_env_file"
        )


def test_the_installer_parser_reads_what_systemd_would(tmp_path):
    """Prove the parser handles the shape that broke the install.

    An operator who writes an unquoted org name is doing something systemd
    accepts. The installer must not fall over on it, even though the file we
    ship is quoted.
    """
    body = (_DEPLOY / "setup-pi.sh").read_text()
    parser = re.search(r"^load_env_file\(\) \{.*?^\}", body, re.M | re.S)
    assert parser, "could not find load_env_file in setup-pi.sh"

    script = tmp_path / "parser.sh"
    script.write_text(parser.group(0))

    awkward = tmp_path / "awkward.env"
    awkward.write_text(
        "# a comment\n"
        "\n"
        "STOCKROOM_ORG=Carlson Center for Imaging Science — RIT\n"
        'STOCKROOM_DATA_DIR="/var/lib/stockroom"\n'
        "STOCKROOM_BACKUP_KEEP=30\n"
        "not an assignment\n"
    )
    result = subprocess.run(
        ["bash", "-c",
         f'. "{script}"; load_env_file "{awkward}"; '
         f'printf "%s|%s|%s" "$STOCKROOM_ORG" "$STOCKROOM_DATA_DIR" "$STOCKROOM_BACKUP_KEEP"'],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    org, data, keep = result.stdout.split("|")
    assert org == "Carlson Center for Imaging Science — RIT"
    assert data == "/var/lib/stockroom", "quotes should be stripped once"
    assert keep == "30"


def test_the_installer_parser_executes_nothing(tmp_path):
    """It reads a root-owned file in /etc; it must never run it."""
    body = (_DEPLOY / "setup-pi.sh").read_text()
    parser = re.search(r"^load_env_file\(\) \{.*?^\}", body, re.M | re.S).group(0)
    script = tmp_path / "parser.sh"
    script.write_text(parser)

    canary = tmp_path / "EXECUTED"
    hostile = tmp_path / "hostile.env"
    hostile.write_text(f"STOCKROOM_ORG=fine\n$(touch {canary})\n`touch {canary}`\n")

    subprocess.run(["bash", "-c", f'. "{script}"; load_env_file "{hostile}"'],
                   capture_output=True, text=True, timeout=30)
    assert not canary.exists(), "the env file was executed, not parsed"


# ---------------------------------------------------------------------------
# drift between the example and the code
# ---------------------------------------------------------------------------


def test_every_documented_variable_is_one_the_app_reads():
    """A typo here is invisible: the setting simply never takes effect."""
    config_source = (_ROOT / "src" / "stockroom" / "config.py").read_text()
    known = set(re.findall(r"STOCKROOM_[A-Z_]+", config_source))
    documented = {k for k, _, _ in _assignments(_ENV_EXAMPLE.read_text())}

    unknown = documented - known
    assert not unknown, (
        f"stockroom.env.example documents variables config.py never reads: "
        f"{sorted(unknown)}"
    )


def test_the_env_example_covers_the_settings_worth_setting():
    """Not every variable needs documenting, but the operational ones do."""
    documented = {k for k, _, _ in _assignments(_ENV_EXAMPLE.read_text())}
    for expected in (
        "STOCKROOM_DATA_DIR", "STOCKROOM_PUBLISH_DIR", "STOCKROOM_ORG",
        "STOCKROOM_BACKUP_COPY_DIR", "STOCKROOM_BACKUP_REMOTE",
    ):
        assert expected in documented, f"{expected} is not in the example file"
