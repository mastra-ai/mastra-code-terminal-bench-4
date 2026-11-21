"""Compare a rebuilt bundle with the bundles we benchmarked.

Usage: python3 -I check_files.py <sol|opus> <app-dir or bundle.tar>

The tar hashes of a rebuild will almost certainly differ from ours (see the README), so this compares the
files that carry our changes instead. These are the @mastra/code-sdk dist files that the dist patches edit.
Their hashes below are the ones in the benchmarked tars. The @mastra/core files the Opus parity patch edits
are not listed: their chunk names and contents depend on where the pnpm store sits relative to the workspace.
"""
import hashlib
import sys
import tarfile
from pathlib import Path

TARS = {
    "sol": "008bf7c7728e03295b622b36e429f58bdaa5263ca699514f0cd4a93e16966ef5",
    "opus": "f40f8217db0250189646e272a8843ab5e9a4309d015eca5e448d3530b89d694b",
}
COMMON = {
    "agents/prompts/build.js": "28fa7664e66c956984e13e0dc5f1dc37bd9a23b381248bda39607a023cf3ecc0",
    "agents/model.js": "b3f6fb78810183b6d483c12f94ddfecc6c4b03b7fe58774d1689e583146582af",
    "headless/run-mc.js": "e421feca3ece17ae5fb406081cf11d569a38054ce92fb65dc8ef73bc2570f452",
    "headless/cli.js": "554db8f473deef072d1c23ceae63cc07bd4b342698bdaff236d567b5ee8b9200",
}
FILES = {
    "sol": COMMON,
    "opus": {**COMMON, "index.js": "5656f8ca525af1e7e007ec4f2fa5191706961ab688b96ffaa0b153a86b20913a"},
}
SDK = "/node_modules/@mastra/code-sdk/dist/"


def from_dir(app: Path):
    dist = (app / SDK.strip("/")).resolve()
    return {rel: (dist / rel).read_bytes() for rel in FILES[variant]}


def from_tar(path: Path):
    # In the tar, node_modules/@mastra/code-sdk is a symlink into node_modules/.pnpm; match the real path.
    found = {}
    with tarfile.open(path) as tar:
        for member in tar:
            if not member.isfile() or "/.pnpm/@mastra+code-sdk@" not in member.name:
                continue
            for rel in FILES[variant]:
                if member.name.endswith(SDK + rel):
                    found[rel] = tar.extractfile(member).read()
    return found


variant, target = sys.argv[1], Path(sys.argv[2])
if target.is_file():
    with target.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    same = "matches" if digest == TARS[variant] else "differs from"
    print(f"tar sha256 {digest} {same} the benchmarked {variant} bundle")
    contents = from_tar(target)
else:
    contents = from_dir(target)

bad = 0
for rel, expected in FILES[variant].items():
    data = contents.get(rel)
    actual = hashlib.sha256(data).hexdigest() if data is not None else "missing"
    ok = actual == expected
    bad += not ok
    print(f"{'ok  ' if ok else 'DIFF'} code-sdk/dist/{rel} {actual}")
sys.exit(1 if bad else 0)
