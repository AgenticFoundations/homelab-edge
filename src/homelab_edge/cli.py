"""Small, independent controller for the shared tunnel and persistent network."""

import argparse
import fcntl
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from dotenv import dotenv_values

ORB_DOCKER = Path("/Applications/OrbStack.app/Contents/MacOS/xbin/docker")


class AppError(Exception):
    """An actionable error safe to display without exposing credentials."""


@dataclass
class Config:
    root: Path
    values: dict[str, str] = field(repr=False)

    @classmethod
    def load(cls, root: Path) -> "Config":
        if not (root / ".env").is_file():
            raise AppError("Run make setup to create .env first.")
        (root / ".env").chmod(0o600)
        defaults = dotenv_values(root / ".env.example", interpolate=False)
        supplied = dotenv_values(root / ".env", interpolate=False)
        values = {
            key: os.environ.get(key, supplied.get(key, default)) or ""
            for key, default in defaults.items()
        }
        if any(any(char in value for char in "\r\n\x00") for value in values.values()):
            raise AppError("Configuration values must each be a single line.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", values["EDGE_NETWORK"]):
            raise AppError("EDGE_NETWORK must be a Docker network name.")
        port = values["METRICS_PORT"]
        if not port.isdigit() or not 1024 <= int(port) <= 65535:
            raise AppError("METRICS_PORT must be between 1024 and 65535.")
        timeout = values["STARTUP_TIMEOUT"]
        if not timeout.isdigit() or int(timeout) < 1:
            raise AppError("STARTUP_TIMEOUT must be a positive integer.")
        image = values["CLOUDFLARED_IMAGE"]
        tag = image.rsplit("/", 1)[-1]
        if (
            ":" not in tag
            or any(char.isspace() for char in image)
            or tag.rsplit(":", 1)[-1] in ("latest", "main-latest", "main-stable")
        ):
            raise AppError("CLOUDFLARED_IMAGE must specify an explicit version or digest.")
        return cls(root, values)


def setup(root: Path) -> None:
    path = root / ".env"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        path.chmod(0o600)
        print("Existing .env preserved; permissions set to 0600.")
        return
    with os.fdopen(fd, "w") as handle:
        handle.write((root / ".env.example").read_text())
    print("Created private .env. Add the tunnel token before make up. Nothing started.")


def docker_binary() -> str:
    binary = str(ORB_DOCKER) if ORB_DOCKER.is_file() else shutil.which("docker")
    if not binary:
        raise AppError("OrbStack's Docker CLI was not found. Install/open OrbStack first.")
    return binary


