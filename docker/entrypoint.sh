#!/bin/sh
# Container entrypoint, also usable for one-off commands as the right user:
#   manage                 run web + sync worker (default; migrates first)
#   manage <command> ...   python manage.py <command> ..., e.g. `manage sync --full`
# Starts as root only to fix ownership of the data volume, then drops to `app`.
set -eu

if [ "$(id -u)" = "0" ]; then
  [ "$(stat -c %u /data)" = "1000" ] || chown -R app:app /data
  exec setpriv --reuid=app --regid=app --init-groups "$0" "$@"
fi

mkdir -p "$HOME"
[ $# -gt 0 ] || set -- serve
exec python manage.py "$@"
