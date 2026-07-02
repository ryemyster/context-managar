# Deploying Context Engine to Fly.io

This runbook describes the recommended Fly.io architecture for Context Engine.
It deliberately keeps the MCP transport on the developer workstation and moves
only the REST engine to Fly.

```text
Codex / Claude
      |
      v
local MCP adapter (:8089)
      |
      | HTTPS + X-API-Key
      v
Fly.io REST engine (:8088)
      |-- Fly Volume: cloned repositories and artifact backup
      |-- OpenAI-compatible cloud API: generation
      |-- Ollama embedding endpoint: existing nomic vectors
      `-- Supabase: vector and artifact storage
```

Inference runs outside the Fly Machine. Use an OpenAI-compatible cloud endpoint
for generation and retain an Ollama endpoint for `nomic-embed-text` embeddings.

## Current Readiness

The inference boundary supports authenticated cloud generation and independent
embedding providers. Repository synchronization remains a deployment
prerequisite because Fly cannot mount the developer workstation's
   `~/Repos` directory.
Add `git` to the runtime image if repositories will be cloned or pulled
   inside the Machine.

Do not point the embedding provider at the cloud generation endpoint unless it
serves the same `nomic-embed-text` model and produces compatible
768-dimensional vectors.

## Recommended Model Configuration

Start with one cloud coding model for every generation role:

```env
INFERENCE_GENERATION_PROVIDER=openai_compatible
INFERENCE_GENERATION_ENDPOINT=https://provider.example/v1
INFERENCE_GENERATION_API_KEY=<stored as a Fly secret>
INFERENCE_FAST_MODEL=qwen3-coder-next
INFERENCE_REASONING_MODEL=qwen3-coder-next
INFERENCE_AGENT_MODEL=qwen3-coder-next
INFERENCE_SELECTION_MODEL=qwen3-coder-next
INFERENCE_VERIFICATION_MODEL=qwen3-coder-next
OLLAMA_NUM_CTX=32768
```

Keep the existing embedding corpus on Ollama:

```env
INFERENCE_EMBEDDING_PROVIDER=ollama
INFERENCE_EMBEDDING_ENDPOINT=https://your-ollama-embedding-host.example
INFERENCE_EMBEDDING_API_KEY=<stored as a Fly secret, if required>
INFERENCE_EMBEDDING_MODEL=nomic-embed-text
```

Use the exact generation model identifier returned by `GET /models`. The
generation provider must support OpenAI-compatible tool calls and JSON Schema
response formatting.

## Repository Storage and Synchronization

Context Engine reads files directly below `REPO_ROOT`. In Fly, mount a persistent
volume and use:

```env
REPO_ROOT=/data/repos
OUTPUT_DIR=/data/artifacts
```

The simplest synchronization strategy for a personal deployment is:

1. Create one Machine and one volume.
2. Clone the required repositories into `/data/repos/<owner>/<repo>`.
3. Pull them on startup and before investigations where freshness matters.
4. Authenticate private clones with a read-only GitHub deploy key or GitHub App.

Preserve the `owner/repo` directory shape because MCP requests require paths
relative to `REPO_ROOT`, for example:

```text
/data/repos/ryemyster/context-manager
```

Do not place repository credentials in the image or `fly.toml`. Store them as
Fly secrets. A single Fly Volume is appropriate for a personal service but is
not replicated automatically; the Git remotes should remain the source of truth.

## Prepare the Runtime Image

The current `context-engine/Dockerfile` is sufficient for the Python REST
process but does not contain `git`. Add it before enabling repository sync:

```dockerfile
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git openssh-client \
    && rm -rf /var/lib/apt/lists/*
```

Use an entrypoint that:

1. Creates `/data/repos` and `/data/artifacts`.
2. Clones missing configured repositories.
3. Fast-forwards existing repositories.
4. Starts Uvicorn only after synchronization succeeds.

Repository synchronization must never write outside `/data/repos`. The
application itself remains read-only with respect to `REPO_ROOT`.

## Create the Fly App

Install and authenticate `flyctl`, then launch from the Docker build context:

```bash
fly auth login
cd context-engine
fly launch --no-deploy --name <unique-app-name>
```

Choose a region near the developer workstation and the external model provider.
The application is single-worker and long-running model calls can consume one
request slot for several minutes.

Create persistent storage:

```bash
fly volumes create context_data --size 10 --region <region> --app <app-name>
```

Ten GB is only a starting point. Size the volume for the checked-out
repositories plus artifact headroom.

## Configure `fly.toml`

Use the generated `fly.toml` as the base and configure the REST service:

```toml
app = "<app-name>"
primary_region = "<region>"

[build]
  dockerfile = "Dockerfile"

[env]
  REPO_ROOT = "/data/repos"
  OUTPUT_DIR = "/data/artifacts"
  LOG_LEVEL = "INFO"
  LOG_FORMAT = "json"
  INFERENCE_GENERATION_PROVIDER = "openai_compatible"
  INFERENCE_GENERATION_ENDPOINT = "https://provider.example/v1"
  INFERENCE_FAST_MODEL = "qwen3-coder-next"
  INFERENCE_REASONING_MODEL = "qwen3-coder-next"
  INFERENCE_AGENT_MODEL = "qwen3-coder-next"
  INFERENCE_SELECTION_MODEL = "qwen3-coder-next"
  INFERENCE_VERIFICATION_MODEL = "qwen3-coder-next"
  INFERENCE_EMBEDDING_PROVIDER = "ollama"
  INFERENCE_EMBEDDING_ENDPOINT = "https://embedding.example"
  INFERENCE_EMBEDDING_MODEL = "nomic-embed-text"
  OLLAMA_NUM_CTX = "32768"
  ARTIFACTS_MAX_MB = "250"

[mounts]
  source = "context_data"
  destination = "/data"

[http_service]
  internal_port = 8088
  force_https = true
  auto_start_machines = true
  auto_stop_machines = "stop"
  min_machines_running = 0
  processes = ["app"]

  [[http_service.checks]]
    interval = "30s"
    timeout = "10s"
    grace_period = "60s"
    method = "GET"
    path = "/healthcheck"

[[vm]]
  size = "shared-cpu-2x"
  memory = "2gb"
```

`shared-cpu-2x` with 2 GB is a reasonable starting point because inference runs
outside the Machine. Increase memory only after observing actual usage.

Autostop reduces idle compute cost, but the first MCP call after an idle period
will include Machine startup latency. Set `min_machines_running = 1` if
interactive latency matters more than idle cost.

## Configure Secrets

Generate a strong API key:

```bash
openssl rand -hex 32
```

Store secrets without writing them to `fly.toml`:

```bash
fly secrets set \
  CONTEXT_ENGINE_API_KEY='<generated-key>' \
  INFERENCE_GENERATION_API_KEY='<provider-api-key>' \
  INFERENCE_EMBEDDING_API_KEY='<embedding-api-key>' \
  SUPABASE_URL='<supabase-url>' \
  SUPABASE_SERVICE_ROLE_KEY='<service-role-key>' \
  --app <app-name>
```

Add repository credentials only after the sync implementation defines their
exact environment variables or mounted-file locations.

## Deploy and Verify

Deploy:

```bash
fly deploy --app <app-name>
```

Inspect status and logs:

```bash
fly status --app <app-name>
fly logs --app <app-name>
```

Verify the public health endpoints:

```bash
curl https://<app-name>.fly.dev/healthcheck
curl https://<app-name>.fly.dev/health
```

Verify an authenticated endpoint:

```bash
curl \
  -H 'X-API-Key: <context-engine-api-key>' \
  https://<app-name>.fly.dev/log-level
```

Do not consider the deployment ready until `/health` reports:

- the configured generation model is available;
- `repo_mounted` is `true`;
- the expected repository path exists;
- Supabase and vector status match the chosen embedding architecture.

## Point the Local MCP Adapter at Fly

Keep `mcp_http_server.py` on the workstation. Configure its REST target:

```env
CONTEXT_ENGINE_URL=https://<app-name>.fly.dev
CONTEXT_ENGINE_API_KEY=<same-context-engine-api-key>
```

Restart the local MCP service:

```bash
bash scripts/install-mcp.sh
```

Codex and Claude can continue using the existing local MCP URL:

```text
http://127.0.0.1:8089/mcp
```

This avoids exposing the MCP transport publicly. Only the authenticated REST
service is exposed by Fly.

## Operations

Common commands:

```bash
fly status --app <app-name>
fly logs --app <app-name>
fly ssh console --app <app-name>
fly secrets list --app <app-name>
fly volumes list --app <app-name>
```

Check repository freshness from an SSH console:

```bash
git -C /data/repos/<owner>/<repo> status --short --branch
git -C /data/repos/<owner>/<repo> log -1 --oneline
```

Deployments replace the Machine's root filesystem but preserve the mounted
volume. Application code belongs in the image; repositories and artifact
backups belong on the volume.

## Security Checklist

- Set `CONTEXT_ENGINE_API_KEY`; never expose an unauthenticated cloud engine.
- Keep MCP bound to `127.0.0.1` on the workstation.
- Use read-only repository credentials.
- Never include `.env`, API keys, SSH keys, or Supabase service-role keys in the
  image.
- Keep `/health`, `/healthcheck`, and `/setup` free of secret values.
- Leave `LOG_LEVEL=INFO`; `TRACE` can log prompt and response content.
- Rotate API, repository, Ollama, and Supabase credentials independently.
- Restrict the set of repositories synchronized into the volume.

## Official References

- [Fly Launch](https://fly.io/docs/launch/)
- [Fly application configuration](https://fly.io/docs/reference/configuration/)
- [Fly Volumes](https://fly.io/docs/volumes/overview/)
- [Fly secrets](https://fly.io/docs/apps/secrets/)
- [Fly autostop and autostart](https://fly.io/docs/launch/autostop-autostart/)
- [Fly GPU deprecation notice](https://fly.io/docs/gpus/getting-started-gpus/)
- [Ollama Cloud](https://docs.ollama.com/cloud)
- [Ollama API authentication](https://docs.ollama.com/api/authentication)
