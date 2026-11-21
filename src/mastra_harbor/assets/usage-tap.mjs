// Records provider-reported token usage for every model API call made by this Node process.
// Loaded with `node --import`. It never alters what the caller sends or receives: it reads
// clones of the request and response in the background and swallows every failure.
import { appendFileSync } from "node:fs";
import { createHash } from "node:crypto";

const OUT = process.env.MASTRA_USAGE_TAP_FILE;
const MODEL_HOSTS = /(^|\.)(api\.deepseek\.com|api\.openai\.com|chatgpt\.com|api\.anthropic\.com|generativelanguage\.googleapis\.com|openrouter\.ai)$/;
const EXTRA_HOST = process.env.MASTRA_USAGE_TAP_EXTRA_HOST; // tests only, e.g. a local mock server
const original = globalThis.fetch;

function modelHost(input) {
  try {
    const url = new URL(typeof input === "string" || input instanceof URL ? String(input) : input.url);
    return MODEL_HOSTS.test(url.hostname) || url.hostname === EXTRA_HOST ? url : null;
  } catch {
    return null;
  }
}

const seenToolSets = new Set();

function toolsOf(json) {
  // Names of the tools offered to the model (no schemas). Full list once per distinct set, then hash + count.
  if (!Array.isArray(json.tools)) return {};
  const names = json.tools.map((t) => t?.name ?? t?.function?.name ?? t?.type).filter(Boolean);
  const key = names.join(",");
  const hash = createHash("sha256").update(key).digest("hex").slice(0, 10);
  if (seenToolSets.has(key)) return { tools_hash: hash, tools_n: names.length };
  seenToolSets.add(key);
  return { tools_hash: hash, tools_n: names.length, tools: names };
}

function describeBody(text) {
  // Model id and a short fingerprint of the system prompt (groups main agent, subagents, memory).
  try {
    const json = JSON.parse(text);
    const items = json.messages || json.input || [];
    const first = Array.isArray(items) ? items.find((m) => m.role === "system" || m.role === "developer") : null;
    // Prefer the agent's own system/developer message; Codex's top-level `instructions` is a fixed preamble.
    // Anthropic carries the system prompt in a top-level `system` (string or blocks).
    const system = first ? (typeof first.content === "string" ? first.content : JSON.stringify(first.content))
      : json.system !== undefined ? (typeof json.system === "string" ? json.system : JSON.stringify(json.system))
      : (json.instructions ?? "");
    return {
      model: json.model,
      system: createHash("sha256").update(String(system).slice(0, 2000)).digest("hex").slice(0, 10),
      items: Array.isArray(items) ? items.length : undefined,
      // Requested reasoning settings, as sent on the wire (no message content).
      ...(json.thinking ? { thinking: json.thinking } : {}),
      ...(json.output_config?.effort ? { effort: json.output_config.effort } : {}),
      ...(json.reasoning ? { reasoning: json.reasoning } : {}),
      ...(json.max_tokens ? { max_tokens: json.max_tokens } : {}),
      ...toolsOf(json),
    };
  } catch {
    return {};
  }
}

function usageOf(obj) {
  // Chat completions (incl. DeepSeek): {usage}. Responses API (incl. Codex): {response:{usage}}.
  return obj?.usage ?? obj?.response?.usage ?? null;
}

async function record(url, requestText, response, started) {
  let text = "", aborted = false;
  try {
    const reader = response.body?.getReader();
    const decoder = new TextDecoder();
    for (;;) {
      const { value, done } = reader ? await reader.read() : { done: true };
      if (done) break;
      text += decoder.decode(value, { stream: true });
    }
  } catch {
    aborted = true; // caller cancelled or the connection dropped; keep what arrived
  }
  let usage = null, responseModel, events = 0, stop;
  if (/^\s*(event|data):/m.test(text.slice(0, 2000))) {
    for (const line of text.split("\n")) {
      if (!line.startsWith("data:")) continue;
      const data = line.slice(5).trim();
      if (!data || data === "[DONE]") continue;
      try {
        const obj = JSON.parse(data);
        events++;
        responseModel ??= obj.model ?? obj.response?.model ?? obj.message?.model;
        // Anthropic: message_start holds input/cache usage, message_delta the final output usage.
        if (obj.type === "message_start" && obj.message?.usage) usage = { ...obj.message.usage };
        else if (obj.type === "message_delta" && obj.usage) usage = { ...(usage ?? {}), ...obj.usage };
        else usage = usageOf(obj) ?? usage;
        if (obj.type === "message_delta" && obj.delta?.stop_reason) {
          stop = { reason: obj.delta.stop_reason, ...(obj.delta.stop_details ? { details: obj.delta.stop_details } : {}) };
        }
      } catch {}
    }
  } else {
    try {
      const obj = JSON.parse(text);
      responseModel = obj.model;
      usage = usageOf(obj);
      if (obj.stop_reason) stop = { reason: obj.stop_reason, ...(obj.stop_details ? { details: obj.stop_details } : {}) };
    } catch {}
  }
  // Subscription rate-limit state the provider reports on each call (utilization and reset times, no secrets).
  const limits = {};
  try {
    response.headers.forEach((value, name) => {
      if (/^anthropic-ratelimit-unified-/.test(name)) limits[name.slice("anthropic-ratelimit-unified-".length)] = value;
    });
  } catch {}
  appendFileSync(OUT, JSON.stringify({
    t: new Date(started).toISOString(), ms: Date.now() - started, status: response.status,
    host: url.hostname, path: url.pathname, ...describeBody(requestText), responseModel, events, usage,
    ...(stop ? { stop } : {}),
    ...(Object.keys(limits).length ? { limits } : {}),
    ...(aborted ? { aborted: true } : {}),
    // Provider error text for failed calls (e.g. rejected models); bodies of successful calls are never kept.
    ...(response.status >= 400 ? { error: text.slice(0, 500) } : {}),
  }) + "\n");
}

if (OUT && typeof original === "function") {
  globalThis.fetch = async function tappedFetch(input, init) {
    const url = modelHost(input);
    let requestText = null;
    if (url) {
      try {
        if (typeof init?.body === "string") requestText = Promise.resolve(init.body);
        else if (input instanceof Request && !init?.body) requestText = input.clone().text();
      } catch {}
    }
    const started = Date.now();
    const response = await original.call(this, input, init);
    if (url) {
      try {
        const copy = response.clone();
        Promise.resolve(requestText).then((body) => record(url, body ?? "", copy, started)).catch(() => {});
      } catch {}
    }
    return response;
  };
}
