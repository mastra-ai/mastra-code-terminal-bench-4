"""Run the unmodified Mastra Code CLI inside a Harbor task environment."""

import hashlib
import json
import os
import re
import shlex
import time
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from harbor.agents.installed.base import BaseInstalledAgent
from harbor.agents.capabilities import AgentCapabilities
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from .reporting import export_report

MASTRACODE_VERSION = "0.44.1"
NODE_VERSION = "22.22.0"
PREFIX = "/opt/mastra-harbor"
THINKING_LEVELS = ("off", "low", "medium", "high", "xhigh", "max")
# Lists each configured MCP server's tools through Mastra's own client; exits non-zero if any has none.
MCP_PREFLIGHT = """
import { createRequire } from "node:module";
const req = createRequire("/opt/mastra-harbor/node_modules/mastracode/package.json");
// require.resolve lands on the CJS build, whose named exports arrive under `default` when imported.
const mcp = await import(req.resolve("@mastra/mcp"));
const MCPClient = mcp.MCPClient ?? mcp.default?.MCPClient;
const servers = JSON.parse(process.env.MC_MCP_SERVERS).mcpServers;
let bad = 0;
for (const [name, cfg] of Object.entries(servers)) {
  const client = new MCPClient({ servers: { [name]: cfg.url ? { url: new URL(cfg.url) } : cfg }, timeout: 60000 });
  let count = 0;
  try { count = Object.keys(await client.listTools()).length; } catch {}
  await client.disconnect().catch(() => {});
  console.log(JSON.stringify({ server: name, tools: count }));
  if (!count) bad++;
}
process.exit(bad ? 3 : 0);
"""


def mastra_mcp_config(servers) -> tuple[dict, list[dict]]:
    """Harbor MCP servers -> Mastra Code mcp.json. Mastra's MCP client speaks Streamable HTTP only,
    so an SSE server is registered at its /mcp endpoint (same server, same tools)."""
    config, notes = {}, []
    for server in servers:
        if server.transport == "stdio":
            config[server.name] = {"command": server.command, "args": list(server.args)}
            continue
        url = server.url
        if server.transport == "sse" and url.rstrip("/").endswith("/sse"):
            url = url.rstrip("/")[: -len("/sse")] + "/mcp"
            notes.append({"server": server.name, "declared": server.url, "registered": url,
                          "reason": "Mastra MCP client has no SSE transport; using the server's Streamable HTTP endpoint"})
        config[server.name] = {"url": url}
    return {"mcpServers": config}, notes


