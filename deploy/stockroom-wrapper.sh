#!/usr/bin/env bash
#
# Installed as /usr/local/bin/stockroom by deploy/setup-pi.sh.
#
# The CLI itself lives in the venv, at /opt/stockroom/.venv/bin/stockroom,
# which is on nobody's PATH -- so every doc either wrote the full path or told
# the reader to set up an alias, and README.md and docs/security.md just said
# `stockroom doctor` and were wrong on the actual machine. This is what makes
# the short form true there.
#
# It has to run as the service user. Running the CLI as root writes a
# root-owned WAL and -shm beside the database, and the service -- which is not
# root -- then fails every write with "attempt to write a readonly database"
# until someone chowns them back.

set -euo pipefail

REAL=/opt/stockroom/.venv/bin/stockroom
SERVICE_USER=stockroom

if [[ ! -x "$REAL" ]]; then
    echo "stockroom: $REAL is missing -- is the app installed?" >&2
    exit 1
fi

# The service account already: the backup timer and the health check land
# here. Do not go through sudo -- a --system account with a nologin shell has
# no password to answer a prompt with.
if [[ "$(id -un)" == "$SERVICE_USER" ]]; then
    exec "$REAL" "$@"
fi

# sudo resets the environment, which would silently break the documented
# one-off override -- `STOCKROOM_ENV_FILE=/tmp/other.env stockroom status`
# would quietly read /etc/stockroom.env instead of the file that was asked
# for, and operate on the production database. Carry the app's own variables
# across and nothing else; a real environment variable beating the file is
# the contract in docs/operations.md.
KEEP="$(env | sed -n 's/^\(STOCKROOM_[A-Za-z0-9_]*\)=.*/\1/p' | paste -sd, -)"

if [[ -n "$KEEP" ]]; then
    exec sudo --preserve-env="$KEEP" -u "$SERVICE_USER" "$REAL" "$@"
fi

exec sudo -u "$SERVICE_USER" "$REAL" "$@"
