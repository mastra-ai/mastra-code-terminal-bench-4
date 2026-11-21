"""Small sanity checks for the adapter boundary; no simulated model behavior."""
import asyncio
import json
import shlex
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from harbor.models.agent.context import AgentContext
from mastra_harbor.agent import MastraCode


class Environment:
    default_user = "1000"

    def __init__(self, return_code=0):
        self.calls = []
        self.files = {}
        self.return_code = return_code

    async def exec(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(return_code=self.return_code if "--prompt" in kwargs["command"] else 0, stdout="", stderr="")

    async def upload_file(self, source_path, target_path):
        self.files[str(target_path)] = Path(source_path).read_bytes()


def _launch(env):
    return [c for c in env.calls if "--prompt" in c["command"]][-1]


def test_stock_launch_keeps_prompt_credentials_and_state_separate(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-secret")
    agent = MastraCode(logs_dir=tmp_path, credential_env="GOOGLE_API_KEY")
    env = Environment()
    prompt = "Write 'ok'; $(do-not-execute-this)"
    asyncio.run(agent.run(prompt, env, AgentContext()))
    first = _launch(env)
    assert env.calls[-1]["command"].endswith("credentials.env") and env.calls[-1]["user"] == "root"
    assert prompt.encode() in env.files.values()
    assert prompt not in first["command"] and "test-secret" not in first["command"]
    assert "test-secret" not in json.dumps(env.calls)
    [secrets] = [k for k in env.files if k.endswith("/credentials.env")]
    assert env.files[secrets] == b"export GOOGLE_API_KEY=test-secret\n"
    assert f"set -a; . {secrets}; set +a; rm -f {secrets};" in first["command"]
    assert "--prompt - --output jsonl" in first["command"]
    assert "umask" not in first["command"]  # Task tools inherit the task environment's mask.
    assert "--settings /logs/agent/settings.json" in first["command"]
    assert json.loads(env.files["/logs/agent/settings.json"]) == {
        "models": {"omObservationThreshold": 120_000, "omReflectionThreshold": 40_000},
        "preferences": {"quietMode": True},
    }
    for override in ("--thinking-level", "--timeout", "--max-turns", "--model", "--import"):
        assert override not in first["command"]
    assert set(first["env"]) == {"MASTRA_APP_DATA_DIR"}
    assert first.get("cwd") is None
    asyncio.run(agent.run(prompt, env, AgentContext()))
    assert first["env"]["MASTRA_APP_DATA_DIR"] != _launch(env)["env"]["MASTRA_APP_DATA_DIR"]


def test_selected_model_is_forwarded_and_cli_failure_is_not_hidden(tmp_path):
    agent = MastraCode(logs_dir=tmp_path, model_name="provider/model")
    env = Environment()
    asyncio.run(agent.run("hello", env, AgentContext()))
    assert "--model provider/model" in env.calls[-1]["command"]
    env.return_code = 2
    with pytest.raises(RuntimeError):
        asyncio.run(agent.run("hello", env, AgentContext()))


@pytest.mark.parametrize("uid_gid", ["0\n0\n", "65534\n65534\n"])
def test_implicit_image_user_owns_private_inputs_before_launch(tmp_path, uid_gid):
    class ImageUserEnvironment(Environment):
        default_user = None

        async def exec(self, **kwargs):
            result = await super().exec(**kwargs)
            if kwargs["command"] == "set -o pipefail; id -u; id -g":
                result.stdout = uid_gid
            return result

    env = ImageUserEnvironment()
    asyncio.run(MastraCode(logs_dir=tmp_path).run("hello", env, AgentContext()))
    identity, ownership, launch = env.calls[-3:]
    assert identity["user"] is None and launch["user"] is None
    owner = ":".join(uid_gid.split())
    assert ownership["user"] == "root"
    assert ownership["command"] == (
        f"set -o pipefail; chown {owner} /logs/agent/prompt.txt /logs/agent/settings.json && "
        "chmod 600 /logs/agent/prompt.txt /logs/agent/settings.json"
    )
    assert "--prompt -" in launch["command"]


@pytest.mark.parametrize("exit_code", [0, 7])
def test_file_stdout_preserves_large_node_write_and_exit(tmp_path, exit_code):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the stdout flush regression check")
    env = Environment()
    asyncio.run(MastraCode(logs_dir=tmp_path).run("hello", env, AgentContext()))
    for name, content in env.files.items():
        (tmp_path / Path(name).name).write_bytes(content)
    producer = tmp_path / "producer.cjs"
    producer.write_text(
        'process.stdout.write(JSON.stringify({type:"result",text:"x".repeat(2*1024*1024)})+"\\n");'
        f"process.exit({exit_code});\n"
    )
    command = env.calls[-1]["command"].replace(
        "/opt/mastra-harbor/node/bin/node /opt/mastra-harbor/node_modules/mastracode/dist/cli.js",
        shlex.join([node, str(producer)]),
    ).replace("/logs/agent", str(tmp_path))
    result = subprocess.run(["bash", "-c", command], capture_output=True, timeout=5)
    assert result.returncode == exit_code
    raw = (tmp_path / "mastracode.jsonl").read_bytes()
    assert raw.endswith(b"\n")
    assert json.loads(raw) == {"type": "result", "text": "x" * (2 * 1024 * 1024)}


def test_thinking_level_is_forwarded_and_validated(tmp_path):
    env = Environment()
    asyncio.run(MastraCode(logs_dir=tmp_path, thinking_level="max").run("hello", env, AgentContext()))
    assert "--thinking-level max" in env.calls[-1]["command"]
    with pytest.raises(ValueError):
        MastraCode(logs_dir=tmp_path, thinking_level="extreme")


def _bundle(tmp_path, content=b"bundle bytes"):
    import hashlib
    bundle = tmp_path / "mc.tar"
    bundle.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    manifest = {"version": "0.47.2-alpha.0", "sha256": digest, "source": {"sha": "b7e71a6"},
                "packages": {"@mastra/code-sdk": "1.15.0-alpha.0"}}
    bundle.with_suffix(".json").write_text(json.dumps(manifest))
    return bundle, digest, manifest


def test_bundle_install_uploads_verified_snapshot_and_records_it(tmp_path):
    bundle, digest, manifest = _bundle(tmp_path)
    agent = MastraCode(logs_dir=tmp_path / "logs", bundle=str(bundle), bundle_sha256=digest)
    assert agent.version() == "0.47.2-alpha.0"
    env = Environment()
    asyncio.run(agent.install(env))
    assert env.files["/opt/mastra-harbor/mastracode-bundle.tar"] == b"bundle bytes"
    assert json.loads(env.files["/logs/agent/bundle.json"]) == manifest
    commands = [call["command"] for call in env.calls]
    assert any(f"MC_BUNDLE_SHA256={digest} bash /opt/mastra-harbor/install.sh" in c for c in commands)
    assert any("node_modules/mastracode/pnpm-lock.yaml /logs/agent/pnpm-lock.yaml" in c for c in commands)
    assert not any("npm install" in c for c in commands)


def test_bundle_rejects_mismatched_or_missing_digest(tmp_path):
    bundle, digest, _ = _bundle(tmp_path)
    with pytest.raises(ValueError):
        MastraCode(logs_dir=tmp_path, bundle=str(bundle), bundle_sha256="0" * 64)
    with pytest.raises(ValueError):
        MastraCode(logs_dir=tmp_path, bundle=str(bundle))
    with pytest.raises(ValueError):
        MastraCode(logs_dir=tmp_path, bundle_sha256=digest)
    with pytest.raises(ValueError):
        MastraCode(logs_dir=tmp_path, bundle=str(bundle), bundle_sha256=digest, version="0.44.1")


def test_install_script_verifies_and_unpacks_bundle(tmp_path):
    import hashlib, tarfile
    from importlib.resources import files
    app = tmp_path / "src" / "app"
    (app / "dist").mkdir(parents=True)
    (app / "dist" / "cli.js").write_text("ok\n")
    archive = tmp_path / "mastracode-bundle.tar"
    with tarfile.open(archive, "w") as tar:
        tar.add(app, arcname="app")
    script = files("mastra_harbor").joinpath("assets/install.sh").read_text()
    start = script.index('if [ -n "${MC_BUNDLE_SHA256:-}" ]')
    block = script[start:script.index("\nfi\n", start) + 4]
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    run = lambda sha: subprocess.run(["bash", "-euo", "pipefail", "-c", block], cwd=tmp_path,
                                     env={"PATH": "/usr/bin:/bin", "MC_BUNDLE_SHA256": sha}, capture_output=True)
    assert run("0" * 64).returncode != 0 and archive.exists()
    assert run(digest).returncode == 0
    assert (tmp_path / "node_modules/mastracode/dist/cli.js").read_text() == "ok\n"
    assert not archive.exists()


def test_oauth_login_reaches_trial_without_refresh_token(tmp_path):
    import time
    auth = tmp_path / "auth.json"
    far = int((time.time() + 240 * 3600) * 1000)
    auth.write_text(json.dumps({"openai-codex": {"access": "acc", "refresh": "RT-secret", "expires": far, "type": "oauth"},
                                "accounts:openai-codex:1": {"access": "acc", "refresh": "RT-secret", "expires": far}}))
    env = Environment()
    agent = MastraCode(logs_dir=tmp_path / "logs", oauth_auth_file=str(auth))
    asyncio.run(agent.run("hello", env, AgentContext()))
    state = next(c for c in env.calls if "--prompt" in c["command"])["env"]["MASTRA_APP_DATA_DIR"]
    uploaded = json.loads(env.files[f"{state}/auth.json"])
    assert all(v["refresh"] == "" and v["access"] == "acc" for v in uploaded.values())
    assert b"RT-secret" not in env.files[f"{state}/auth.json"]
    launch = next(c for c in env.calls if "--prompt" in c["command"])
    assert "acc" not in launch["command"] and set(launch["env"]) == {"MASTRA_APP_DATA_DIR"}
    assert env.calls[-1]["command"].endswith(f"rm -f {state}/auth.json") and env.calls[-1]["user"] == "root"
    soon = int((time.time() + 3600) * 1000)
    auth.write_text(json.dumps({"openai-codex": {"access": "acc", "refresh": "ref", "expires": soon}}))
    with pytest.raises(ValueError):
        asyncio.run(agent.run("hello", env, AgentContext()))


def test_credentials_file_is_sourced_then_removed_before_launch(tmp_path, monkeypatch):
    value = "a'b $HOME \"q\" `x`"
    monkeypatch.setenv("TAVILY_API_KEY", value)
    env = Environment()
    asyncio.run(MastraCode(logs_dir=tmp_path, credential_env="TAVILY_API_KEY").run("hi", env, AgentContext()))
    [secrets] = [k for k in env.files if k.endswith("/credentials.env")]
    local = tmp_path / "credentials.env"
    local.write_bytes(env.files[secrets])
    command = _launch(env)["command"].replace(secrets, str(local))
    probe = command[:command.index('export PATH=')] + 'test ! -e "$CRED" && printf %s "$TAVILY_API_KEY"'
    result = subprocess.run(["bash", "-c", probe], env={"PATH": "/usr/bin:/bin", "CRED": str(local)},
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and result.stdout == value
    assert value not in json.dumps(env.calls)


def test_usage_tap_is_opt_in_and_only_adds_a_preload(tmp_path):
    env = Environment()
    asyncio.run(MastraCode(logs_dir=tmp_path).run("hi", env, AgentContext()))
    plain = _launch(env)
    assert "--import" not in plain["command"] and "MASTRA_USAGE_TAP_FILE" not in plain["env"]
    env = Environment()
    agent = MastraCode(logs_dir=tmp_path, usage_tap="true")
    asyncio.run(agent.install(env))
    assert env.files["/opt/mastra-harbor/usage-tap.mjs"].startswith(b"// Records provider-reported token usage")
    asyncio.run(agent.run("hi", env, AgentContext()))
    tapped = _launch(env)
    assert "/opt/mastra-harbor/node/bin/node --import /opt/mastra-harbor/usage-tap.mjs /opt/mastra-harbor/node_modules/mastracode/dist/cli.js" in tapped["command"]
    assert tapped["env"]["MASTRA_USAGE_TAP_FILE"] == "/logs/agent/provider-usage.jsonl"
    assert tapped["command"].replace("--import /opt/mastra-harbor/usage-tap.mjs ", "").split("--resource-id")[0] == \
        plain["command"].split("--resource-id")[0]


def test_mcp_servers_registered_globally_with_sse_mapped_to_streamable_http(tmp_path):
    from harbor.models.task.config import MCPServerConfig
    from mastra_harbor.agent import mastra_mcp_config
    servers = [MCPServerConfig(name="playwright", transport="sse", url="http://playwright-mcp:3080/sse"),
               MCPServerConfig(name="api", transport="streamable-http", url="http://api:9000/mcp"),
               MCPServerConfig(name="local", transport="stdio", command="npx", args=["srv"])]
    config, notes = mastra_mcp_config(servers)
    assert config == {"mcpServers": {"playwright": {"url": "http://playwright-mcp:3080/mcp"},
                                     "api": {"url": "http://api:9000/mcp"},
                                     "local": {"command": "npx", "args": ["srv"]}}}
    assert [n["server"] for n in notes] == ["playwright"]

    class HomeEnv(Environment):
        async def exec(self, **kwargs):
            r = await super().exec(**kwargs)
            if 'printf %s "$HOME"' in kwargs["command"]:
                r.stdout = "/home/agent"
            return r

    env = HomeEnv()
    agent = MastraCode(logs_dir=tmp_path, mcp_servers=servers[:1])
    asyncio.run(agent.run("hi", env, AgentContext()))
    assert json.loads(env.files["/home/agent/.mastracode/mcp.json"]) == {"mcpServers": {"playwright": {"url": "http://playwright-mcp:3080/mcp"}}}
    assert json.loads(env.files["/logs/agent/mcp-registration.json"])["transport_notes"][0]["declared"].endswith("/sse")
    preflight = next(c for c in env.calls if "--input-type=module" in c["command"])
    assert json.loads(preflight["env"]["MC_MCP_SERVERS"])["mcpServers"]["playwright"]["url"].endswith("/mcp")
    assert env.calls.index(preflight) < env.calls.index(_launch(env))


def test_mcp_preflight_failure_stops_before_the_agent_starts(tmp_path):
    from harbor.models.task.config import MCPServerConfig

    class NoToolsEnv(Environment):
        async def exec(self, **kwargs):
            r = await super().exec(**kwargs)
            if "--input-type=module" in kwargs["command"]:
                r.return_code = 3
            return r

    env = NoToolsEnv()
    agent = MastraCode(logs_dir=tmp_path, mcp_servers=[MCPServerConfig(name="p", transport="sse", url="http://p:1/sse")])
    with pytest.raises(RuntimeError, match="MCP preflight"):
        asyncio.run(agent.run("hi", env, AgentContext()))
    assert not any("--prompt" in c["command"] for c in env.calls)


def test_settings_file_replaces_packaged_settings(tmp_path):
    custom = tmp_path / "custom-settings.json"
    custom.write_text('{"models": {"omObservationThreshold": 800000}}\n')
    env = Environment()
    asyncio.run(MastraCode(logs_dir=tmp_path, settings_file=str(custom)).run("hi", env, AgentContext()))
    assert env.files["/logs/agent/settings.json"] == custom.read_bytes()
    env = Environment()
    asyncio.run(MastraCode(logs_dir=tmp_path).run("hi", env, AgentContext()))
    assert b'"omObservationThreshold": 120000' in env.files["/logs/agent/settings.json"]
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ValueError):
        MastraCode(logs_dir=tmp_path, settings_file=str(bad))
