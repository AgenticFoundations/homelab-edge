import stat
from pathlib import Path

import pytest
import yaml

from homelab_edge import cli

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config(tmp_path, monkeypatch):
    (tmp_path / ".env.example").write_text((ROOT / ".env.example").read_text())
    cli.setup(tmp_path)
    for key in cli.Config.load(tmp_path).values:
        monkeypatch.delenv(key, raising=False)
    return cli.Config.load(tmp_path)


def test_setup_is_private_and_preserves_credentials(config):
    path = config.root / ".env"
    path.write_text(
        path.read_text().replace("CLOUDFLARE_TUNNEL_TOKEN=", "CLOUDFLARE_TUNNEL_TOKEN=test")
    )
    before = path.read_bytes()
    cli.setup(config.root)
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "key,value",
    [
        ("METRICS_PORT", "80"),
        ("EDGE_NETWORK", "invalid network"),
        ("STARTUP_TIMEOUT", "0"),
        ("CLOUDFLARED_IMAGE", "cloudflare/cloudflared:latest"),
    ],
)
def test_invalid_config_rejected_before_commands(config, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(cli.AppError):
        cli.Config.load(config.root)


def test_missing_token_never_starts_containers(config, monkeypatch):
    monkeypatch.setattr(cli.Edge, "compose", lambda *_a, **_kw: pytest.fail("must not run Compose"))
    with pytest.raises(cli.AppError, match="CLOUDFLARE_TUNNEL_TOKEN"):
        cli.Edge(config).up()


def test_persistent_network_is_idempotent_and_shutdown_scoped(config, monkeypatch):
    calls = []

    def run(_self, args, **_kwargs):
        calls.append(args)
        if args[-2:] == ["inspect", "homelab-edge"] and len(calls) == 1:
            raise cli.AppError("network absent")
        return ""

    monkeypatch.setattr(cli.Edge, "run", run)
    edge = cli.Edge(config)
    edge.network()
    edge.network()
    edge.down()
    assert sum("create" in command for command in calls) == 1
    assert not any("rm" in command for command in calls)
    assert "homelab-edge" in calls[-1]
    assert "llm-inference" not in calls[-1]
    assert "--volumes" not in calls[-1]


def test_startup_checks_connection_after_compose(config, monkeypatch):
    config.values["CLOUDFLARE_TUNNEL_TOKEN"] = "private-token"
    events = []
    monkeypatch.setattr(cli.Edge, "doctor", lambda _: events.append("doctor"))
    monkeypatch.setattr(cli.Edge, "network", lambda _: events.append("network"))
    monkeypatch.setattr(cli.Edge, "compose", lambda *_a, **_kw: events.append("compose"))
    monkeypatch.setattr(cli.Edge, "connected", lambda _: events.append("connection") or True)
    cli.Edge(config).up()
    assert events == ["doctor", "network", "compose", "connection"]
    assert "private-token" not in repr(config)


def test_explicit_orbstack_context_overrides_external_daemon(config, monkeypatch):
    monkeypatch.setenv("DOCKER_CONTEXT", "production")
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote:2375")
    env = cli.Edge(config).env
    assert env["DOCKER_CONTEXT"] == "orbstack"
    assert "DOCKER_HOST" not in env


def test_compose_is_independent_and_shared_network_is_external():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert set(compose["services"]) == {"tunnel"}
    tunnel = compose["services"]["tunnel"]
    assert tunnel["restart"] == "unless-stopped"
    assert "depends_on" not in tunnel
    assert tunnel["ports"][0].startswith("127.0.0.1:")
    assert compose["networks"]["ingress"]["external"] is True


@pytest.mark.parametrize("operation", ["setup", "load"])
def test_existing_env_permissions_hardened(config, operation):
    path = config.root / ".env"
    before = path.read_bytes()
    path.chmod(0o644)
    if operation == "setup":
        cli.setup(config.root)
    else:
        cli.Config.load(config.root)
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_setup_handles_concurrent_creator(config, monkeypatch):
    path = config.root / ".env"
    path.unlink()
    original_open = cli.os.open

    def concurrent_open(*args):
        path.write_text("CLOUDFLARE_TUNNEL_TOKEN=keep-me\n")
        return original_open(*args)

    monkeypatch.setattr(cli.os, "open", concurrent_open)
    cli.setup(config.root)
    assert path.read_text() == "CLOUDFLARE_TUNNEL_TOKEN=keep-me\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("command", ["up", "restart"])
def test_readiness_wait_allows_down(config, monkeypatch, command):
    config.values["CLOUDFLARE_TUNNEL_TOKEN"] = "test-token"
    monkeypatch.setattr(cli.Config, "load", lambda _: config)
    events = []

    def start(_self):
        with pytest.raises(cli.AppError, match="lifecycle"):
            with cli.lifecycle_lock(config.root):
                pass
        events.append("start")

    def wait(_self):
        cli.dispatch(config.root, "down")
        events.append("wait")

    monkeypatch.setattr(cli.Edge, "start", start)
    monkeypatch.setattr(cli.Edge, "wait_until_connected", wait)
    monkeypatch.setattr(cli.Edge, "down", lambda _: events.append("down"))
    cli.dispatch(config.root, command)
    assert events[-3:] == ["start", "down", "wait"]


@pytest.mark.parametrize("capture", [True, False])
def test_command_failure_has_bounded_redacted_stderr(config, monkeypatch, capture):
    config.values["CLOUDFLARE_TUNNEL_TOKEN"] = "private-token"
    stderr = "old detail\n" * 10 + "Cannot connect private-token\n" + "x" * 2000
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_a, **_kw: cli.subprocess.CompletedProcess([], 1, "", stderr),
    )
    with pytest.raises(cli.AppError) as failure:
        cli.Edge(config).run(["docker", "info"], capture=capture)
    assert "private-token" not in str(failure.value)
    assert len(str(failure.value)) < 1600


def test_daemon_failure_detail_is_preserved(config, monkeypatch):
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_a, **_kw: cli.subprocess.CompletedProcess(
            [], 1, "", "Cannot connect to the Docker daemon"
        ),
    )
    with pytest.raises(cli.AppError, match="Cannot connect to the Docker daemon"):
        cli.Edge(config).doctor()
