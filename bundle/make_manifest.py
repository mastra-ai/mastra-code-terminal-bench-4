"""Write the manifest the Harbor adapter reads next to a bundle tar.

Usage: python3 -I make_manifest.py <bundle.tar> <app-dir> <label> [patch ...]

<app-dir> is the deployed package directory the tar was made from. The manifest lands at <bundle>.json. The
adapter checks its "sha256" against the tar and its bundle_sha256 argument, and pins "version". The rest is
recorded in each trial's provenance.json.
"""
import hashlib
import json
import sys
from pathlib import Path

BASE_REPO = "mastra-ai/mastra"
BASE_SHA = "f611832d661439f6b2aada795dcc16efdc2b12c3"

tar, app, label, patches = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4:]

with tar.open("rb") as stream:
    digest = hashlib.file_digest(stream, "sha256").hexdigest()

package = json.loads((app / "package.json").read_text())
packages = {package["name"]: package["version"]}
for path in sorted((app / "node_modules/@mastra").iterdir()):
    meta = path / "package.json"
    if meta.exists():
        data = json.loads(meta.read_text())
        packages[data["name"]] = data["version"]

manifest = {
    "name": package["name"],
    "version": package["version"],
    "sha256": digest,
    "label": label,
    "source": {
        "repo": BASE_REPO,
        "sha": BASE_SHA,
        "patches": [Path(p).name for p in patches],
        "note": "The version string is the workspace's, not an npm release. Identify the build by sha256.",
    },
    "build": {
        "command": "pnpm install --frozen-lockfile; pnpm run build:mastracode; "
                   "pnpm --filter mastracode --config.inject-workspace-packages=true deploy --prod app",
        "tar": 'tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="2026-11-04 00:00:00" -cf <tar> app',
    },
    "packages": packages,
    "published_to_npm": False,
}
out = tar.with_suffix(".json")
out.write_text(json.dumps(manifest, indent=2) + "\n")
print(f"wrote {out} ({digest})")
