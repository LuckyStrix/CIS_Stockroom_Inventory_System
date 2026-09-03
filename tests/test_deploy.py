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
import shutil
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
# the systemd units
#
# Nothing checked these at all, and they are the part of the system that runs
# unattended on a machine nobody watches.
# ---------------------------------------------------------------------------


def _exec_starts(unit: str) -> list[str]:
    body = (_DEPLOY / unit).read_text()
    return [
        line.split("=", 1)[1]
        for line in body.splitlines()
        if line.startswith("ExecStart=")
    ]


def test_the_nightly_job_always_reaches_the_health_check():
    """Type=oneshot stops at the first ExecStart that fails.

    `stockroom backup` exits non-zero when an off-box target fails -- a USB
    stick nobody plugged back in -- and that used to take `prune` and
    `doctor` down with it. doctor is the only thing on the Pi that ever looks
    at the audit chain, the database integrity or the age of the backups, so
    the one condition most likely to need diagnosing was the one that stopped
    the diagnosis running.
    """
    commands = _exec_starts("stockroom-backup.service")
    assert len(commands) == 3, "the nightly job's steps changed; re-read this test"

    backup, prune, doctor = commands
    assert backup.startswith("-"), "a failed backup still aborts the whole unit"
    assert prune.startswith("-"), "a failed prune still aborts the whole unit"
    assert doctor.endswith("stockroom doctor"), "doctor must run last"
    assert not doctor.startswith("-"), (
        "doctor's exit code is what marks the unit failed; leave it unprefixed"
    )


def test_the_installer_grants_the_backup_job_its_copy_directory():
    """ProtectSystem=strict makes everything outside ReadWritePaths read-only.

    STOCKROOM_BACKUP_COPY_DIR points outside /var/lib/stockroom by definition
    -- that is the entire point of it -- so without a drop-in the documented
    USB backup fails every night with EROFS while working by hand.
    """
    unit = (_DEPLOY / "stockroom-backup.service").read_text()
    assert "ProtectSystem=strict" in unit
    assert "ReadWritePaths=/var/lib/stockroom" in unit

    body = (_DEPLOY / "setup-pi.sh").read_text()
    assert "STOCKROOM_BACKUP_COPY_DIR" in body, (
        "the installer does not grant the configured copy directory; the "
        "documented off-box backup cannot work under systemd"
    )
    assert "stockroom-backup.service.d" in body, "no drop-in is written"
    assert re.search(r"ReadWritePaths=-\$\{STOCKROOM_BACKUP_COPY_DIR\}", body), (
        "the drop-in should tolerate the path being absent (the '-' prefix), "
        "so an unplugged stick is a backup failure and not a unit that will "
        "not start"
    )


def test_the_service_can_write_everything_it_is_configured_to_write():
    """Every directory the app writes to must be in ReadWritePaths."""
    unit = (_DEPLOY / "stockroom.service").read_text()
    assert "ReadWritePaths=/var/lib/stockroom" in unit
    example = (_DEPLOY / "stockroom.env.example").read_text()
    for key in ("STOCKROOM_DATA_DIR", "STOCKROOM_PUBLISH_DIR"):
        value = re.search(rf'^#?\s*{key}="([^"]*)"', example, re.M)
        if value and not value.group(1).startswith("/var/lib/stockroom"):
            raise AssertionError(
                f"{key} defaults outside ReadWritePaths: {value.group(1)}"
            )


def test_the_database_is_not_world_readable():
    """It holds every password hash and session token in the stockroom.

    systemd creates the state directory 0755 by default and Python creates
    the database 0644, so without both of these any local account on the Pi
    could read the lot. security.py reasons carefully about a stolen SD card
    and not at all about somebody with a shell.
    """
    unit = (_DEPLOY / "stockroom.service").read_text()
    assert "StateDirectoryMode=0751" in unit, (
        "0750 removes the traverse bit nginx needs; 0755 exposes the database"
    )
    assert "UMask=0027" in unit