class MastraCode(BaseInstalledAgent):
    """Stock headless behavior, including native observational memory/recall."""

    capabilities = AgentCapabilities(atif=True)

    def __init__(self, *args, credential_env: str = "", benchmark_version: str | None = None, version=None,
                 bundle: str | None = None, bundle_sha256: str | None = None,
                 thinking_level: str | None = None, oauth_auth_file: str | None = None,
                 oauth_min_valid_hours: float = 9.0, usage_tap: bool | str = False,
                 settings_file: str | None = None, **kwargs):
        # Optional prebuilt snapshot: a tar of one self-contained mastracode package
        # directory, plus a sibling <bundle>.json manifest recording its source.
        self.bundle = self.bundle_manifest = None
        pinned = MASTRACODE_VERSION
        if bundle:
            if not bundle_sha256 or not re.fullmatch(r"[0-9a-f]{64}", bundle_sha256):
                raise ValueError("bundle requires its expected lowercase hex bundle_sha256")
            self.bundle, self.bundle_sha256 = Path(bundle), bundle_sha256
            with self.bundle.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != bundle_sha256:
                    raise ValueError("bundle does not match bundle_sha256")
            self.bundle_manifest = json.loads(self.bundle.with_suffix(".json").read_text())
            if self.bundle_manifest.get("sha256") != bundle_sha256:
                raise ValueError("bundle manifest does not match bundle_sha256")
            pinned = self.bundle_manifest["version"]
        elif bundle_sha256:
            raise ValueError("bundle_sha256 requires bundle")
        if version not in (None, pinned):
            raise ValueError(f"This adapter pins mastracode {pinned}")
        if thinking_level not in (None, *THINKING_LEVELS):
            raise ValueError(f"thinking_level must be one of {', '.join(THINKING_LEVELS)}")
        self.thinking_level = thinking_level
        # Optional Mastra auth.json holding a subscription OAuth login (e.g. ChatGPT
        # via openai-codex). Trials get access tokens only, never refresh tokens.
        self.oauth_auth_file = Path(oauth_auth_file) if oauth_auth_file else None
        self.oauth_min_valid_hours = oauth_min_valid_hours
        # Optional measurement-only preload that logs provider-reported usage for every model call.
        self.usage_tap = str(usage_tap).lower() in ("1", "true", "yes")
        # Optional Mastra Code settings.json on the host, replacing the packaged default (harness settings
        # such as OM thresholds or subagent models; recorded in the trial logs as settings.json).
        self.settings_text = None
        if settings_file:
            self.settings_text = Path(settings_file).read_text()
            json.loads(self.settings_text)
        super().__init__(*args, version=pinned, **kwargs)
        self.benchmark_version = benchmark_version
        if self.extra_env:
            raise ValueError("Pass credential variable names via credential_env, not values")
        self.credential_names = tuple(filter(None, credential_env.split(",")))
        if any(not re.fullmatch(r"[A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|ACCESS_KEY_ID|SECRET_ACCESS_KEY|REGION)", name)
               for name in self.credential_names):
            raise ValueError("credential_env must contain provider credential variable names")

    @staticmethod
    def name() -> str:
        return "mastracode"

    async def install(self, environment: BaseEnvironment) -> None:
        await self.ensure_system_dependencies(
            environment, ("bash", "curl", "ca_certificates", "tar", "xz", "git", "ripgrep"),
        )
        await self.exec_as_root(environment, f"mkdir -p {PREFIX}")
        await self._upload_config_text(
            environment,
            content=files("mastra_harbor").joinpath("assets/install.sh").read_text(),
            remote_path=f"{PREFIX}/install.sh", filename="install.sh",
        )
        if self.usage_tap:
            await self._upload_config_text(
                environment, content=files("mastra_harbor").joinpath("assets/usage-tap.mjs").read_text(),
                remote_path=f"{PREFIX}/usage-tap.mjs", filename="usage-tap.mjs",
            )
        log = shlex.quote(str(self.environment_logs_dir / "install.log"))
        if self.bundle:
            await environment.upload_file(self.bundle, f"{PREFIX}/mastracode-bundle.tar")
            await self._upload_config_text(
                environment, content=json.dumps(self.bundle_manifest, indent=2) + "\n",
                remote_path=str(self.environment_logs_dir / "bundle.json"), filename="bundle.json",
            )
            await self.exec_as_root(
                environment, f"MC_BUNDLE_SHA256={self.bundle_sha256} bash {PREFIX}/install.sh > {log} 2>&1",
            )
            lock = f"{PREFIX}/node_modules/mastracode/pnpm-lock.yaml"
            copied = "pnpm-lock.yaml"
        else:
            await self.exec_as_root(environment, f"bash {PREFIX}/install.sh > {log} 2>&1")
            lock, copied = f"{PREFIX}/package-lock.json", "package-lock.json"
        await self.exec_as_root(
            environment, f"cp {lock} {shlex.quote(str(self.environment_logs_dir / copied))}",
        )

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        credentials = {}
        for name in self.credential_names:
            value = os.environ.get(name)
            if not value:
                raise ValueError(f"Missing provider credential: {name}")
            credentials[name] = value

        auth_json = self._trial_auth_json() if self.oauth_auth_file else None
        run_id = uuid4().hex
        state = str(self.environment_logs_dir / "state" / run_id)
        prompt = str(self.environment_logs_dir / "prompt.txt")
        settings = str(self.environment_logs_dir / "settings.json")
        stdout = str(self.environment_logs_dir / "mastracode.jsonl")
        stderr = str(self.environment_logs_dir / "mastracode.stderr.log")
        q = shlex.quote
        # Limit the private mask to log creation; task commands keep their native mask.
        await self.exec_as_agent(environment,
            f"umask 077; mkdir -p {q(state)}; : > {q(stdout)}; : > {q(stderr)}")
        await self._upload_config_text(
            environment, content=instruction, remote_path=prompt, filename="prompt.txt",
        )
        await self._upload_config_text(
            environment,
            content=self.settings_text or files("mastra_harbor").joinpath("assets/settings.json").read_text(),
            remote_path=settings, filename="settings.json",
        )
        private = [prompt, settings]
        # Credentials travel as a private file, never inside the command string, so
        # sandbox providers that record exec commands (e.g. Daytona sessions) never see them.
        secrets = f"{state}/credentials.env"
        if credentials:
            await self._upload_config_text(
                environment, remote_path=secrets, filename="credentials.env",
                content="".join(f"export {name}={q(value)}\n" for name, value in credentials.items()),
            )
            private.append(secrets)
        if auth_json:
            auth = f"{state}/auth.json"
            await self._upload_config_text(environment, content=auth_json, remote_path=auth, filename="auth.json")
            private.append(auth)
        # Harbor's upload helper cannot infer an image's implicit USER.
        if environment.default_user is None:
            identity = await self.exec_as_agent(environment, "id -u; id -g")
            ids = (identity.stdout or "").split()
            if len(ids) != 2 or not all(re.fullmatch(r"[0-9]+", value) for value in ids):
                raise RuntimeError("Could not resolve native agent identity")
            paths = " ".join(q(path) for path in private)
            await self.exec_as_root(
                environment, f"chown {ids[0]}:{ids[1]} {paths} && chmod 600 {paths}",
            )
        tap = ["--import", f"{PREFIX}/usage-tap.mjs"] if self.usage_tap else []
        if self.mcp_servers:
            await self._register_mcp_servers(environment)
        args = [
            f"{PREFIX}/node/bin/node", *tap, f"{PREFIX}/node_modules/mastracode/dist/cli.js",
            "--prompt", "-", "--output", "jsonl", "--resource-id", run_id,
            "--settings", settings,
        ]
        if self.model_name:
            args.extend(("--model", self.model_name))
        if self.thinking_level:
            args.extend(("--thinking-level", self.thinking_level))
        # The CLI uses its own Node; task commands keep their original PATH priority.
        # Load and delete the credentials file before the agent starts.
        load = f"set -a; . {q(secrets)}; set +a; rm -f {q(secrets)}; " if credentials else ""
        command = (
            f"{load}"
            f'export PATH="$PATH":{q(PREFIX + "/node/bin")}; '
            f"{shlex.join(args)} < {q(prompt)} "
            f"> {q(stdout)} 2> {q(stderr)}"
        )
        try:
            await self.exec_as_agent(
                environment, command,
                env={"MASTRA_APP_DATA_DIR": state,
                     **({"MASTRA_USAGE_TAP_FILE": str(self.environment_logs_dir / "provider-usage.jsonl")}
                        if self.usage_tap else {})},
            )
        finally:
            if credentials:
                await self.exec_as_root(environment, f"rm -f {q(secrets)}")
            if auth_json:
                # The access token must not outlive the trial in its downloaded logs.
                await self.exec_as_root(environment, f"rm -f {q(auth)}")

    async def _register_mcp_servers(self, environment: BaseEnvironment) -> None:
        """Write the task's MCP servers to Mastra Code's global ~/.mastracode/mcp.json, then verify tools load."""
        config, notes = mastra_mcp_config(self.mcp_servers)
        text = json.dumps(config, indent=2) + "\n"
        home = (await self.exec_as_agent(environment, 'printf %s "$HOME"')).stdout.strip() or "/root"
        q = shlex.quote
        await self.exec_as_agent(environment, f"mkdir -p {q(home + '/.mastracode')}")
        target = f"{home}/.mastracode/mcp.json"
        await self._upload_config_text(environment, content=text, remote_path=target, filename="mcp.json")
        if environment.default_user is None:
            ids = (await self.exec_as_agent(environment, "id -u; id -g")).stdout.split()
            await self.exec_as_root(environment, f"chown {ids[0]}:{ids[1]} {q(target)}")
        # Provenance copy: server names/URLs only, plus any transport substitution.
        await self._upload_config_text(
            environment, content=json.dumps({"mcp_json": config, "transport_notes": notes}, indent=2) + "\n",
            remote_path=str(self.environment_logs_dir / "mcp-registration.json"), filename="mcp-registration.json",
        )
        try:
            await self.exec_as_agent(
                environment,
                f"{PREFIX}/node/bin/node --input-type=module -e {q(MCP_PREFLIGHT)}",
                env={"MC_MCP_SERVERS": text},
            )
        except Exception as error:
            raise RuntimeError("MCP preflight: a task MCP server exposed no tools to Mastra Code") from error

    def _trial_auth_json(self) -> str:
        """Copy of the host login with refresh tokens removed, checked for remaining validity."""
        data = json.loads(self.oauth_auth_file.read_text())
        deadline = (time.time() + self.oauth_min_valid_hours * 3600) * 1000
        oauth = [v for v in data.values() if isinstance(v, dict) and "access" in v]
        if not oauth:
            raise ValueError("oauth_auth_file contains no OAuth login")
        for entry in oauth:
            if not entry.get("access") or entry.get("expires", 0) < deadline:
                raise ValueError("OAuth access token expires too soon; refresh the host login")
            entry["refresh"] = ""
        return json.dumps(data)

    def populate_context_post_run(self, context: AgentContext) -> None:
        # Harbor invokes this on the host after downloading the native logs.
        try:
            export_report(self.logs_dir, context, version=self.version(),
                          model=self.model_name, benchmark_version=self.benchmark_version)
        except Exception as error:
            # Reporting failure must not replace the native task outcome.
            context.metadata = {**(context.metadata or {}), "mastracode_reporting_error": type(error).__name__}
