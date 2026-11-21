# Mastra Code on Terminal-Bench 4

Run [Mastra Code](https://code.mastra.ai) on Terminal-Bench 4.0 through Harbor, and rebuild the two Mastra Code
builds behind our published results.

Terminal-Bench 4.0 has 66 tasks. Each one gives an agent a container and a job, such as fixing a build or
migrating a live database. A script then checks the container, and the task passes or fails. This repo has the
Harbor adapter we ran, the patches and scripts that rebuild our two Mastra Code bundles, and the per-task results.

## Published results

Terminal-Bench 4.0, original 4.0 grader, maximum reasoning effort. Score is passed trials over all trials.

| Agent | Model | Score | Passed | Passes per run (of 66) |
| --- | --- | ---: | ---: | --- |
| Mastra Code, goal mode | Claude Opus 5.5 | 69.2% | 137/198 | 46, 45, 46 |
| Mastra Code, goal mode | GPT-6.1 Sol | 58.1% | 115/198 | 39, 39, 37 |
| Mastra Code, no goal mode | Claude Opus 5.5 | 63.6% | 42/66 | 42 |
| Mastra Code, no goal mode | GPT-6.1 Sol | 56.8% | 75/132 | 38, 37 |
| Claude Code (leaderboard) | Claude Opus 5.5 | 64.85% | 214/330 | 5 attempts per task |
| Codex (leaderboard) | GPT-6.1 Sol | 58.2% | 192/330 | 5 attempts per task |

The goal-mode rows are three runs of every task. The no-goal rows use the same bundles without the goal-mode
patch: one run for Opus, two for Sol. The leaderboard rows are the official Claude Code and Codex submissions.

[results/tb4-per-task.csv](results/tb4-per-task.csv) has the pass or fail of every trial, by task, with each
task's category and Harbor task digest. To recompute the table:

```sh
python3 results/headline.py                 # the table above
python3 results/headline.py --by-category   # the same, split by task category
```

## What we changed from stock Mastra Code

The bundles start from [mastra-ai/mastra](https://github.com/mastra-ai/mastra) `main` at
`f611832d661439f6b2aada795dcc16efdc2b12c3` (November 3, 2026). On top of that:

1. **Nine bug fixes.** We found these bugs while running Terminal-Bench and opened a pull request for each. The
   bundles apply each PR's diff at the head commit listed, without its changeset file.

   | PR | Head | Fix |
   | --- | --- | --- |
   | [#29461](https://github.com/mastra-ai/mastra/pull/29461) | `be40dce0` | Report upstream failures of `cmd \| tail` when /bin/sh lacks pipefail |
   | [#29462](https://github.com/mastra-ai/mastra/pull/29462) | `afaf1d84` | Retry stream cuts after HTTP 200 instead of declaring an outage |
   | [#29463](https://github.com/mastra-ai/mastra/pull/29463) | `da72efea` | Emit message_end when a turn ends in a tool suspension |
   | [#29464](https://github.com/mastra-ai/mastra/pull/29464) | `77e1f032` | Embed observations in a short-lived worker process |
   | [#29469](https://github.com/mastra-ai/mastra/pull/29469) | `1802bc96` | Detect media type from file contents in read_file |
   | [#29473](https://github.com/mastra-ai/mastra/pull/29473) | `b544fa1c` | Keep surrogate pairs whole when truncating tool output |
   | [#29474](https://github.com/mastra-ai/mastra/pull/29474) | `ed942dce` | Drop a tool-result file part the provider rejects and retry once |
   | [#29485](https://github.com/mastra-ai/mastra/pull/29485) | `d3782518` | Raise the OOM score of commands spawned by LocalSandbox |
   | [#29486](https://github.com/mastra-ai/mastra/pull/29486) | `4db9a483` | Run an explicit model when the model catalog is unavailable |

   Six of these have since been merged and three are still open. Later commits on those PRs are not in the
   bundles.

2. **A new "Verification is Required" section** in the build-mode system prompt. It replaces the stock section
   of the same name. The new text, in [bundle/dist/verification-section.md](bundle/dist/verification-section.md),
   tells the agent that the work is done when the request is met and shown to work, not when its plan is
   finished.

3. **Claude Code parity, Opus only.** Claude Code handles two Opus 5.5 stop reasons that stock Mastra Code does
   not. On a refusal it sends a fixed message, retries, and falls back to Claude Opus 5 through Anthropic's
   server-side fallback. On an output-length stop it asks the model to resume, up to three times. The Opus
   bundle does the same, with Claude Code's exact messages, so that the comparison is not about these two
   cases. See [bundle/dist/patch_opus_parity.py](bundle/dist/patch_opus_parity.py).

4. **Goal mode.** Mastra Code already has a goal judge: in the TUI, `/goal` sets an objective and a judge model
   checks each finished turn against it. Headless runs never set one. With this patch, a headless run makes the
   task prompt its goal and uses the run's own model as the judge. When the agent stops, the judge either accepts
   the result or sends the agent back with a reason, for at most three checks per run. The judge uses Mastra's
   stock judge prompt and read-only tools. It runs without extended thinking on Opus and at medium reasoning
   effort on Sol. See [bundle/dist/patch_goal_judge.py](bundle/dist/patch_goal_judge.py), and
   [bundle/patches/goal-judge-source.patch](bundle/patches/goal-judge-source.patch) for the same change against
   the TypeScript source, with tests.

Apart from these four changes, the bundles are stock Mastra Code at `f611832d`. Subagents stay off, which is
the headless default. Both models ran with Tavily web search (`TAVILY_API_KEY`) and `--thinking-level max`.

## Build the bundles

A bundle is a tar of a self-contained `mastracode` package directory, made with `pnpm deploy`. The adapter
uploads it into each task container. You need git, Node 22.19+ or 24.11+, pnpm 11.21.0, Python 3.11 or later
and GNU tar.

```sh
bundle/build.sh /tmp/tb4-build
```

This clones mastra-ai/mastra, checks out `f611832d`, applies the nine fix patches in order, builds Mastra Code and
deploys it. Then it applies the dist patches and writes two bundles, each with the `.json` manifest the
adapter reads:

| Bundle | Built from | Our sha256 |
| --- | --- | --- |
| `mastracode-tb4-sol.tar` | fixes, Verification section, goal mode | `008bf7c7728e03295b622b36e429f58bdaa5263ca699514f0cd4a93e16966ef5` |
| `mastracode-tb4-opus.tar` | fixes, Opus parity, Verification section, goal mode | `f40f8217db0250189646e272a8843ab5e9a4309d015eca5e448d3530b89d694b` |

With the pnpm store already filled, the build took about two minutes on our 16-core machine. Pass a clean
mastra checkout as a second argument to skip the clone. To build the no-goal bundles, remove `judge` from the
two `variant` lines in `build.sh`.

**Your tar hashes will not match ours.** `pnpm deploy` writes the workspace's absolute path into folder names
under `node_modules/.pnpm`, and the build embeds the pnpm store's path relative to the workspace in some
`@mastra/core` files. Both depend on where you build. Instead, `build.sh` ends by running
[bundle/check_files.py](bundle/check_files.py), which compares the `@mastra/code-sdk` files our patches touch
with the same files in our bundles. You can also run it on a tar:

```sh
python3 bundle/check_files.py opus /tmp/tb4-build/mastracode-tb4-opus.tar
```

We checked this on two rebuilds in different directories. All nine fix patches applied cleanly, and every file
`check_files.py` lists matched our bundles: four for Sol and five for Opus. The `@mastra/core` files the parity
patch edits can't be compared this way, because their names and contents carry the pnpm store path.

## Run it with Harbor

You need Python 3.12 or later and Docker. Daytona is optional.

```sh
git clone https://github.com/mastra-ai/mastra-code-terminal-bench-4.git
cd mastra-code-terminal-bench-4
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
pytest -q
```

Check that the adapter installs a bundle in a container, with no model calls:

```sh
harbor run --path examples/smoke --agent mastra_harbor.agent:MastraCode \
  --ak bundle=/tmp/tb4-build/mastracode-tb4-opus.tar \
  --ak bundle_sha256=<sha256 of that tar> \
  --install-only --n-attempts 1 --n-concurrent 1 --max-retries 0 --job-name mastra-install-check
```

The Opus configuration we ran, three attempts per task on Daytona:

```sh
harbor run \
  --dataset terminal-bench/terminal-bench@sha256:39d9f44b40420cde8fdcc087579c0d72a7e14fa3656d603c3f0d22fb35e27732 \
  --agent mastra_harbor.agent:MastraCode \
  --model anthropic/claude-opus-5-5 \
  --ak bundle=/tmp/tb4-build/mastracode-tb4-opus.tar \
  --ak bundle_sha256=<sha256 of that tar> \
  --ak settings_file=src/mastra_harbor/assets/settings-opus55-ccparity.json \
  --ak thinking_level=max \
  --ak oauth_auth_file=/path/to/auth.json \
  --ak credential_env=TAVILY_API_KEY \
  --ak benchmark_version=4.0.0 \
  --ak usage_tap=true \
  -e mastra_harbor.daytona_env:DaytonaImageUser --ek auto_stop_interval_mins=30 \
  --n-attempts 3 --n-concurrent 8 --max-retries 0 \
  --job-name mastra-tb4-opus
```

For Sol, use `--model openai/gpt-6.1-sol`, the Sol bundle, and drop `settings_file` so the packaged
[settings.json](src/mastra_harbor/assets/settings.json) applies. These commands make paid model calls.

| Option | What it does |
| --- | --- |
| `bundle`, `bundle_sha256` | The bundle tar and its sha256. The adapter refuses a tar whose hash or manifest does not match. Without `bundle`, it installs `mastracode@0.44.1` from npm, which is not what we benchmarked. |
| `settings_file` | A Mastra Code settings.json that replaces the packaged one. The packaged file sets the observational memory thresholds (120,000 and 40,000 tokens) and quiet mode. The Opus file adds `anthropic/claude-opus-5-5` as the subagent model. |
| `thinking_level` | Passed to the CLI as `--thinking-level`. We used `max`. |
| `oauth_auth_file` | A Mastra Code `auth.json` holding a subscription login (Claude or ChatGPT). Each trial gets a copy with the refresh tokens removed. The access token must stay valid for `oauth_min_valid_hours` (default 9). |
| `credential_env` | Names, never values, of environment variables to forward into the trial, for example `ANTHROPIC_API_KEY,TAVILY_API_KEY`. Use this with API keys in place of `oauth_auth_file`. |
| `benchmark_version` | A label recorded in each trial's provenance. It does not select the dataset. |
| `usage_tap` | Logs the provider-reported token usage of every model call to `provider-usage.jsonl`. It reads copies of requests and responses and never changes them. |

Drop the `-e` and `--ek` lines to run in local Docker. `DaytonaImageUser` is Harbor's Daytona environment with
one change: for images whose Dockerfile ends in a non-root `USER`, the sandbox runs as root and default commands
run as that user, which matches `docker exec`. Daytona needs `DAYTONA_API_KEY` in your shell. Harbor asks for
confirmation before some tasks start; `--yes` answers it.

Each trial's `agent/` folder has the CLI's JSONL output, `trajectory.json` (Harbor's ATIF format),
`usage.json`, `provenance.json` with the bundle manifest, and the settings and prompt the CLI received.

## Differences from the leaderboard runs

- **Logins.** We ran on Claude and ChatGPT subscription logins. The official runs used API keys.
- **Attempts.** We ran 3 attempts per task in goal mode, and 1 or 2 without it. The official runs used 5.
- **Infrastructure.** Our trials ran in Daytona sandboxes, not on the leaderboard's infrastructure. A trial that
  failed for an infrastructure reason before the grader ran, such as a sandbox outage or a grader timeout, got
  one clean retry. A few of those retries ran on a Docker host instead of Daytona. Agent failures always count
  as failures.
- **Grading.** Every number here, ours and the leaderboard's, uses the original Terminal-Bench 4.0 grader. We
  have not regraded with the 4.1 verifiers.
- **Patches.** The Opus bundle includes the Claude Code parity patch, and both bundles include fixes and a goal
  mode that stock Mastra Code does not have.

## Layout

```
src/mastra_harbor/   the Harbor adapter: agent, Daytona environment, log export
  assets/            install script, settings files, usage tap
tests/               adapter tests (pytest)
examples/smoke/      a one-file Harbor task for checking an install
bundle/build.sh      rebuilds both bundles from mastra f611832d
bundle/patches/      the nine fix PRs as diffs, plus the goal-mode change as a source patch
bundle/dist/         edits to the built dist: Verification section, Opus parity, goal mode
results/             per-task outcomes and the script that recomputes the table
```

## License

MIT.
