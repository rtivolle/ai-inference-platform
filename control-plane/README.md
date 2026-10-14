# Inference console configuration

The FastAPI service serves the browser console at `/`. It includes model
deployment, GPU inventory, live Prometheus usage, node health, component scaling,
container logs, and pod replacement. Browser requests use the same server-side
Kubernetes identity as the API; credentials and kubeconfigs never enter the browser.

## Administrator sign-in

For first-run setup, launch the service from an interactive terminal:

```bash
source .venv/bin/activate
python -m api
```

The setup prompts for the administrator account, browser origin, an existing
kubeconfig/context or in-cluster identity, gateway credentials, and Prometheus
URL. It then starts the service. `python -m api setup` configures without starting;
`python -m api setup --force` replaces existing configuration. An explicit mock
option is available for offline development.

Settings are saved atomically to `.inference/config.json` with mode 0600; this
directory is excluded from Git and Docker build contexts. Only a kubeconfig path
and context name are saved, not a copy of Kubernetes credentials. The local file
contains the administrator hash, API token, and any supplied gateway/monitoring
credentials, and is loaded into the server environment. A private SQLite file
in the same directory retains sessions. `--config` selects another protected
configuration file. Explicit environment variables take precedence.

In Kubernetes, provision the ConfigMap and Secrets described below before the
first pod starts; unattended containers use environment configuration directly.
First-run setup does not create a cluster, monitoring stack, or gateway.

Configure a username and an Argon2id password hash. Generate the hash interactively;
the password is never passed in command-line arguments:

```bash
source .venv/bin/activate
export CONTROL_PLANE_ADMIN_USERNAME=admin
export CONTROL_PLANE_ADMIN_PASSWORD_HASH="$(python -m api.auth hash-password)"
```

Use a password of at least 12 characters. Supply the hash through your secret
manager or environment. Sign-in is disabled until both values are set. There is
no default administrator password or public registration endpoint. The API token
remains available for scripts and automation; it is a separate administrator credential.
Cloudflare Access can protect the service externally, but arbitrary Access header
values do not authenticate requests to the control plane.

Passwords are verified with Argon2id. Successful login rotates any existing
session and issues an opaque, host-only, HttpOnly, SameSite=Strict cookie. Secure
cookies are the default in live mode. Sessions expire after one hour by default
(`CONTROL_PLANE_SESSION_SECONDS`, between five minutes and one day). Logout revokes
the server-side session. Cookie-authenticated mutations require the session's
`X-CSRF-Token`; requests with a foreign Origin are rejected. Repeated failed
logins are limited to five attempts per source address in five minutes.

Set `CONTROL_PLANE_PUBLIC_URL` to the exact browser origin, for example
`https://control.example.com`, when using an ingress or TLS proxy. It must match
the browser's Origin header. `CONTROL_PLANE_COOKIE_SECURE=0` is available for
local HTTP development; use HTTPS for live browser access.

Session and login-attempt state use SQLite in memory by default. Restarting the
server signs users out. Set `CONTROL_PLANE_SESSION_DB` to a protected writable
SQLite file to retain sessions across restarts. Run one Uvicorn worker and one
control-plane replica with this session implementation, as in the supplied
Deployment. Multiple replicas require a shared session service and shared rate limiting.

For local development with simulated Kubernetes resources and real password login:

```bash
export CONTROL_PLANE_PUBLIC_URL=http://127.0.0.1:8000
CONTROL_PLANE_MOCK=1 uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Mock mode is explicit and refused in-cluster. It never generates usage metrics.
If no administrator is configured, a development-token form is available only
in mock mode; its default token is `test-token` unless overridden.

## Live usage metrics

Set `PROMETHEUS_URL` to a reachable Prometheus HTTP API. Supply
`PROMETHEUS_BEARER_TOKEN` through a Secret when the monitoring endpoint requires
authentication. URLs cannot contain embedded credentials, query parameters, or fragments.
Prometheus calls run on the server with bounded timeouts and ten-second caching.

The Live usage view reports successful requests/second, generated tokens/second,
running and waiting requests, p95 time to first token, workload GPU utilization,
GPU memory occupancy, and maximum KV-cache occupancy. History windows are 5, 15,
or 60 minutes. Rates use two-minute PromQL windows. Missing samples, invalid
values, missing configuration, and failed queries are shown as unavailable rather
than zero. Charts preserve gaps and do not invent or interpolate samples.

Scrape vLLM worker `/metrics` endpoints and NVIDIA DCGM exporter into Prometheus.
The default queries use standard `vllm:*` and `DCGM_FI_DEV_*` metric names and
scope data to `namespace="dynamo-workloads"`. DCGM series must carry the workload
namespace label. Adjust the queries in `api/metrics.py` to match deployment-specific
exporter versions or label conventions. This view reports successful inference
usage from vLLM; gateway failures and billing data need separate gateway telemetry.

Live deletion waits for gateway deregistration to propagate, then checks fresh
model-scoped running-request metrics. If monitoring is unavailable or requests do
not drain before the deadline, the Kubernetes workload remains and its status
becomes `delete_failed`. The gateway propagation wait defaults to 15 seconds;
set `CONTROL_PLANE_GATEWAY_DRAIN_DELAY_SECONDS` above your gateway's configuration
reload interval. Retry deletion after fixing the failure. A failed drain can leave
the model deregistered from the gateway while its workload remains.

## Kubernetes operations

Live mode uses in-cluster service-account credentials or the configured local
kubeconfig. The console can inspect cluster nodes and operate deployments labeled
`app.kubernetes.io/managed-by=dynamo-control-plane` in `dynamo-workloads`.
It provides:

- Pod readiness, node placement, and restart counts.
- Container log reads bounded to 500 lines and 128 KB; the UI requests 100 lines.
- Component replicas through Dynamo scaling adapters, or direct component replicas
  when the component has no adapter configured. Resource versions prevent stale writes.
- Pod replacement through the Kubernetes `policy/v1` Eviction API, using the pod UID
  as a precondition. Kubernetes disruption budgets can refuse replacement.

Manual scaling is blocked when KEDA, HPA, or Dynamo Planner owns the component.
Scaling checks available GPU capacity and minimum replica counts. The UI asks for
confirmation before replica changes and pod replacement; these operations can
interrupt active requests. Scaling is limited to 1–64 replicas; graceful model
deletion handles complete teardown. There is no browser shell, arbitrary pod
deletion, Secret access, or node mutation endpoint.

Apply the reviewed permissions from `infra/control-plane-rbac.yaml`. The
application uses the `control-plane` service account, with workload operations
restricted to `dynamo-workloads` and cluster-wide node/pod listing for inventory.

## Cluster configuration

Each cluster can supply the optional `inference-console-config` ConfigMap and
`inference-console-auth` Secret in `dynamo-control-plane`; the Deployment imports
them through `envFrom`. Examples are in `infra/console-config.example.yaml` and
`infra/console-auth.example.yaml`. Replace the public origin and Prometheus URL
with that cluster's values, and provision the administrator hash through your
secret manager. Existing `inference-runtime` Secrets still provide the LiteLLM
master key and API token. Changes to environment configuration require a new pod.

The API reference at `/docs` describes login/session endpoints and all operational
routes. The checked-in OpenAPI contract is generated from the running application.
