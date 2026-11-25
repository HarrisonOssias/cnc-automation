#!/usr/bin/env bash
# Build a Pi (edge) release, run on a dev machine or in CI:
#   source/edge/build-release.sh [version]
# Writes releases/forge-<version>.tar.gz and releases/latest.json at the repo root. The backend serves
# both, and every Pi picks the new version up on its next update check.
#
# The tarball mirrors the repo layout, so on a Pi the release root (/opt/forge/current) holds
# pyproject.toml, VERSION and source/... just like a checkout.
set -euo pipefail
repo="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$repo"

version=${1:-$(date -u +%Y.%m.%d-%H%M%S)}
file="forge-$version.tar.gz"

(cd source/edge/kiosk && pnpm install --frozen-lockfile && pnpm build)

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/source/shared" "$stage/source/edge/kiosk" "$stage/source/cloud"
cp pyproject.toml "$stage/"
cp -R source/shared/forge_core "$stage/source/shared/"
cp -R source/edge/agent source/edge/pi "$stage/source/edge/"
cp -R source/edge/kiosk/dist "$stage/source/edge/kiosk/"
cp -R source/cloud/backend "$stage/source/cloud/"  # tiny; pyproject declares all three packages
echo "$version" > "$stage/VERSION"
find "$stage" -name __pycache__ -prune -exec rm -rf {} +

mkdir -p releases
mac_flags=()  # macOS (bsdtar) adds Apple metadata that GNU tar on the Pi warns about
tar --version | grep -q bsdtar && mac_flags=(--no-xattrs --no-mac-metadata)
COPYFILE_DISABLE=1 tar "${mac_flags[@]}" -czf "releases/$file" -C "$stage" .
sha256=$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "releases/$file")
printf '{"version": "%s", "file": "%s", "sha256": "%s"}\n' "$version" "$file" "$sha256" > releases/latest.json

echo "published $version -> releases/$file"
