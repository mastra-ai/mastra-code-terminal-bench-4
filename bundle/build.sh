#!/usr/bin/env bash
# Build the two Mastra Code bundles used for the Terminal-Bench 4 results.
#
# Usage: bundle/build.sh <work-dir> [mastra-checkout]
#
# Writes to <work-dir>:
#   mastracode-tb4-fixes.tar  base: mastra f611832d + the 9 fix PRs (an intermediate, not run on its own)
#   mastracode-tb4-sol.tar    fixes + Verification section + goal mode           (GPT-6.1 Sol)
#   mastracode-tb4-opus.tar   fixes + Opus parity + Verification section + goal mode  (Claude Opus 5.5)
# and a .json manifest next to each tar for the Harbor adapter.
#
# If [mastra-checkout] is given it must be a clean git checkout of mastra-ai/mastra that contains f611832d.
# Otherwise the script clones https://github.com/mastra-ai/mastra into <work-dir>/mastra.
# Needs git, Node 22.19+ or 24.11+, pnpm 11.21.0, Python 3.11+, GNU tar. Makes no model or network calls
# beyond git and pnpm downloads.
set -euo pipefail

BASE_SHA=f611832d661439f6b2aada795dcc16efdc2b12c3
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mkdir -p "$1" && cd "$1" && pwd)"
SRC="${2:-$WORK/mastra}"
TAR_FLAGS=(--sort=name --owner=0 --group=0 --numeric-owner --mtime="2026-11-04 00:00:00")
FIX_PATCHES=("$HERE"/patches/0[1-9]-pr*.patch)
umask 0002

if [ ! -d "$SRC/.git" ]; then
  git clone https://github.com/mastra-ai/mastra.git "$SRC"
fi
cd "$SRC"
if [ -n "$(git status --porcelain)" ]; then
  echo "$SRC has local changes; use a clean checkout" >&2
  exit 1
fi
git -c advice.detachedHead=false checkout "$BASE_SHA"

# 1. The 9 fix PRs, in order. git apply refuses a patch whose context does not match.
for patch in "${FIX_PATCHES[@]}"; do
  echo "applying $(basename "$patch")"
  git apply "$patch"
done

# 2. Build and deploy a self-contained mastracode package directory.
pnpm install --frozen-lockfile
pnpm run build:mastracode
rm -rf "$WORK/fixes"
mkdir -p "$WORK/fixes"
pnpm --filter mastracode --config.inject-workspace-packages=true deploy --prod "$WORK/fixes/app"
tar "${TAR_FLAGS[@]}" -C "$WORK/fixes" -cf "$WORK/mastracode-tb4-fixes.tar" app
python3 -I "$HERE/make_manifest.py" "$WORK/mastracode-tb4-fixes.tar" "$WORK/fixes/app" tb4-fixes "${FIX_PATCHES[@]}"

# 3. Edits to the built dist. Each script checks its anchors and exits before writing if one does not match.
variant() {
  local name=$1; shift
  rm -rf "${WORK:?}/$name"
  mkdir -p "$WORK/$name"
  tar -xpf "$WORK/mastracode-tb4-fixes.tar" -C "$WORK/$name"
  local applied=()
  for step in "$@"; do
    case $step in
      parity) python3 -I "$HERE/dist/patch_opus_parity.py" "$WORK/$name/app"
              applied+=("$HERE/dist/patch_opus_parity.py") ;;
      verify) python3 -I "$HERE/dist/patch_verification_section.py" "$WORK/$name/app" "$HERE/dist/verification-section.md"
              applied+=("$HERE/dist/patch_verification_section.py") ;;
      judge)  python3 -I "$HERE/dist/patch_goal_judge.py" "$WORK/$name/app"
              applied+=("$HERE/dist/patch_goal_judge.py") ;;
    esac
  done
  tar "${TAR_FLAGS[@]}" -C "$WORK/$name" -cf "$WORK/mastracode-tb4-$name.tar" app
  python3 -I "$HERE/make_manifest.py" "$WORK/mastracode-tb4-$name.tar" "$WORK/$name/app" "tb4-$name" \
    "${FIX_PATCHES[@]}" "${applied[@]}"
}
variant sol verify judge
variant opus parity verify judge

# 4. Compare with the bundles we ran. See bundle/expected-sha256.txt for why the tar hashes can differ.
check() {
  local tar=$1 expected=$2 actual
  actual=$(sha256sum "$tar" | cut -c1-64)
  if [ "$actual" = "$expected" ]; then
    echo "$(basename "$tar"): $actual (matches the benchmarked bundle)"
  else
    echo "$(basename "$tar"): $actual (benchmarked bundle: $expected)"
  fi
}
check "$WORK/mastracode-tb4-sol.tar" 008bf7c7728e03295b622b36e429f58bdaa5263ca699514f0cd4a93e16966ef5
check "$WORK/mastracode-tb4-opus.tar" f40f8217db0250189646e272a8843ab5e9a4309d015eca5e448d3530b89d694b
python3 -I "$HERE/check_files.py" sol "$WORK/sol/app" || true
python3 -I "$HERE/check_files.py" opus "$WORK/opus/app" || true
