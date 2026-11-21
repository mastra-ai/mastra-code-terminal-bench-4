import asyncio
import shutil
import subprocess

import pytest

from harbor.environments.daytona.environment import DaytonaEnvironment
from mastra_harbor.daytona_env import DaytonaImageUser, as_image_user, dockerfile_user


@pytest.mark.parametrize(
    ("text", "user"),
    [
        ("FROM ubuntu\nRUN useradd agent\nUSER agent\n", "agent"),
        ("FROM ubuntu\nUSER agent:agent\n", "agent"),
        ("FROM ubuntu\nUSER 1000\n", "1000"),
        ("FROM ubuntu\nUSER agent\nUSER root\n", None),
        ("FROM ubuntu\nUSER 0\n", None),
        ("FROM ubuntu AS build\nUSER agent\nFROM ubuntu\nRUN true\n", None),
        ("FROM ubuntu\nUSER ${APP_USER}\n", None),
        ("FROM ubuntu\nRUN true\n", None),
    ],
)
def test_dockerfile_user(tmp_path, text, user):
    path = tmp_path / "Dockerfile"
    path.write_text(text)
    assert dockerfile_user(path) == user


def test_dockerfile_user_missing(tmp_path):
    assert dockerfile_user(tmp_path / "Dockerfile") is None


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker")
def test_as_image_user_keeps_environment_and_quoting():
    # As root in a throwaway container, like the sandbox: the image user, the image PATH and env kept.
    command = as_image_user("agent", "echo \"$MARK\" 'it''s' $(id -un) $PATH $(pwd)", "bash -c")
    setup = "useradd -m agent && mkdir -p /app && cd /app && "
    out = subprocess.run(
        ["docker", "run", "--rm", "-e", "MARK=kept", "-e", "PATH=/opt/cuda/bin:/usr/sbin:/usr/bin:/bin",
         "ubuntu:22.04", "sh", "-c", setup + command],
        capture_output=True, text=True, timeout=300,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["kept", "its", "agent", "/opt/cuda/bin:/usr/sbin:/usr/bin:/bin", "/app"]


def _env(image_user):
    env = DaytonaImageUser.__new__(DaytonaImageUser)
    env._image_user = image_user
    return env


@pytest.mark.parametrize(
    ("image_user", "user", "expect_user", "wrapped"),
    [
        ("agent", None, None, True),
        ("agent", "agent", None, True),
        ("agent", "root", None, False),
        ("agent", 0, None, False),
        ("agent", "postgres", "postgres", False),
        (None, None, None, False),
        (None, "root", "root", False),
    ],
)
def test_sandbox_exec_user_mapping(monkeypatch, image_user, user, expect_user, wrapped):
    seen = {}

    async def fake(self, command, cwd=None, env=None, timeout_sec=None, shell="bash -c", user=None):
        seen.update(command=command, shell=shell, user=user, cwd=cwd)

    monkeypatch.setattr(DaytonaEnvironment, "_sandbox_exec", fake)
    asyncio.run(_env(image_user)._sandbox_exec("make test", cwd="/app", user=user))
    assert seen["user"] == expect_user
    assert seen["cwd"] == "/app"
    assert seen["command"].startswith("setpriv ") is wrapped
    assert seen["shell"] == ("sh -c" if wrapped else "bash -c")


@pytest.mark.parametrize("method", ["_sdk_upload_dir", "_sdk_download_dir"])
def test_directory_transfers_run_as_root(monkeypatch, method):
    # tar unpack/pack must be able to create top-level dirs such as /tests and /solution.
    seen = []

    async def fake_exec(self, command, cwd=None, env=None, timeout_sec=None, shell="bash -c", user=None):
        seen.append((command, shell, user))

    async def fake_transfer(self, source, target):
        await self._sandbox_exec("mkdir -p /tests && tar -xzf /tmp/a.tar.gz -C /tests", shell="sh -c")

    monkeypatch.setattr(DaytonaEnvironment, "_sandbox_exec", fake_exec)
    monkeypatch.setattr(DaytonaEnvironment, method, fake_transfer)
    env = _env("agent")
    asyncio.run(getattr(env, method)("/src", "/tests"))
    asyncio.run(env._sandbox_exec("make test"))  # outside a transfer: back to the image user
    assert seen[0] == ("mkdir -p /tests && tar -xzf /tmp/a.tar.gz -C /tests", "sh -c", None)
    assert seen[1][0].startswith("setpriv ")
