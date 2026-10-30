#!/bin/sh
set -e

# Target data directory and app user credentials
DATA_DIR="${SAHA_DATA_DIR:-/data}"
APP_USER="${SAHA_APP_USER:-appuser}"
APP_UID=10001
APP_GID=10001

# Dynamic cloud PORT handling (Render, Railway set $PORT)
if [ -n "$PORT" ] && [ "$PORT" != "8080" ] && [ "$1" != "sh" ]; then
    set -- $(echo "$@" | sed "s/--port 8080/--port $PORT/")
fi

# If running as root (UID 0), fix permissions on data directory and drop privileges
if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR" "$DATA_DIR/workspace"
    chown -R "$APP_UID:$APP_GID" "$DATA_DIR"
    chmod 700 "$DATA_DIR"
    echo "[docker-entrypoint] Data directory $DATA_DIR initialized. Dropping privileges to $APP_USER (target UID $APP_UID)..."
    if command -v gosu >/dev/null 2>&1; then
        exec gosu "$APP_USER" sh -c 'echo "[docker-entrypoint] Dropped privileges; running as UID $(id -u)"; exec "$@"' dummy "$@"
    else
        exec su -s /bin/sh "$APP_USER" -c 'echo "[docker-entrypoint] Dropped privileges; running as UID $(id -u)"; exec "$@"' dummy "$@"
    fi
else
    # Running as non-root user (e.g. UID 10001)
    echo "[docker-entrypoint] Running as current user (UID $(id -u))"
    if [ ! -w "$DATA_DIR" ] 2>/dev/null; then
        if [ "$SAHA_REQUIRE_PERSISTENT" = "true" ] || [ "$SAHA_REQUIRE_PERSISTENT" = "1" ]; then
            echo "ERROR: Data directory '$DATA_DIR' is not writable by current user (UID $(id -u))." >&2
            echo "Mount permission error: ensure the host directory is writable by UID $APP_UID (e.g. chown -R $APP_UID:$APP_GID /path/to/data) or run as root so the entrypoint can fix ownership." >&2
            exit 1
        fi
    fi
    exec "$@"
fi
