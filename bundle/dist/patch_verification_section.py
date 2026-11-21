"""Replace the "## Verification is Required" section of Mastra Code's build-mode prompt in a deployed bundle.

Usage: python3 -I patch_verification_section.py <app-dir> <verification-section.md>

<app-dir> is the directory `pnpm deploy` wrote (the one holding package.json and node_modules/).

Edits exactly one file: @mastra/code-sdk/dist/agents/prompts/build.js (the `buildModePrompt` template literal,
source mastracode/sdk/src/agents/prompts/build.ts). The old section, from its heading up to but not including
"## Error Recovery", must match exactly once. The replacement is the section file verbatim (trailing newline
stripped), followed by the same blank line the neighbouring sections use. build.d.ts and build.js.map are left
alone: they hold types and a source map and are not loaded at runtime.
"""
import sys
from pathlib import Path

app = Path(sys.argv[1])
section = Path(sys.argv[2]).read_text()
build = app / "node_modules/@mastra/code-sdk/dist/agents/prompts/build.js"

# Old section as it appears inside the template literal in dist JS (backticks escaped, as in the source).
OLD = """## Verification is Required

Before considering any task complete:
- Run relevant tests (check package.json for test scripts)
- For TypeScript, run \\`tsc --noEmit\\` to catch type errors
- If there are no automated tests, manually verify the behavior works as expected
- Confirm the result makes sense in the codebase and improves the requested behavior
- Use available task-checking tools to ensure all tracked tasks are done

**Don't mark something as done until you've verified and proved it actually works.**

## Error Recovery
"""

if not section.startswith("## Verification is Required\n"):
    raise SystemExit("the section file does not start with the expected heading")
body = section.rstrip("\n")
# Template-literal escaping: backslash first, then backtick and ${.
esc = body.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
if esc != body:
    print(f"note: section needed template-literal escaping ({len(esc) - len(body)} chars added)")
NEW = esc + "\n\n## Error Recovery\n"

s = build.read_text()
n = s.count(OLD)
if n != 1:
    raise SystemExit(f"{build}: expected exactly 1 match of the old section, found {n}")
if s.count("## Verification is Required") != 1:
    raise SystemExit("heading count in build.js is not 1")
s2 = s.replace(OLD, NEW)
assert s2.count(NEW) == 1 and OLD not in s2
build.write_text(s2)
print(f"patched {build.relative_to(app)}: {len(s)} -> {len(s2)} bytes")
