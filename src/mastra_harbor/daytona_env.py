"""Daytona environment that behaves like Docker for images whose Dockerfile ends in a non-root USER.

Stock Harbor creates the sandbox as the image's USER and reaches root with ``su root``, which has no
password to give, so root commands (agent install, verifier setup) hang or fail (harbor#1543).
``os_user="root"`` is ignored by Daytona for such images, so this class appends ``USER root`` to the
image and runs default-user commands as the image's user through ``setpriv``. Unlike ``su``, setpriv
keeps the image environment (PATH, HOME, CUDA vars), matching ``docker exec`` without ``-u``.
Directory transfers (tar unpack/pack around SDK uploads and downloads) run as root, as they do for
root images on Daytona and as ``docker cp`` does locally; as the image user, unpacking into a new
top-level directory such as /tests or /solution fails.
Images without a non-root USER, compose tasks and Windows tasks are left exactly as stock Harbor.
"""

from __future__ import annotations

import contextvars
import re
import shlex
from pathlib import Path

from harbor.environments.daytona.environment import DaytonaEnvironment

ROOT_USERS = {"root", "0"}
_AS_ROOT = contextvars.ContextVar("daytona_transfer_as_root", default=False)


def dockerfile_user(dockerfile: Path) -> str | None:
    """The final stage's last USER, or None when absent, root, or not a literal name/uid."""
    if not dockerfile.is_file():
        return None
    user = None
    for line in dockerfile.read_text().splitlines():
        words = line.strip().split()
        if not words:
            continue
        keyword = words[0].upper()
        if keyword == "FROM":
            user = None
        elif keyword == "USER" and len(words) > 1:
            user = words[1].split(":", 1)[0]
    if user is None or user in ROOT_USERS or not re.fullmatch(r"[A-Za-z0-9_.-]+", user):
        return None
    return user


def as_image_user(user: str, command: str, shell: str) -> str:
    ids = f"$(id -u {shlex.quote(user)})"
    return (f"setpriv --reuid={ids} --regid=$(id -g {shlex.quote(user)}) --init-groups -- "
            f"{shell} {shlex.quote(command)}")


class DaytonaImageUser(DaytonaEnvironment):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._image_user = (
            None if self._compose_mode or self._is_windows else dockerfile_user(self._dockerfile_path)
        )
        if self._image_user:
            self.logger.info(
                "Image USER %s: sandbox runs as root, default commands run as %s via setpriv",
                self._image_user, self._image_user,
            )

    def _image_sandbox_params(self, *, image, resources, network):
        if self._image_user:
            image = image.dockerfile_commands(["USER root"])
        return super()._image_sandbox_params(image=image, resources=resources, network=network)

    async def _sdk_upload_dir(self, source_dir, target_dir):
        token = _AS_ROOT.set(True)
        try:
            return await super()._sdk_upload_dir(source_dir, target_dir)
        finally:
            _AS_ROOT.reset(token)

    async def _sdk_download_dir(self, source_dir, target_dir):
        token = _AS_ROOT.set(True)
        try:
            return await super()._sdk_download_dir(source_dir, target_dir)
        finally:
            _AS_ROOT.reset(token)

    async def _sandbox_exec(self, command, cwd=None, env=None, timeout_sec=None, shell="bash -c", user=None):
        if self._image_user:
            if _AS_ROOT.get() and user is None:
                user = None  # transfer plumbing: the sandbox's own root, like docker cp
            elif user is None or str(user) == self._image_user:
                command, shell, user = as_image_user(self._image_user, command, shell), "sh -c", None
            elif str(user) in ROOT_USERS:
                user = None  # the sandbox already runs as root; su would reset PATH
        return await super()._sandbox_exec(
            command, cwd=cwd, env=env, timeout_sec=timeout_sec, shell=shell, user=user,
        )
