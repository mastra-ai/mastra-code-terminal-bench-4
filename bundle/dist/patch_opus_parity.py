"""Claude Code refusal and length parity for Mastra Code + Claude Opus 5.5. Applied to the Opus bundle only.

Usage: python3 -I patch_opus_parity.py <app-dir>

<app-dir> is the directory `pnpm deploy` wrote (the one holding package.json and node_modules/).

The leaderboard's Claude Code runs handle two Opus 5.5 stop reasons in ways stock Mastra Code does not. This
script makes Mastra Code do the same, so that the comparison is about the agent and not about these two cases.
It edits the built dist in place and changes only these things:

1. @mastra/core agent-controller: request Anthropic's server-side fallback for claude-opus-5-5 -> claude-opus-5,
   through the same `providerOptions.anthropic.fallbacks` path Mastra already uses for claude-fable-5.
2. @mastra/core agent loop (and the durable agent copy): processor retry feedback that starts with a sentinel
   prefix is sent as a plain user message (signal type "user-message") instead of a <system-reminder>.
3. @mastra/code-sdk: a ClaudeCodeRefusalParity processor (input and output lanes) replaces CyberRefusalHandler
   in the output lane.
   - A refusal stop on claude-opus-5-5 sends Claude Code's verbatim user message and retries.
   - Once a server-side fallback is seen, the run switches to anthropic/claude-opus-5 for good (the fallbacks
     option is dropped).
   - A refusal after the fallback ends the run, as stock does.
   - A max_tokens stop sends Claude Code's verbatim resume message and retries, at most 3 times per run. The 4th
     length stop ends the run. MASTRA_CLAUDE_CODE_LENGTH_RESUME=0 turns the resume off.

Every anchor must match the expected number of times, or the script exits. The inserted code is kept exactly as
it was in the benchmarked bundle, comment included.
"""
import sys
from pathlib import Path

app = Path(sys.argv[1])
core = app / "node_modules/@mastra/core/dist"
sdk = app / "node_modules/@mastra/code-sdk/dist/index.js"
SENT = "\\u0000mastra-user-message\\u0000"


def sub(path, old, new, count=1):
    s = path.read_text()
    n = s.count(old)
    if n != count:
        raise SystemExit(f"{path.name}: expected {count} match(es), found {n}: {old[:80]!r}")
    path.write_text(s.replace(old, new))


# 1. fallback options for opus-5-5
ac = next(core.glob("agent-controller-*.js"))
sub(ac, '''function buildFableFallbackProviderOptions(modelId) {
	if (!/(^|\\/)claude-fable-5$/.test(modelId)) return;
	return { anthropic: { fallbacks: [{ model: FABLE_FALLBACK_MODEL }] } };
}''', '''function buildFableFallbackProviderOptions(modelId) {
	if (/(^|\\/)claude-opus-5-5$/.test(modelId)) return { anthropic: { fallbacks: [{ model: "claude-opus-5" }] } };
	if (!/(^|\\/)claude-fable-5$/.test(modelId)) return;
	return { anthropic: { fallbacks: [{ model: FABLE_FALLBACK_MODEL }] } };
}''')

# 2. retry feedback as a user message when marked
old_sig = '''messageList.addSignal(createSignal({
					type: "reactive",
					tagName: "system-reminder",
					contents: inputData.processorRetryFeedback
				}))'''
new_sig = '''messageList.addSignal(createSignal(inputData.processorRetryFeedback.startsWith("%s") ? {
					type: "user-message",
					contents: inputData.processorRetryFeedback.slice("%s".length)
				} : {
					type: "reactive",
					tagName: "system-reminder",
					contents: inputData.processorRetryFeedback
				}))''' % (SENT, SENT)
agent_js = next(p for p in sorted(core.glob("agent-*.js")) if "processorRetryFeedback" in p.read_text())
sub(agent_js, old_sig, new_sig)
durable = next(core.glob("create-durable-agent-*.js"))
sub(durable, '''messageList.addSignal(createSignal({
					type: "reactive",
					tagName: "system-reminder",
					contents: processorRetryFeedback
				}))''', '''messageList.addSignal(createSignal(processorRetryFeedback.startsWith("%s") ? {
					type: "user-message",
					contents: processorRetryFeedback.slice("%s".length)
				} : {
					type: "reactive",
					tagName: "system-reminder",
					contents: processorRetryFeedback
				}))''' % (SENT, SENT))

# 3. processor in code-sdk
sub(sdk, 'import { canUseProvider, createMastraCodeGateway, getDynamicModel, getGoalJudgeModel, resolveModel } from "./agents/model.js";',
    'import { canUseProvider, createMastraCodeGateway, getDynamicModel, getGoalJudgeModel, resolveModel, resolveRequestThinkingLevel } from "./agents/model.js";')
