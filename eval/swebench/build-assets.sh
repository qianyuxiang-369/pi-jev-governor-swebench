#!/usr/bin/env bash
# Build container-injection assets for the Pi SWE-bench runner (P4).
#
# Reproduces the asset layout that was validated in P2 (see memory_doc §5.5/§6.12):
#   node                       host node binary (glibc must match SWE images: Ubuntu 22.04)
#   pi/node_modules/@earendil-works/pi-coding-agent/{package.json,dist/bundle}
#   governor/{package.json,src,extensions}
#   inject.tar.gz              = node + pi + governor  -> extracted to /root/agent/
#   pi-deps.tar.gz             = nested node_modules of pi-coding-agent
#                                -> extracted into <pi-pkg>/ (jiti etc. are not bundled)
#
# Usage: bash build-assets.sh [output-dir]   (default: eval/swebench/container-assets)
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
PI_PKG="${ROOT}/node_modules/@earendil-works/pi-coding-agent"
PKG_REL="pi/node_modules/@earendil-works/pi-coding-agent"
OUT="${1:-${ROOT}/eval/swebench/container-assets}"
NODE_BIN="${NODE_BIN:-$(command -v node || true)}"

[ -d "${PI_PKG}/dist/bundle" ] || { echo "error: pi package not found at ${PI_PKG} (run npm install in repo root)" >&2; exit 1; }
[ -n "${NODE_BIN}" ] && [ -x "${NODE_BIN}" ] || { echo "error: node binary not found (set NODE_BIN=...)" >&2; exit 1; }

# JuiceFS reports spurious "file changed as we read it" on freshly copied
# large files (metadata lags a few seconds); --warning=no-file-changed only
# silences the message, the exit code stays 1. Retry once after a pause.
create_tarball() {
	local archive="$1"
	shift
	if tar -czf "${archive}" --warning=no-file-changed "$@"; then
		return 0
	fi
	echo "note: ${archive}: tar returned non-zero, retrying once after 5s" >&2
	sleep 5
	tar -czf "${archive}" --warning=no-file-changed "$@"
}

echo "== building assets =="
echo "  repo      : ${ROOT}"
echo "  pi package: ${PI_PKG}"
echo "  node      : ${NODE_BIN} ($(${NODE_BIN} --version))"
echo "  output    : ${OUT}"

rm -rf "${OUT}"
mkdir -p "${OUT}/${PKG_REL}/dist" "${OUT}/governor"

cp "${NODE_BIN}" "${OUT}/node"
cp "${PI_PKG}/package.json" "${OUT}/${PKG_REL}/"
cp -r "${PI_PKG}/dist/bundle" "${OUT}/${PKG_REL}/dist/"
cp "${ROOT}/package.json" "${OUT}/governor/"
cp -r "${ROOT}/src" "${OUT}/governor/"
cp -r "${ROOT}/extensions" "${OUT}/governor/"

cd "${OUT}"
sync  # flush JuiceFS write cache before archiving
create_tarball inject.tar.gz node pi governor

if [ -d "${PI_PKG}/node_modules" ]; then
	create_tarball pi-deps.tar.gz -C "${PI_PKG}" node_modules
else
	echo "warn: ${PI_PKG}/node_modules missing; pi-deps.tar.gz not created (extensions may fail to load)" >&2
fi

echo "== done =="
ls -la "${OUT}"
echo
echo "inject.tar.gz  contents:"
tar tzf inject.tar.gz | head -5 || true
echo "..."
echo "pi-deps.tar.gz contents:"
tar tzf pi-deps.tar.gz | head -3 || true
