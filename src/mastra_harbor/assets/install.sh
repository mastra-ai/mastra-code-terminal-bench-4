#!/usr/bin/env bash
set -euo pipefail
cd /opt/mastra-harbor
case "$(uname -m)" in
  x86_64) mc_arch=x64; mc_sha=9aa8e9d2298ab68c600bd6fb86a6c13bce11a4eca1ba9b39d79fa021755d7c37 ;;
  aarch64|arm64) mc_arch=arm64; mc_sha=1bf1eb9ee63ffc4e5d324c0b9b62cf4a289f44332dfef9607cea1a0d9596ba6f ;;
  *) echo 'Supported platforms: Linux glibc x86_64 or aarch64' >&2; exit 1 ;;
esac
mc_archive="node-v22.22.0-linux-${mc_arch}.tar.xz"
curl --fail --show-error --location --retry 3 "https://nodejs.org/dist/v22.22.0/${mc_archive}" -o "$mc_archive"
printf '%s  %s\n' "$mc_sha" "$mc_archive" | sha256sum --check -
mkdir -p node
tar -xJf "$mc_archive" -C node --strip-components=1
rm "$mc_archive"
export PATH="/opt/mastra-harbor/node/bin:$PATH"
node --version
if [ -n "${MC_BUNDLE_SHA256:-}" ]; then
  # Prebuilt source snapshot: a self-contained mastracode package directory.
  printf '%s  %s\n' "$MC_BUNDLE_SHA256" mastracode-bundle.tar | sha256sum --check -
  mkdir -p node_modules/mastracode
  tar -xf mastracode-bundle.tar --no-same-owner --strip-components=1 -C node_modules/mastracode
  rm mastracode-bundle.tar
else
  npm install --no-audit --no-fund --cache /opt/mastra-harbor/npm-cache mastracode@0.44.1
fi
# Uploaded files may have belonged to Harbor's agent user. Freeze the runtime.
chown -R root:root /opt/mastra-harbor
chmod -R a+rX /opt/mastra-harbor
node node_modules/mastracode/dist/cli.js --help
