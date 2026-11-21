"""Sanity checks for native-event conversion, separate from agent execution."""
import gzip
import json
import pytest

from harbor.models.agent.context import AgentContext
from harbor.utils.trajectory_validator import TrajectoryValidator
from mastra_harbor.agent import MastraCode


def write_log(path, events, tail=""):
    (path / "prompt.txt").write_text("Run the check")
    (path / "mastracode.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events) + tail)


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("dataset,label,expected_version", [
    ({"name": "terminal-bench", "version": "2.0"}, None, "2.0"),
    ({"name": "terminal-bench/terminal-bench", "ref": "sha256:dataset"}, "4.0.0", "4.0.0"),
])
def test_native_deltas_tools_and_usage_export_without_double_counting(tmp_path, dataset, label, expected_version, compressed):
    job = tmp_path / "job"
    tmp_path = job / "trial" / "agent"
    tmp_path.mkdir(parents=True)
    (job / "config.json").write_text(json.dumps({"datasets": [dataset], "env": {"SECRET": "do-not-copy"}}))
    (tmp_path.parent / "config.json").write_text(json.dumps({"task": {"source": dataset["name"]}}))
    (tmp_path.parent / "lock.json").write_text(json.dumps({"task": {"name": "example", "digest": "sha256:task"}}))
    (tmp_path / "package-lock.json").write_text(json.dumps({"packages": {
        "node_modules/mastracode": {"version": "0.43.0"},
        "node_modules/@mastra/code-sdk": {"version": "1.9.0"},
    }}))
    tool = {"toolCallId": "c", "toolName": "execute_command", "args": {"command": "true"}}
    write_log(tmp_path, [
        {"type": "message_start", "message": {"id": "m", "role": "assistant", "content": {
            "parts": [{"type": "text", "text": ""}]}}},
        {"type": "message_update", "id": "m", "event": {"type": "text-delta", "delta": "Checking."}},
        {"type": "message_update", "id": "m", "event": {"type": "part", "index": 1,
            "part": {"type": "reasoning", "reasoning": ""}}},
        {"type": "message_update", "id": "m", "event": {"type": "reasoning-delta", "index": 1, "delta": "Use the shell."}},
        {"type": "message_update", "id": "m", "event": {"type": "part", "index": 2,
            "part": {"type": "tool-invocation", "toolInvocation": tool}}},
        {"type": "tool_start", **tool},
        {"type": "tool_end", "toolCallId": "c", "result": {"exitCode": 0}, "isError": False},
        {"type": "message_end", "id": "m"},
        {"type": "usage_update", "usage": {"promptTokens": 20, "completionTokens": 3, "cachedInputTokens": 5}},
        {"type": "usage_update", "usage": {"promptTokens": 70, "completionTokens": 11, "cachedInputTokens": 10}},
        {"type": "om_observation_end", "tokensObserved": 30000, "observationTokens": 4000},
        {"type": "result", "status": "completed", "exitCode": 0, "threadId": "t",
            "usage": {"inputTokens": 70, "outputTokens": 11}},
    ], tail="\x1b[?2004l\x1b[<u\x1b[>4;0m\x1b[?25h")
    original = (tmp_path / "mastracode.jsonl").read_bytes()
    if compressed:
        (tmp_path / "mastracode.jsonl.gz").write_bytes(gzip.compress(original))
        (tmp_path / "mastracode.jsonl").unlink()
    context = AgentContext()
    agent = MastraCode(logs_dir=tmp_path, model_name="provider/model", benchmark_version=label)
    agent.populate_context_post_run(context)
    assert agent.capabilities.atif
    assert (context.n_input_tokens, context.n_output_tokens, context.n_cache_tokens) == (90, 14, 15)
    assert context.cost_usd is None
    validator = TrajectoryValidator()
    assert validator.validate(tmp_path / "trajectory.json"), validator.get_errors()
    trajectory = json.loads((tmp_path / "trajectory.json").read_text())
    provenance = json.loads((tmp_path / "provenance.json").read_text())
    assert trajectory["extra"]["provenance"] == provenance
    assert json.loads((tmp_path / "usage.json").read_text())["provenance"] == provenance
    assert context.metadata["mastracode"]["provenance"] == provenance
    assert trajectory["agent"]["version"] == provenance["mastracode"]["version"] == "0.43.0"
    assert provenance["benchmark"]["name"] == dataset["name"]
    assert provenance["benchmark"]["version"] == expected_version
    assert provenance["benchmark"]["resolved_ref"] == dataset.get("ref")
    assert provenance["task"]["digest"] == "sha256:task"
    assert "do-not-copy" not in json.dumps(provenance)
    step = trajectory["steps"][1]
    assert step["message"] == "Checking." and step["reasoning_content"] == "Use the shell."
    assert len(step["tool_calls"]) == 1 and step["observation"]["results"][0]["source_call_id"] == "c"
    assert trajectory["extra"]["capture_status"] == "native_cli_only"
    assert trajectory["extra"]["terminal_control_lines"] == [13]
    assert trajectory["extra"]["auxiliary_usage"] == "unavailable"
    assert "total_cost_usd" not in trajectory["final_metrics"]
    before = (tmp_path / "trajectory.json").read_bytes()
    agent.populate_context_post_run(AgentContext())
    assert (tmp_path / "trajectory.json").read_bytes() == before
    stored = gzip.decompress((tmp_path / "mastracode.jsonl.gz").read_bytes()) if compressed else (tmp_path / "mastracode.jsonl").read_bytes()
    assert stored == original


def test_truncated_gzip_keeps_events_but_marks_capture_partial(tmp_path):
    events = [
        {"type": "message_start", "message": {"id": "m", "role": "assistant", "content": {"parts": [{"type": "text", "text": "done"}]}}},
        {"type": "message_end", "id": "m"},
        {"type": "result", "status": "completed", "exitCode": 0},
    ]
    write_log(tmp_path, events)
    raw = tmp_path / "mastracode.jsonl"
    (tmp_path / "mastracode.jsonl.gz").write_bytes(gzip.compress(raw.read_bytes())[:-8])
    raw.unlink()
    context = AgentContext()
    MastraCode(logs_dir=tmp_path).populate_context_post_run(context)
    trace = json.loads((tmp_path / "trajectory.json").read_text())
    assert trace["steps"][1]["message"] == "done"
    assert trace["extra"]["capture_status"] == "partial"
    assert trace["extra"]["issues"]["incomplete_compressed_log"] == 1


def test_interrupted_log_preserves_partial_evidence_and_unknown_totals(tmp_path):
    write_log(tmp_path, [
        {"type": "tool_start", "toolCallId": "pending", "toolName": "execute_command", "args": {}},
        {"type": "shell_output", "toolCallId": "pending", "output": "still working", "stream": "stdout"},
        {"type": "result", "status": "timeout", "usage": {"inputTokens": 50, "outputTokens": 2}},
    ], tail='{"type":')
    context = AgentContext()
    MastraCode(logs_dir=tmp_path).populate_context_post_run(context)
    trajectory = json.loads((tmp_path / "trajectory.json").read_text())
    assert trajectory["extra"]["capture_status"] == "partial"
    assert trajectory["extra"]["missing_tool_results"] == ["pending"]
    assert trajectory["extra"]["issues"]["unreadable_or_unsupported_events"] == 1
    assert trajectory["steps"][1]["extra"]["partial_shell_output_lines"] == [2]
    assert "observation" not in trajectory["steps"][1]
    assert context.n_input_tokens is None and context.n_output_tokens is None and context.cost_usd is None
    assert context.metadata["mastracode"]["last_step_usage_from_result"]["inputTokens"] == 50
    assert trajectory["extra"]["provenance"]["benchmark"]["version"] is None


def test_reporting_failure_is_visible_without_becoming_an_agent_failure(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("private detail")
    monkeypatch.setattr("mastra_harbor.agent.export_report", fail)
    context = AgentContext()
    MastraCode(logs_dir=tmp_path).populate_context_post_run(context)
    assert context.metadata["mastracode_reporting_error"] == "OSError"
    assert "private detail" not in str(context.metadata)


def test_bundle_manifest_supplies_snapshot_provenance(tmp_path):
    write_log(tmp_path, [{"type": "result", "status": "completed", "usage": {"inputTokens": 1, "outputTokens": 1}}])
    bundle = {"version": "0.47.2-alpha.0", "sha256": "ab" * 32, "source": {"sha": "b7e71a6"},
              "packages": {"@mastra/code-sdk": "1.15.0-alpha.0"}}
    (tmp_path / "bundle.json").write_text(json.dumps(bundle))
    MastraCode(logs_dir=tmp_path).populate_context_post_run(AgentContext())
    recorded = json.loads((tmp_path / "provenance.json").read_text())["mastracode"]
    assert recorded["version"] == "0.47.2-alpha.0" and recorded["version_source"] == "bundle_manifest"
    assert recorded["sdk_version"] == "1.15.0-alpha.0" and recorded["bundle"] == bundle


def test_usage_tap_totals_cover_calls_beyond_the_main_agent(tmp_path):
    write_log(tmp_path, [
        {"type": "usage_update", "usage": {"promptTokens": 100, "completionTokens": 10, "cachedInputTokens": 40}},
        {"type": "result", "status": "completed", "usage": {"inputTokens": 100, "outputTokens": 10}},
    ])
    rows = [
        {"status": 200, "model": "gpt-6.1-sol", "usage": {"input_tokens": 100, "output_tokens": 10,
            "input_tokens_details": {"cached_tokens": 40}, "output_tokens_details": {"reasoning_tokens": 4}}},
        {"status": 200, "model": "gpt-6.1-sol", "usage": {"input_tokens": 300, "output_tokens": 20,
            "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}},
        {"status": 200, "model": "deepseek-v4-flash", "usage": {"prompt_tokens": 50, "completion_tokens": 5,
            "prompt_cache_hit_tokens": 10, "completion_tokens_details": {"reasoning_tokens": 2}}},
        {"status": 400, "model": "gpt-5.4-mini", "usage": None},
    ]
    (tmp_path / "provider-usage.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
    context = AgentContext()
    MastraCode(logs_dir=tmp_path).populate_context_post_run(context)
    tapped = json.loads((tmp_path / "usage.json").read_text())["provider_usage"]
    assert tapped["calls"] == 4 and tapped["calls_with_usage"] == 3 and tapped["failed_calls"] == 1
    assert tapped["unreadable_lines"] == 1
    assert tapped["total"] == {"input": 450, "cached_input": 50, "cache_write": 0, "output": 35, "reasoning": 6}
    assert tapped["by_model"]["deepseek-v4-flash"]["cached_input"] == 10
    assert tapped["beyond_main_agent"] == {"input": 350, "cached_input": 10, "output": 25}