class Edge:
    def __init__(self, config: Config):
        self.config = config

    @property
    def env(self) -> dict[str, str]:
        values = dict(os.environ)
        for key in ("DOCKER_HOST", "COMPOSE_FILE", "COMPOSE_PROFILES"):
            values.pop(key, None)
        values.update(self.config.values)
        values["DOCKER_CONTEXT"] = "orbstack"
        values["PATH"] = str(Path(docker_binary()).parent) + os.pathsep + values.get("PATH", "")
        return values

    def diagnostic(self, detail: str) -> str:
        token = self.config.values["CLOUDFLARE_TUNNEL_TOKEN"]
        if token:
            detail = detail.replace(token, "[REDACTED]")
        detail = re.sub(r"eyJ[A-Za-z0-9_.=-]+", "[REDACTED]", detail)
        detail = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", detail)
        return "\n".join(detail.strip().splitlines()[-6:])[-1500:]

    def run(self, args: list[str], *, capture: bool = True, timeout: float | None = 30) -> str:
        try:
            result = subprocess.run(
                args,
                env=self.env,
                stdout=subprocess.PIPE if capture else None,
                stderr=None if not capture and timeout is None else subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = exc.stderr or ""
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            detail = self.diagnostic(stderr)
            message = "OrbStack command timed out. Check that OrbStack is available."
            raise AppError(f"{message}\n{detail}" if detail else message) from exc
        except OSError as exc:
            detail = self.diagnostic(str(exc))
            raise AppError(f"Could not run OrbStack command.\n{detail}") from exc
        if result.returncode:
            detail = self.diagnostic(result.stderr or "")
            message = "OrbStack command failed. Check make doctor or make logs."
            raise AppError(f"{message}\n{detail}" if detail else message)
        return result.stdout or ""

    def compose(self, *args: str, capture: bool = True, timeout: float | None = 30) -> str:
        standalone = Path(docker_binary()).with_name("docker-compose")
        prefix = (
            [str(standalone)]
            if standalone.is_file()
            else [docker_binary(), "--context", "orbstack", "compose"]
        )
        return self.run(
            prefix
            + [
                "--project-name",
                "homelab-edge",
                "--project-directory",
                str(self.config.root),
                "--env-file",
                "/dev/null",
                "-f",
                str(self.config.root / "compose.yaml"),
                *args,
            ],
            capture=capture,
            timeout=timeout,
        )

    def doctor(self) -> None:
        try:
            self.run([docker_binary(), "context", "inspect", "orbstack"])
        except AppError as exc:
            raise AppError(
                f"Open OrbStack once to create its orbstack Docker context.\n{exc}"
            ) from exc
        self.run([docker_binary(), "--context", "orbstack", "info"])
        self.compose("version")
        self.compose("config", "--quiet")
        state = "configured" if self.config.values["CLOUDFLARE_TUNNEL_TOKEN"] else "not configured"
        print(f"OrbStack and Compose available. Tunnel token: {state}.")

    def network(self) -> None:
        name = self.config.values["EDGE_NETWORK"]
        prefix = [docker_binary(), "--context", "orbstack", "network"]
        try:
            self.run(prefix + ["inspect", name])
        except AppError:
            try:
                self.run(prefix + ["create", "--driver", "bridge", name])
            except AppError:
                self.run(prefix + ["inspect", name])  # Concurrent application startup is safe.
        print(f"Shared network ready: {name}")

    def connected(self) -> bool:
        url = f"http://127.0.0.1:{self.config.values['METRICS_PORT']}/ready"
        try:
            with build_opener(ProxyHandler({})).open(url, timeout=2) as response:
                return bool(response.status == 200)
        except (URLError, OSError):
            return False

    def require_token(self) -> None:
        if not self.config.values["CLOUDFLARE_TUNNEL_TOKEN"]:
            raise AppError("Set CLOUDFLARE_TUNNEL_TOKEN in this project's .env before make up.")

    def up(self) -> None:
        with lifecycle_lock(self.config.root):
            self.start()
        self.wait_until_connected()

    def start(self) -> None:
        self.require_token()
        self.doctor()
        self.network()
        self.compose("up", "-d", "tunnel", capture=False, timeout=600)

    def wait_until_connected(self) -> None:
        print("Waiting for Cloudflare connection; make down is available to stop the tunnel.")
        deadline = time.monotonic() + int(self.config.values["STARTUP_TIMEOUT"])
        while not self.connected():
            if time.monotonic() >= deadline:
                raise AppError(
                    "Tunnel is not connected yet. Inspect make logs; stop with make down."
                )
            time.sleep(0.5)
        print("Shared tunnel connected. Configure hostname routes in Cloudflare.")

    def down(self) -> None:
        self.compose("down", "--timeout", "10", "--remove-orphans", capture=False, timeout=90)
        print("Shared tunnel stopped. Application containers and shared network are preserved.")


@contextmanager
def lifecycle_lock(root: Path) -> Iterator[None]:
    directory = root / ".runtime"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / "lifecycle.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AppError("Another infrastructure lifecycle command is running.") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def dispatch(root: Path, command: str) -> None:
    if command == "setup":
        setup(root)
        return
    edge = Edge(Config.load(root))
    match command:
        case "doctor" | "up":
            getattr(edge, command)()
        case "network" | "down":
            with lifecycle_lock(root):
                getattr(edge, command)()
        case "restart":
            edge.require_token()  # Reject missing credentials before interrupting other apps.
            with lifecycle_lock(root):
                edge.down()
                edge.start()
            edge.wait_until_connected()
        case "status":
            edge.compose("ps", "--all", capture=False)
        case "logs":
            edge.compose("logs", "--follow", "--tail", "100", capture=False, timeout=None)
        case "check":
            if not edge.connected():
                raise AppError("Tunnel has no active Cloudflare connection. Inspect make logs.")
            print("Tunnel has an active Cloudflare connection.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Shared Cloudflare Tunnel on OrbStack")
    parser.add_argument(
        "command",
        choices=[
            "setup",
            "doctor",
            "network",
            "up",
            "down",
            "restart",
            "status",
            "logs",
            "check",
        ],
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    try:
        dispatch(root, args.command)
    except AppError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Use make down if you want to stop the tunnel.", file=sys.stderr)
        return 130
    return 0
