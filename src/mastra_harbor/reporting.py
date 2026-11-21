"""Post-run export of the stock CLI event stream; no provider access."""

import gzip
import json
import os
import re
import zlib
from collections import Counter
from pathlib import Path

from harbor.models.agent.context import AgentContext
from harbor.models.trajectories import Trajectory


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _write(path, data):
    temporary = path.with_suffix(".tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as out:
        json.dump(data, out, ensure_ascii=False, indent=2)
        out.write("\n")
    temporary.replace(path)


def _update(message, event):
    """Apply native part replacements/deltas, following Mastra's display reducer."""
    parts = message["content"]["parts"]
    kind = event["type"]
    if kind == "text-delta":
        part = next((p for p in reversed(parts) if p.get("type") == "text"), None)
        if part is None:
            part = {"type": "text", "text": ""}
            parts.append(part)
        part["text"] += event["delta"]
    elif kind == "reasoning-delta":
        index = event["index"]
        if type(index) is not int or index < 0:
            raise ValueError("Invalid reasoning index")
        part = parts[index]
        if part["type"] != "reasoning":
            raise ValueError("Reasoning part missing")
        part["reasoning"] = part.get("reasoning", "") + event["delta"]
    elif kind == "part":
        index = event["index"]
        if type(index) is not int or index < 0 or index > len(parts):
            raise ValueError("Part index gap")
        if index == len(parts):
            parts.append(event["part"])
        else:
            parts[index] = event["part"]
    else:
        raise ValueError("Unsupported message update")


def _provenance(logs_dir, version, benchmark_version):
    """Copy only version identifiers, never credentials or full Harbor configs."""
    unreadable = []

    def read(path):
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise ValueError("Expected object")
            return data
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            unreadable.append(str(path.relative_to(logs_dir.parent.parent)))
            return {}

    trial = read(logs_dir.parent / "config.json").get("task", {})
    task = read(logs_dir.parent / "lock.json").get("task", {})
    source = trial.get("source") or task.get("source")
    datasets = read(logs_dir.parent.parent / "config.json").get("datasets", [])
    matches = [d for d in datasets if source and d.get("name") == source]
    dataset = matches[0] if len(matches) == 1 else {}
    ref = dataset.get("ref")
    recorded_version = dataset.get("version") or (ref if isinstance(ref, str)
        and re.fullmatch(r"\d+\.\d+(?:\.\d+)?", ref) else None)
    packages = read(logs_dir / "package-lock.json").get("packages", {})
    installed = packages.get("node_modules/mastracode", {}).get("version")
    bundle = read(logs_dir / "bundle.json")
    return {
        "mastracode": {"version": installed or bundle.get("version") or version,
            "version_source": "package-lock.json" if installed else "bundle_manifest" if bundle else "adapter_pin",
            "sdk_version": packages.get("node_modules/@mastra/code-sdk", {}).get("version")
                or bundle.get("packages", {}).get("@mastra/code-sdk"),
            **({"bundle": bundle} if bundle else {})},
        "benchmark": {"name": source, "version": recorded_version or benchmark_version,
            "version_source": "harbor_config" if recorded_version else "agent_argument" if benchmark_version else "unknown",
            "declared_version": benchmark_version, "resolved_ref": ref,
            "version_conflict": bool(recorded_version and benchmark_version and recorded_version != benchmark_version)},
        "task": {key: task.get(key) for key in ("name", "version", "digest", "git_commit_id")},
        "unreadable_metadata": unreadable,
    }


def _normalize_provider_usage(u):
    """Chat Completions (incl. DeepSeek), Responses API (incl. Codex) and Anthropic usage shapes."""
    details_in = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
    details_out = u.get("completion_tokens_details") or u.get("output_tokens_details") or {}
    cached = (details_in.get("cached_tokens") or u.get("prompt_cache_hit_tokens")
              or u.get("cache_read_input_tokens") or 0)
    write = details_in.get("cache_write_tokens") or u.get("cache_creation_input_tokens") or 0
    prompt = u.get("prompt_tokens", u.get("input_tokens", 0)) or 0
    if "cache_read_input_tokens" in u:  # Anthropic reports cache reads/writes outside input_tokens
        prompt += cached + write
    return {"input": prompt, "cached_input": cached, "cache_write": write,
            "output": u.get("completion_tokens", u.get("output_tokens", 0)) or 0,
            "reasoning": details_out.get("reasoning_tokens", 0) or 0}


def _provider_usage(logs_dir):
    """Summarize the optional usage tap: provider-reported usage for every model call."""
    path = logs_dir / "provider-usage.jsonl"
    if not path.exists():
        return None
    keys = ("input", "cached_input", "cache_write", "output", "reasoning")
    total, by_model = dict.fromkeys(keys, 0), {}
    calls = with_usage = failed = unreadable = 0
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            unreadable += 1
            continue
        calls += 1
        if (row.get("status") or 0) >= 400:
            failed += 1
        if not row.get("usage"):
            continue
        with_usage += 1
        usage = _normalize_provider_usage(row["usage"])
        model = by_model.setdefault(row.get("model") or row.get("responseModel") or "unknown", {"calls": 0, **dict.fromkeys(keys, 0)})
        model["calls"] += 1
        for key in keys:
            total[key] += usage[key]
            model[key] += usage[key]
    return {"calls": calls, "calls_with_usage": with_usage, "failed_calls": failed,
            "unreadable_lines": unreadable, "total": total, "by_model": by_model}


def _native_lines(source, issues):
    opener = gzip.open if source.suffix == ".gz" else open
    try:
        with opener(source, "rb") as stream:
            yield from stream
    except (EOFError, gzip.BadGzipFile, zlib.error):
        # A killed compressor may leave useful events without a valid footer.
        issues["incomplete_compressed_log"] += 1


def export_report(logs_dir: Path, context: AgentContext, *, version: str, model: str | None,
                  benchmark_version: str | None = None):
    provenance = _provenance(logs_dir, version, benchmark_version)
    _write(logs_dir / "provenance.json", provenance)
    source = logs_dir / "mastracode.jsonl"
    if not source.exists():
        source = logs_dir / "mastracode.jsonl.gz"
    if not source.exists():
        context.metadata = {**(context.metadata or {}), "mastracode": {
            "capture_status": "missing_native_log", "provenance": provenance}}
        return

    messages, calls, ends, shell = {}, {}, {}, {}
    pending, issues, totals, usage_rows = set(), Counter(), Counter(), []
    auxiliary, errors, result = [], [], {}
    terminal_control_lines = []
    for index, line in enumerate(_native_lines(source, issues)):
        # The stock CLI appends terminal-reset CSI sequences after its JSON result.
        # Keep their original bytes and line numbers; they contain no event data.
        if re.fullmatch(rb"(?:\x1b\[[0-?]*[ -/]*[@-~])+\s*", line):
            terminal_control_lines.append(index + 1)
            continue
        try:
            event = json.loads(line)
            kind = event["type"]
            if kind == "message_start":
                message = event["message"]
                mid = message["id"]
                if mid in messages:
                    issues["duplicate_message_start"] += 1
                messages[mid] = (index, message)
                pending.add(mid)
            elif kind == "message_update":
                _update(messages[event["id"]][1], event["event"])
            elif kind == "message_end":
                if event["id"] not in pending:
                    issues["orphan_message_end"] += 1
                pending.discard(event["id"])
            elif kind == "tool_start":
                cid = event["toolCallId"]
                if cid in calls:
                    issues["duplicate_tool_start"] += 1
                calls[cid] = (index, event)
            elif kind == "tool_end":
                if event["toolCallId"] in ends and ends[event["toolCallId"]] != event:
                    issues["conflicting_tool_results"] += 1
                ends[event["toolCallId"]] = event
            elif kind == "shell_output":
                # Keep raw chunks in the source log; don't duplicate large output.
                shell.setdefault(event["toolCallId"], []).append(index + 1)
            elif kind == "usage_update":
                usage = event["usage"]
                if not isinstance(usage, dict):
                    raise ValueError("Invalid usage")
                usage_rows.append(usage)
                for key in ("promptTokens", "completionTokens", "cachedInputTokens"):
                    value = usage.get(key)
                    if type(value) is int and value >= 0:
                        totals[key] += value
                    elif value is not None:
                        issues["invalid_usage"] += 1
            elif kind == "result":
                if result:
                    issues["duplicate_result"] += 1
                result = event
            elif kind == "error":
                errors.append(event)
            elif kind.startswith(("om_", "subagent_")):
                auxiliary.append(event)
        except (ValueError, KeyError, TypeError, AttributeError, IndexError, UnicodeError):
            issues["unreadable_or_unsupported_events"] += 1

    # usage_update is per model step. The CLI result repeats the LAST step only.
    for native, field in (("promptTokens", "n_input_tokens"), ("completionTokens", "n_output_tokens"),
                          ("cachedInputTokens", "n_cache_tokens")):
        known = usage_rows and all(type(row.get(native)) is int and row[native] >= 0 for row in usage_rows)
        setattr(context, field, totals[native] if known else None)
    context.cost_usd = None

    ordered, used, missing = [], set(), []

    def attach(step, cid, name, arguments, native):
        if cid in used:
            return
        used.add(cid)
        step.setdefault("tool_calls", []).append({
            "tool_call_id": cid, "function_name": name,
            "arguments": arguments if isinstance(arguments, dict) else {"native_arguments": arguments},
        })
        end = ends.get(cid, native)
        if "result" in end:
            step.setdefault("observation", {"results": []})["results"].append({
                "source_call_id": cid, "content": _text(end["result"]),
                "extra": {"is_error": end.get("isError"), "shell_output_lines": shell.get(cid, [])},
            })
        else:
            missing.append(cid)
            step.setdefault("extra", {})["partial_shell_output_lines"] = shell.get(cid, [])

    for mid, (index, message) in messages.items():
        role, content = message.get("role"), message.get("content", {})
        parts = content.get("parts", []) if isinstance(content, dict) else []
        texts, reasoning, tools, other = [], [], [], []
        for part in parts:
            if part.get("type") == "text":
                texts.append(part.get("text", ""))
            elif part.get("type") == "reasoning":
                reasoning.append(part.get("reasoning", ""))
            elif part.get("type") == "tool-invocation":
                tools.append(part["toolInvocation"])
            elif part.get("type") == "data-user-message":
                role = "user"
                texts.append(_text(part.get("data", {}).get("contents", "")))
            else:
                other.append(part)
        if isinstance(content, str):
            texts.append(content)
        if role not in {"user", "system", "assistant"}:
            auxiliary.append({"type": "native_message", "message": message})
            continue
        step = {"source": "agent" if role == "assistant" else role, "message": "".join(texts),
                "extra": {"native_message_id": mid}}
        if reasoning:
            step["reasoning_content"] = "".join(reasoning)
        if other:
            step["extra"]["native_parts"] = other
        if role == "assistant":
            for tool in tools:
                attach(step, tool["toolCallId"], tool["toolName"], tool.get("args", {}), tool)
        ordered.append((index, step))
    for cid, (index, call) in calls.items():
        if cid not in used:
            step = {"source": "agent", "message": "", "extra": {"from_tool_events": True}}
            attach(step, cid, call["toolName"], call.get("args", {}), {})
            ordered.append((index, step))
    prompt = logs_dir / "prompt.txt"
    if not any(step["source"] == "user" for _, step in ordered) and prompt.exists():
        ordered.append((-1, {"source": "user", "message": prompt.read_text(), "extra": {"from_prompt_file": True}}))
    ordered.sort(key=lambda pair: pair[0])
    steps = [dict(step_id=i, **step) for i, (_, step) in enumerate(ordered, 1)]
    orphan_results = [event for cid, event in ends.items() if cid not in used]
    metadata = {
        "provenance": provenance,
        "terminal_control_lines": terminal_control_lines,
        "capture_status": "partial" if issues or pending or missing or orphan_results or errors
            or result.get("status") not in {"completed", "done"} or not steps else "native_cli_only",
        "native_result_seen": bool(result), "result_status": result.get("status"),
        "issues": dict(issues), "unfinished_message_ids": sorted(pending), "missing_tool_results": missing,
        "orphan_tool_results": orphan_results,
        "usage_scope": "observed_main_agent_steps_only", "usage_events": len(usage_rows),
        "last_step_usage_from_result": result.get("usage"),
        "auxiliary_usage": "unavailable", "cost_usd": None,
        "provider_capture": "unavailable", "native_log": source.name,
    }
    tapped = _provider_usage(logs_dir)
    if tapped:
        main = {"input": context.n_input_tokens, "cached_input": context.n_cache_tokens, "output": context.n_output_tokens}
        tapped["beyond_main_agent"] = {k: (tapped["total"][k] - v if v is not None else None) for k, v in main.items()}
        metadata.update(provider_capture="usage_tap", provider_usage=tapped,
                        auxiliary_usage="included_in_provider_usage")
    context.metadata = {**(context.metadata or {}), "mastracode": metadata}
    _write(logs_dir / "usage.json", {**metadata, "input_tokens": context.n_input_tokens,
                                    "output_tokens": context.n_output_tokens, "cached_tokens": context.n_cache_tokens})
    if not steps:
        return  # No fabricated system/assistant message just to satisfy the schema.
    trajectory = Trajectory.model_validate({
        "schema_version": "ATIF-v1.7", "session_id": result.get("threadId"),
        "agent": {"name": "mastracode", "version": provenance["mastracode"]["version"], "model_name": model}, "steps": steps,
        "notes": "Native CLI view, not a complete provider transcript. Messages may span multiple model calls. "
                 "System prompts, full auxiliary conversations, per-step billing and costs are unavailable. "
                 "Shell line references point into the unchanged native JSONL. Non-text parts remain in extra.",
        "final_metrics": {"total_prompt_tokens": context.n_input_tokens, "total_completion_tokens": context.n_output_tokens,
                          "total_cached_tokens": context.n_cache_tokens, "total_steps": len(steps)},
        "extra": {**metadata, "auxiliary_events": auxiliary, "errors": errors,
                  "terminal_result": {k: result[k] for k in ("status", "exitCode", "error", "finishReason") if k in result}},
    })
    _write(logs_dir / "trajectory.json", trajectory.to_json_dict())
