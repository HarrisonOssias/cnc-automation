#!/usr/bin/env bash
# OTA update for the Pi agent + kiosk. Runs as root from forge-update.timer, or by hand:
#   sudo systemctl start forge-update
#
# 1. Ask the backend which release this machine should run.
# 2. Download it, check its sha256, install it side by side in /opt/forge/releases/<version>.
# 3. Switch the /opt/forge/current symlink and restart the agent.
# 4. If the agent doesn't report the new version as healthy, switch back and never retry that version.
set -euo pipefail

FORGE_HOME=${FORGE_HOME:-/opt/forge}
STATE_DIR=${STATE_DIR:-/var/lib/forge}
ENV_FILE=${ENV_FILE:-/etc/forge/agent.env}
UNIT_DIR=${UNIT_DIR:-/etc/systemd/system}
HEALTH_URL=${HEALTH_URL:-http://127.0.0.1:8080/healthz}
KEEP_RELEASES=3

set -a; source "$ENV_FILE"; set +a
log() { echo "forge-update: $*"; }

activate() {  # point "current" at a release (atomically), install its units, restart
  ln -sfn "releases/$1" "$FORGE_HOME/current.new"
  mv -Tf "$FORGE_HOME/current.new" "$FORGE_HOME/current"
  mkdir -p "$UNIT_DIR"
  cp "$FORGE_HOME/current/source/edge/pi/"*.service "$FORGE_HOME/current/source/edge/pi/"*.timer "$UNIT_DIR/"
  systemctl daemon-reload
  systemctl restart forge-agent.service
}

healthy() {  # the agent answers /healthz with the expected version within ~60 s
  for _ in $(seq 30); do
    curl -fsS --max-time 2 "$HEALTH_URL" 2>/dev/null | grep -q "\"version\":\"$1\"" && return 0
    sleep 2
  done
  return 1
}

mkdir -p "$STATE_DIR" "$FORGE_HOME/releases"
current=$(basename "$(readlink "$FORGE_HOME/current" 2>/dev/null || echo none)")

manifest=$(curl -fsS --max-time 20 -H "X-Machine-Token: $MACHINE_TOKEN" "$BACKEND_URL/machines/$MACHINE_ID/release")
read -r version url sha256 < <(python3 -c 'import json,sys; m=json.load(sys.stdin); print(m["version"], m["url"], m["sha256"])' <<<"$manifest")

if [[ "$version" == "$current" ]]; then log "up to date ($current)"; exit 0; fi
if grep -qxF "$version" "$STATE_DIR/bad-releases" 2>/dev/null; then log "skipping $version: it failed before"; exit 0; fi
log "installing $version (running: $current)"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsS --max-time 300 -o "$tmp/release.tar.gz" "$BACKEND_URL$url"
echo "$sha256  $tmp/release.tar.gz" | sha256sum -c --quiet -

dest="$FORGE_HOME/releases/$version"
rm -rf "$dest" && mkdir -p "$dest"
if ! { tar -xzf "$tmp/release.tar.gz" -C "$dest" \
      && python3 -m venv "$dest/venv" \
      && "$dest/venv/bin/pip" install --quiet --disable-pip-version-check "$dest[agent]"; }; then
  rm -rf "$dest"; log "install of $version failed; still on $current"; exit 1
fi

activate "$version"
if healthy "$version"; then
  systemctl restart forge-kiosk.service  # reload the screen so it picks up the new UI
  log "now running $version"
  # Keep the newest few releases for rollback; never delete the one in use.
  ls -1dt "$FORGE_HOME"/releases/*/ | tail -n +$((KEEP_RELEASES + 1)) | while read -r old; do
    [[ "$(basename "$old")" == "$version" ]] || rm -rf "$old"
  done
  exit 0
fi

log "$version is not healthy; rolling back to $current"
echo "$version" >> "$STATE_DIR/bad-releases"
if [[ "$current" != none ]]; then
  activate "$current"
  healthy "$current" && log "back on $current" || log "WARNING: $current is not healthy either"
  systemctl restart forge-kiosk.service
fi
exit 1
