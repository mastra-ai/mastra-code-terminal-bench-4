#!/usr/bin/env python3
"""Goal mode for headless runs: turn on Mastra Code's existing goal judge. Applied to both bundles.

Usage: python3 -I patch_goal_judge.py <app-dir>

<app-dir> is the directory `pnpm deploy` wrote (the one holding package.json and node_modules/).

Mastra Code already has a goal judge (the TUI's /goal), but headless runs never set an objective, so it never ran.
With this edit the task prompt becomes the goal objective, the run's --model is the judge, and the judge may
send the agent back at most twice (maxRuns 3). The prompt is still sent as the normal user message, so the
first request is unchanged apart from the objective block core adds. Opt out with
MASTRACODE_HEADLESS_GOAL_JUDGE=0. A settings `models.goalJudgeModel` still wins.

This edits the built dist in place (code-sdk agents/model.js, headless/run-mc.js, headless/cli.js).
../patches/goal-judge-source.patch is the same change against the TypeScript source, with tests.
Every old string must occur exactly once in its file, or the script exits before writing anything.
Running it on an already patched tree exits non-zero.
"""
import hashlib, pathlib, sys

APP = pathlib.Path(sys.argv[1])
SDK = APP / "node_modules/@mastra/code-sdk/dist"

EDITS = {
    "agents/model.js": [
        (
            "function getGoalJudgeModel({ requestContext }, settingsPath) {\n"
            "\tconst judgeModelId = loadSettings(settingsPath).models.goalJudgeModel;\n",
            "/**\n"
            "* Goal judge model for headless runs when settings name none. Set by the headless\n"
            "* CLI to the run's model so a headless goal judge needs no extra configuration.\n"
            "*/\n"
            "let headlessGoalJudgeModelId;\n"
            "function setHeadlessGoalJudgeModel(modelId) {\n"
            "\theadlessGoalJudgeModelId = modelId || void 0;\n"
            "}\n"
            "function getGoalJudgeModel({ requestContext }, settingsPath) {\n"
            "\tconst judgeModelId = loadSettings(settingsPath).models.goalJudgeModel ?? headlessGoalJudgeModelId;\n",
        ),
        (
            "resolveTenantFromRequestContext, setCredentialStoreProvider, setCustomProvidersSource };",
            "resolveTenantFromRequestContext, setCredentialStoreProvider, setCustomProvidersSource, setHeadlessGoalJudgeModel };",
        ),
    ],
    "headless/run-mc.js": [
        (
            "\t\t\t} else {\n"
            "\t\t\t\tif (!options.prompt) return fail(\"A prompt or goal is required.\");\n"
            "\t\t\t\tawait session.sendMessage({ content: options.prompt });\n",
            "\t\t\t} else {\n"
            "\t\t\t\tif (!options.prompt) return fail(\"A prompt or goal is required.\");\n"
            "\t\t\t\tif (options.judge) try {\n"
            "\t\t\t\t\tconst judgeGoalManager = new GoalManager();\n"
            "\t\t\t\t\tconst judgeState = {\n"
            "\t\t\t\t\t\tcontroller,\n"
            "\t\t\t\t\t\tsession,\n"
            "\t\t\t\t\t\tgoalManager: judgeGoalManager,\n"
            "\t\t\t\t\t\tpendingNewThread: false\n"
            "\t\t\t\t\t};\n"
            "\t\t\t\t\tif (await judgeGoalManager.setGoal(judgeState, options.prompt, void 0, options.judge.maxRuns)) await judgeGoalManager.saveToThread(judgeState);\n"
            "\t\t\t\t} catch {}\n"
            "\t\t\t\tawait session.sendMessage({ content: options.prompt });\n",
        ),
    ],
    "headless/cli.js": [
        (
            "import { createMastraCode } from \"../index.js\";\n",
            "import { createMastraCode } from \"../index.js\";\n"
            "import { setHeadlessGoalJudgeModel } from \"../agents/model.js\";\n",
        ),
        (
            "const STOP_NOTIFICATION_DISPATCH_TIMEOUT_MS = 2e3;\n",
            "const STOP_NOTIFICATION_DISPATCH_TIMEOUT_MS = 2e3;\n"
            "/** Judge evaluations per headless run: the judge can send the agent back at most twice. */\n"
            "const HEADLESS_GOAL_JUDGE_MAX_RUNS = 3;\n",
        ),
        (
            "\t\tconst effectiveDefaults = resolveModelDefaults(settings, listBuiltinModePacks());\n",
            "\t\tconst effectiveDefaults = resolveModelDefaults(settings, listBuiltinModePacks());\n"
            "\t\tconst headlessJudgeModelId = process.env.MASTRACODE_HEADLESS_GOAL_JUDGE === \"0\" ? void 0 : settings.models.goalJudgeModel ?? args.model;\n"
            "\t\tif (headlessJudgeModelId && !settings.models.goalJudgeModel) setHeadlessGoalJudgeModel(headlessJudgeModelId);\n",
        ),
        (
            "\t\t\tmaxTurns: args.maxTurns,\n"
            "\t\t\tpolicy: args.permissionMode ? permissionModeToPolicy(args.permissionMode) : void 0\n",
            "\t\t\tmaxTurns: args.maxTurns,\n"
            "\t\t\t...headlessJudgeModelId ? { judge: { maxRuns: HEADLESS_GOAL_JUDGE_MAX_RUNS } } : {},\n"
            "\t\t\tpolicy: args.permissionMode ? permissionModeToPolicy(args.permissionMode) : void 0\n",
        ),
    ],
}


def main():
    plan = {}
    for rel, edits in EDITS.items():
        p = SDK / rel
        text = p.read_text()
        for old, new in edits:
            if new in text:
                sys.exit(f"{rel}: already patched")
            c = text.count(old)
            if c != 1:
                sys.exit(f"{rel}: anchor found {c} times, expected 1:\n{old[:120]}")
            text = text.replace(old, new, 1)
        plan[rel] = (p, text)
    for rel, (p, text) in plan.items():
        before = hashlib.sha256(p.read_bytes()).hexdigest()
        st = p.stat()
        p.write_text(text)
        p.chmod(st.st_mode & 0o7777)
        after = hashlib.sha256(p.read_bytes()).hexdigest()
        print(f"{rel} {before} -> {after}")


main()