def test_the_unit_and_the_installer_agree_on_the_data_directory_mode():
    """systemd re-applies StateDirectoryMode on every start.

    So the installer's `chmod` is not the last word: whatever the unit says
    wins from the next restart onwards. They drifted to 0751 and 0750, which
    cost www-data the traverse bit into publish/ and made the public page
    return 404 -- with the file present, and doctor reporting it present,
    because the service user owns the directory and could always see it.
    """
    unit = (_DEPLOY / "stockroom.service").read_text()
    installer = (_DEPLOY / "setup-pi.sh").read_text()

    unit_mode = re.search(r"^StateDirectoryMode=(\d+)$", unit, re.M)
    installer_mode = re.search(r'chmod (\d+) "\$DATA_DIR"$', installer, re.M)
    assert unit_mode and installer_mode, "one of the two stopped setting a mode"
    assert unit_mode.group(1) == installer_mode.group(1), (
        f"the unit sets {unit_mode.group(1)} and the installer "
        f"{installer_mode.group(1)}; systemd wins on the next restart"
    )


def test_the_installer_does_not_widen_the_data_directory():
    """nginx needs to traverse into publish/, not to read what is beside it.

    `chmod 0755 $DATA_DIR` was how www-data used to get through, and it let
    every local account list the directory holding the database. 0751 grants
    the traverse and nothing else.
    """
    body = (_DEPLOY / "setup-pi.sh").read_text()
    assert re.search(r'chmod 0751 "\$DATA_DIR"', body), \
        "the data directory must be traverse-only for others"
    assert not re.search(r'chmod 0755 "\$DATA_DIR"[\s"]', body), \
        "0755 on the data directory exposes stockroom.db to every local user"
    assert re.search(r'chmod 0755 "\$DATA_DIR/publish"', body), \
        "nginx still has to read the generated public page"
    assert re.search(r'chmod 0750 "\$DATA_DIR/backups"', body), \
        "a backup is a whole copy of the database"


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
    """A typo here is invisible: the setting simply never takes effect.

    The character class has to include digits. Without them this reads
    STOCKROOM_SSO_REJECT_SHA1 out of config.py as "..._SHA", so a variable
    that IS read looks undocumented -- the enumeration failing rather than the
    thing it enumerates, which is the trap `_walk_routes` fell into.
    """
    config_source = (_ROOT / "src" / "stockroom" / "config.py").read_text()
    known = set(re.findall(r"STOCKROOM_[A-Z0-9_]+", config_source))
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


# ---------------------------------------------------------------------------
# the installer's rsync
#
# A second real failure. setup-pi.sh copied the source with
#
#     rsync -a --delete --exclude 'publish' ...
#
# and an rsync pattern with no leading slash matches at EVERY level of the
# tree, not just the top. The intent was to skip the generated ./publish
# output directory; the effect was to also delete src/stockroom/publish/, the
# Python subpackage that renders it. The install then died with
#
#     ModuleNotFoundError: No module named 'stockroom.publish'
#
# The clean-clone check that ran before release did not catch it, because it
# cloned with git and installed directly -- it never went through the rsync.
# This does.
# ---------------------------------------------------------------------------


def _rsync_excludes() -> list[str]:
    """The exact --exclude arguments setup-pi.sh passes."""
    body = (_DEPLOY / "setup-pi.sh").read_text()
    block = re.search(r"^rsync -a --delete.*?\n\s*\"\$REPO_DIR/\"", body,
                      re.M | re.S)
    assert block, "could not find the rsync invocation in setup-pi.sh"
    return re.findall(r"--exclude '([^']+)'", block.group(0))