processor = r'''
//#region benchmark parity: Claude Code 2.1.284 refusal handling (TB4 Opus 5.5 run, approved deviation)
const CLAUDE_CODE_PARITY_SENTINEL = "%s";
const CLAUDE_CODE_REFUSAL_MESSAGE = "Your response above was stopped by a safety classifier — this is not a tool or API error. The rest of it was withheld, and tool calls in it that had not finished did not run. Do not produce that content again, even reworded.";
const CLAUDE_CODE_LENGTH_MESSAGE = "Output token limit hit. Resume directly — no apology, no recap of what you were doing. Pick up mid-thought if that is where the cut happened. Break remaining work into smaller pieces.";
const CLAUDE_CODE_PARITY_PRIMARY = /(^|\/)claude-opus-5-5$/;
const CLAUDE_CODE_LENGTH_RESUME_LIMIT = 3;
const CLAUDE_CODE_PARITY_FALLBACK = "anthropic/claude-opus-5";
function claudeCodeParityFallbackModel(providerMetadata) {
	const iterations = providerMetadata?.anthropic?.iterations;
	if (!Array.isArray(iterations)) return;
	const hop = [...iterations].reverse().find((iter) => iter && typeof iter === "object" && iter.type === "fallback_message");
	return hop ? (typeof hop.model === "string" && hop.model ? hop.model : "unknown") : void 0;
}
function claudeCodeParityIsRefusal(finishReason, providerMetadata) {
	if (finishReason !== "content-filter") return false;
	const details = providerMetadata?.anthropic?.stopDetails;
	return !details || typeof details !== "object" || details.type === void 0 || details.type === "refusal";
}
var ClaudeCodeRefusalParity = class {
	id = "claude-code-refusal-parity";
	name = "Claude Code refusal parity";
	#fellBack = /* @__PURE__ */ new Map();
	#lengthResumes = /* @__PURE__ */ new Map();
	constructor({ settingsPath, modeless } = {}) {
		this.settingsPath = settingsPath;
		this.modeless = modeless;
	}
	#key(args) {
		return args.threadId ?? args.requestContext?.get?.("controller")?.threadId ?? "default";
	}
	processInputStep(args) {
		if (!this.#fellBack.get(this.#key(args))) return {};
		const modelId = typeof args.model === "string" ? args.model : args.model?.modelId ?? "";
		if (!CLAUDE_CODE_PARITY_PRIMARY.test(modelId)) return {};
		const controller = args.requestContext?.get?.("controller");
		const thinkingLevel = resolveRequestThinkingLevel(this.modeless ? { state: controller?.state } : controller, this.settingsPath, void 0);
		const model = resolveModel(CLAUDE_CODE_PARITY_FALLBACK, { thinkingLevel, remapForCodexOAuth: true, requestContext: args.requestContext });
		const providerOptions = { ...args.providerOptions ?? {} };
		if (providerOptions.anthropic?.fallbacks !== void 0) {
			const { fallbacks: _dropped, ...anthropic } = providerOptions.anthropic;
			providerOptions.anthropic = anthropic;
		}
		return { model, providerOptions };
	}
	processOutputStep({ finishReason, providerMetadata, abort, messageList, ...rest }) {
		const key = this.#key(rest);
		const before = this.#fellBack.get(key) === true;
		const hop = claudeCodeParityFallbackModel(providerMetadata);
		if (hop) this.#fellBack.set(key, true);
		if (claudeCodeParityIsRefusal(finishReason, providerMetadata)) {
			if (before || hop) return messageList;
			return abort(CLAUDE_CODE_PARITY_SENTINEL + CLAUDE_CODE_REFUSAL_MESSAGE, { retry: true, metadata: { claudeCodeParity: "refusal" } });
		}
		if (finishReason === "length" && process.env.MASTRA_CLAUDE_CODE_LENGTH_RESUME !== "0") {
			const resumes = this.#lengthResumes.get(key) ?? 0;
			if (resumes >= CLAUDE_CODE_LENGTH_RESUME_LIMIT) return messageList;
			this.#lengthResumes.set(key, resumes + 1);
			return abort(CLAUDE_CODE_PARITY_SENTINEL + CLAUDE_CODE_LENGTH_MESSAGE, { retry: true, metadata: { claudeCodeParity: "length", resume: resumes + 1 } });
		}
		return messageList;
	}
};
//#endregion
''' % SENT
s = sdk.read_text()
anchor = "//#region src/index.ts"
if s.count(anchor) != 1:
    # fall back: insert right after the import block
    idx = s.index("\n", s.rindex("\nimport ", 0, s.index("\nconst ") if "\nconst " in s else len(s)) + 1)
else:
    idx = s.index(anchor)
s = s[:idx] + processor + s[idx:]
sdk.write_text(s)
# one shared instance per agent; input lane switches the model, output lane detects refusals and fallbacks
sub(sdk, '''	const baseCodeAgent = createCodingAgent({''', '''	const claudeCodeRefusalParity = new ClaudeCodeRefusalParity({ settingsPath: config?.settingsPath, modeless });
	const baseCodeAgent = createCodingAgent({''')
sub(sdk, '''		inputProcessors: ({ requestContext }) => {
			const resolveProcessors = (configured) => [
				...configured,''', '''		inputProcessors: ({ requestContext }) => {
			const resolveProcessors = (configured) => [
				claudeCodeRefusalParity,
				...configured,''')
sub(sdk, '''		outputProcessors: ({ requestContext }) => {
			const resolveProcessors = (configured) => [
				new CyberRefusalHandler(),''', '''		outputProcessors: ({ requestContext }) => {
			const resolveProcessors = (configured) => [
				claudeCodeRefusalParity,''')
print("patched")
