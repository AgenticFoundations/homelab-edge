# homelab-edge

Shared Cloudflare Tunnel and networking for your applications on OrbStack.
This is an independent Python/uv project with its own Makefile. It never starts
or stops application containers, databases, or models.

## Setup

Install `uv` and open OrbStack once to finish its setup. The controller discovers
OrbStack's bundled Docker/Compose CLI and selects the `orbstack` context for its
commands without changing the global Docker context.

```bash
make setup       # Install locked dependencies; create private .env; starts nothing
# Put your remotely managed Cloudflare Tunnel token in .env.
make doctor
make up
make check
```

Create the tunnel in Cloudflare and copy its token into `CLOUDFLARE_TUNNEL_TOKEN`.
The token belongs only to this project. Published hostname routes are configured
in Cloudflare; changing this project's `.env` does not create DNS records or routes.
The token is passed as an environment variable, never a process argument.

`make setup` preserves existing `.env` contents. Setup and configuration loading
restrict its permissions to `0600`. Configuration precedence is shell environment,
then project `.env`, then `.env.example` defaults; unset shell overrides when you
want the project file to control a value.

`make up` runs doctor validation, creates the shared network if absent and waits for the connector's
`/ready` endpoint to confirm a connection to Cloudflare. This checks the connector;
use each application's own checks to verify its hostname, authentication, and API.
Image downloads can occur on first startup. A connection failure leaves the
container available for inspection using `make logs`; `make down` stops it, even
while startup is polling for readiness. Lifecycle mutations remain serialized.

## Commands

| Command | Purpose |
| --- | --- |
| `make setup` | Install dependencies and create `.env` once |
| `make doctor` | Validate configuration and OrbStack availability |
| `make network` | Initialize the shared network without starting the tunnel |
| `make up` | Start the tunnel and wait for a Cloudflare connection |
| `make down` | Stop the tunnel, preserving the shared network and all apps |
| `make restart` | Restart the tunnel using current configuration |
| `make status` | Show the tunnel container's state |
| `make logs` | Follow tunnel logs |
| `make check` | Check for an active Cloudflare connection |
| `make test` | Run offline tests |
| `make lint` | Run Ruff checks |
| `make typecheck` | Run strict mypy checks on application source |

The tunnel has `restart: unless-stopped`, so it resumes when the Docker engine starts
unless it was intentionally stopped. OrbStack startup is managed separately.
You can leave it running while hosted applications need it. Stopping the shared
tunnel makes all its published applications unavailable remotely; their local
containers continue running. Stopping one application leaves other routes working.
OrbStack itself is left running because other workloads may use it.

## Connect llm-inference

`llm-inference` joins the external `homelab-edge` network through its Nginx ingress
using the unique alias `llm-edge`. In the Cloudflare Tunnel dashboard, publish:

| Public hostname | Service |
| --- | --- |
| `llm.rohithkankipati.com` | `http://llm-edge:8080` |

Start that application independently:

```bash
make -C ../llm-inference up
make -C ../llm-inference down
```

Its `make down` releases model memory and removes its application containers.
It never stops this shared tunnel or removes the external network. Requests to a
stopped application fail as an unavailable origin and cannot wake its model.
The LLM retains API-key validation and an Nginx allowlist; its database, model,
and administrative gateway are not joined to the shared network.

## Add another application

Attach its ingress to the same external network with a unique alias:

```yaml
services:
  web:
    # Keep the rest of the application's configuration.
    networks:
      ingress:
        aliases: [notes-web]

networks:
  ingress:
    external: true
    name: homelab-edge
```

Then add a published hostname route, for example `notes.rohithkankipati.com` to
`http://notes-web:8080` (use that application's actual listening port).
Each application owns its ingress, authentication, and private networks. Only
ingresses that need routing join the shared network. No additional reverse proxy
is required just to publish multiple hostnames through Cloudflare.

`EDGE_NETWORK` must match across projects. Metrics are bound to loopback on
`METRICS_PORT` (default 42000). The external network is intentionally persistent;
neither project's shutdown removes it. `.env` is private and ignored by Git.

References: [Cloudflare routing](https://developers.cloudflare.com/tunnel/concepts/routing/),
[Compose external networks](https://docs.docker.com/compose/how-tos/networking/#use-an-existing-network),
[OrbStack networking](https://docs.orbstack.dev/docker/network).