def _source_packages() -> set[str]:
    """Every importable package under src/, as dotted names."""
    src = _ROOT / "src"
    return {
        str(path.parent.relative_to(src)).replace("/", ".")
        for path in src.rglob("__init__.py")
        if "__pycache__" not in path.parts
    }


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_the_installer_rsync_keeps_every_python_package(tmp_path):
    """Run the real exclude list and check nothing importable is dropped."""
    destination = tmp_path / "opt"
    destination.mkdir()
    excludes = []
    for pattern in _rsync_excludes():
        excludes += ["--exclude", pattern]

    result = subprocess.run(
        ["rsync", "-a", "--delete", *excludes, f"{_ROOT}/", f"{destination}/"],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stderr

    expected = _source_packages()
    assert "stockroom.publish" in expected, "test is not looking at the right tree"

    survived = {
        str(path.parent.relative_to(destination / "src")).replace("/", ".")
        for path in (destination / "src").rglob("__init__.py")
        if "__pycache__" not in path.parts
    }
    missing = expected - survived
    assert not missing, (
        f"setup-pi.sh's rsync deletes these Python packages: {sorted(missing)}. "
        "An --exclude pattern without a leading slash matches at every level "
        "of the tree, not just the transfer root."
    )


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_the_installer_rsync_still_skips_the_generated_directories(tmp_path):
    """The excludes must keep doing their actual job.

    Anchoring them must not turn into deleting them: a database or a rendered
    public page copied into /opt would be stale from the moment it landed.
    """
    destination = tmp_path / "opt"
    destination.mkdir()
    (_ROOT / "data").mkdir(exist_ok=True)
    (_ROOT / "publish").mkdir(exist_ok=True)

    excludes = []
    for pattern in _rsync_excludes():
        excludes += ["--exclude", pattern]
    subprocess.run(
        ["rsync", "-a", "--delete", *excludes, f"{_ROOT}/", f"{destination}/"],
        capture_output=True, text=True, timeout=180, check=True,
    )

    for unwanted in ("data", "publish", ".git", ".venv", ".pytest_cache"):
        assert not (destination / unwanted).exists(), (
            f"{unwanted}/ was copied into the install directory"
        )
    assert not list(destination.rglob("__pycache__")), "__pycache__ was copied"


def test_top_level_excludes_are_anchored():
    """The rule, stated directly, so the reason survives a future edit."""
    unanchored = [
        pattern for pattern in _rsync_excludes()
        # __pycache__ is deliberately unanchored: it should go at every level.
        if pattern != "__pycache__" and not pattern.startswith("/")
    ]
    assert not unanchored, (
        f"these rsync excludes match at every level of the tree: {unanchored}. "
        "Anchor them with a leading slash unless they are genuinely meant to "
        "match everywhere."
    )


# ---------------------------------------------------------------------------
# the CLI reads the same settings the service does
# ---------------------------------------------------------------------------
#
# Another real failure. `stockroom user create --admin` -- the command
# setup-pi.sh prints as the required next step -- got no environment, fell back
# to <repo root>/data and died with
#
#     PermissionError: [Errno 13] Permission denied: '/opt/stockroom/data'
#
# config.py now parses /etc/stockroom.env itself, which means it is a second
# implementation of the grammar setup-pi.sh's load_env_file() implements in
# shell. These check it, and check the two agree.


@pytest.fixture
def clean_environ(monkeypatch):
    """os.environ, restored afterwards -- _load_env_file mutates it."""
    import os

    for key in [k for k in os.environ if k.startswith("STOCKROOM_")]:
        monkeypatch.delenv(key)
    return os.environ


def test_config_reads_the_env_file(tmp_path, clean_environ):
    from stockroom import config

    env = tmp_path / "stockroom.env"
    env.write_text('STOCKROOM_DATA_DIR="/var/lib/stockroom"\n')
    config._load_env_file(env)
    assert clean_environ["STOCKROOM_DATA_DIR"] == "/var/lib/stockroom"


def test_config_defaults_to_the_installed_env_file():
    """The path systemd uses. Changing it silently unfixes the bug above."""
    from stockroom import config

    assert str(config.ENV_FILE) == "/etc/stockroom.env"


def test_a_real_environment_variable_wins_over_the_file(tmp_path, clean_environ):
    """systemd and the test suite stay authoritative; the file only fills in."""
    from stockroom import config

    clean_environ["STOCKROOM_DATA_DIR"] = "/somewhere/else"
    env = tmp_path / "stockroom.env"
    env.write_text('STOCKROOM_DATA_DIR="/var/lib/stockroom"\n')
    config._load_env_file(env)
    assert clean_environ["STOCKROOM_DATA_DIR"] == "/somewhere/else"


def test_a_missing_env_file_is_not_an_error(tmp_path, clean_environ):
    """Development, and the CI that runs this suite, have no /etc file."""
    from stockroom import config

    config._load_env_file(tmp_path / "nope.env")  # must not raise


def test_config_executes_nothing_in_the_env_file(tmp_path, clean_environ):
    """It reads a root-owned file in /etc; it must never run it."""
    from stockroom import config

    canary = tmp_path / "EXECUTED"
    env = tmp_path / "hostile.env"
    env.write_text(f"STOCKROOM_ORG=fine\n$(touch {canary})\n`touch {canary}`\n")
    config._load_env_file(env)
    assert not canary.exists()
    assert clean_environ["STOCKROOM_ORG"] == "fine"


def test_both_parsers_read_the_env_file_the_same_way(tmp_path, clean_environ):
    """config.py in Python and setup-pi.sh in bash must not drift apart."""
    from stockroom import config

    awkward = tmp_path / "awkward.env"
    awkward.write_text(
        "# a comment\n"
        "\n"
        "STOCKROOM_ORG=Carlson Center for Imaging Science — RIT\n"
        'STOCKROOM_DATA_DIR="/var/lib/stockroom"\n'
        "STOCKROOM_PUBLISH_DIR='/var/lib/stockroom/publish'\n"
        "STOCKROOM_BACKUP_KEEP=30\n"
        "not an assignment\n"
    )
    keys = ["STOCKROOM_ORG", "STOCKROOM_DATA_DIR", "STOCKROOM_PUBLISH_DIR",
            "STOCKROOM_BACKUP_KEEP"]

    parser = re.search(r"^load_env_file\(\) \{.*?^\}",
                       (_DEPLOY / "setup-pi.sh").read_text(), re.M | re.S).group(0)
    script = tmp_path / "parser.sh"
    script.write_text(parser)
    args = " ".join(f'"${k}"' for k in keys)  # space: `|` here would be a pipe
    result = subprocess.run(
        ["bash", "-c",
         f'. "{script}"; load_env_file "{awkward}"; printf "%s|%s|%s|%s" {args}'],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr

    config._load_env_file(awkward)
    assert [clean_environ[k] for k in keys] == result.stdout.split("|")


def test_the_installer_tells_the_operator_to_run_the_cli_as_the_service_user():
    """Running it as root would write a root-owned WAL beside the database."""
    body = (_DEPLOY / "setup-pi.sh").read_text()
    for line in body.splitlines():
        if "/.venv/bin/stockroom" in line and "sudo" in line:
            assert "sudo -u" in line, f"runs the CLI as root: {line.strip()}"


def test_the_installer_installs_the_short_command():
    """README.md and docs/security.md say `stockroom doctor`, not the venv
    path. That is only true on the Pi because the installer puts a wrapper on
    the PATH."""
    body = (_DEPLOY / "setup-pi.sh").read_text()
    assert re.search(
        r"install -m 0755 .*stockroom-wrapper\.sh\"? /usr/local/bin/stockroom", body
    ), "setup-pi.sh no longer installs /usr/local/bin/stockroom"


def test_the_wrapper_runs_the_cli_as_the_service_user():
    """Same reason as the installer check: a root-owned WAL beside the
    database locks the service out of its own writes."""
    body = (_DEPLOY / "stockroom-wrapper.sh").read_text()
    for line in body.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if "/.venv/bin/stockroom" in line and "sudo" in line:
            assert "-u \"$SERVICE_USER\"" in line or "sudo -u" in line, \
                f"runs the CLI as the invoking user: {line.strip()}"
    assert 'exec sudo' in body, "the wrapper stopped dropping privileges"
    assert 'id -un' in body, (
        "the wrapper must skip sudo when it is already the service user -- "
        "a nologin --system account has no password to answer a prompt with"
    )


def test_the_wrapper_carries_the_apps_environment_across_sudo():
    """`STOCKROOM_ENV_FILE=... stockroom status` is documented in
    docs/operations.md. Plain sudo resets the environment, which would point
    that command at the production database without saying so."""
    body = (_DEPLOY / "stockroom-wrapper.sh").read_text()
    assert "--preserve-env=" in body, "sudo would drop every STOCKROOM_* override"
    assert "STOCKROOM_" in body


def test_the_installer_restarts_the_service():
    """`enable --now` does nothing to a unit that is already running, so an
    in-place upgrade served the old code from the new files."""
    body = (_DEPLOY / "setup-pi.sh").read_text()
    assert re.search(r"^systemctl restart stockroom\.service", body, re.M), (
        "setup-pi.sh never restarts the app; an upgrade would keep running "
        "the code it replaced"
    )
    assert not re.search(r"^systemctl enable --now stockroom\.service", body, re.M), (
        "`enable --now` is a no-op on a running unit -- use restart"
    )


# ---------------------------------------------------------------------------
# the nginx configuration
# ---------------------------------------------------------------------------

_NGINX = _DEPLOY / "nginx-stockroom.conf"

# No block in this file nests another, so a non-greedy match to the first `}`
# is an accurate reading of it -- and it fails loudly (by matching too little)
# if that ever stops being true. Anchored to the start of a line: the word
# "location" also appears in the comments, and an unanchored match started
# there and swallowed the server-level directives that follow.
_LOCATION = re.compile(r"^[ \t]*location\s+([^{\n]+?)\s*\{(.*?)\}", re.S | re.M)

_IDENTITY_HEADERS = ("X-Shib-Mail", "X-Shib-DisplayName", "X-Remote-User")


def _locations() -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in _LOCATION.finditer(_NGINX.read_text())]


def test_the_identity_headers_are_cleared_at_server_level():
    """nginx forwards whatever the client sent unless told otherwise.

    docs/sso-integration.md: trusting X-Shib-* is impersonation-as-a-service,
    so nginx overwrites them with empty values -- which stops them being
    forwarded at all. They live outside any location so that every proxying
    location gets them.
    """
    outside = _LOCATION.sub("", _NGINX.read_text())
    for header in _IDENTITY_HEADERS:
        assert f'proxy_set_header {header} "";' in outside, (
            f"{header} is not cleared at server level; a client could send it"
        )


def test_the_application_never_reads_an_identity_header():
    """The other half of the two tests above, enforced in the source.

    nginx blanks X-Shib-* and X-Remote-User so a client cannot send them. That
    is worth something only while the application would not believe them
    anyway. Single sign-on is spoken in-process -- see stockroom/saml.py -- so
    there is no service provider in front of us setting those headers and no
    reason for any code here to look at one.

    This exists because the tempting shortcut, when SSO is being added, is
    exactly the one docs/sso-integration.md warns about: read X-Shib-Mail and
    trust it. On this deployment that is impersonation-as-a-service for
    anybody on the campus network.
    """
    offenders = []
    for path in sorted((_ROOT / "src" / "stockroom").rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if "X-Shib" in code or "X-Remote-User" in code:
                offenders.append(f"{path.name}:{number}")
    assert not offenders, (
        "these read an identity header the application must not trust: "
        f"{offenders}"
    )


def test_no_proxying_location_sets_headers_of_its_own():
    """proxy_set_header does not merge -- it replaces.

    A location that sets one header of its own inherits *none* of the server's,
    so adding `proxy_set_header X-Anything` to a location that proxies would
    silently start forwarding the client's X-Shib-* headers again. Either
    repeat the whole list there, or (better) do not set any.
    """
    for name, body in _locations():
        if "proxy_pass" not in body:
            continue
        assert "proxy_set_header" not in body, (
            f"location {name.strip()} sets its own headers, so it inherits "
            "none of the server-level ones -- including the X-Shib-* clearing"
        )


def test_the_public_page_falls_back_to_the_application():
    """A miss on /public/ must not be answered by nginx itself.

    nginx serves that path from disk, so `=404` was returned for two quite
    different faults -- the page has never been generated, or
    STOCKROOM_PUBLISH_DIR and this alias name different directories -- and
    said neither. The application serves the same directory and can tell them
    apart, so it gets the request instead.
    """
    blocks = [(n, b) for n, b in _locations() if n.strip() == "/public/"]
    assert len(blocks) == 2, "expected a /public/ location on both 80 and 443"
    for name, body in blocks:
        assert "try_files" in body
        assert "=404" not in body, (
            "a bare nginx 404 on the public page tells nobody which fault it is"
        )
        assert re.search(r"try_files\s[^;]*\s@\w+;", body), (
            "the last try_files argument must be a named location to fall "
            "back to, not a status code"
        )
