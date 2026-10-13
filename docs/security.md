# Security

## API authentication (`control-plane/api/auth.py`)

- Bearer (`CONTROL_PLANE_API_TOKEN`) **or** admin cookie session on every
  `/api/v1/*`. No anonymous access; Cloudflare Access headers alone do not
  authenticate (outer gate only).
- Admin: `CONTROL_PLANE_ADMIN_USERNAME` + Argon2 `CONTROL_PLANE_ADMIN_PASSWORD_HASH`
  (`python -m api.auth hash-password`, ≥12 chars). Sessions: opaque token in
  `HttpOnly; SameSite=Strict` cookie (`Secure` unless mock or
  `CONTROL_PLANE_COOKIE_SECURE=0`), SQLite store (`CONTROL_PLANE_SESSION_DB`,
  default memory), TTL `CONTROL_PLANE_SESSION_SECONDS` clamped 300–86400 s,
  previous session revoked on login. Mutations need `X-CSRF-Token` + valid `Origin`.
- Brute force: 5 failures / IP / 5 min → `429` (`Retry-After: 300`).

## Secrets (never commit)

| Secret | Namespace | Keys | Consumed by |
|---|---|---|---|
| `inference-runtime` | `dynamo-control-plane` | `litellm-master-key`, `api-token` | control-plane env (`secretKeyRef`) |
| `inference-runtime` | `ai-gateway` | `litellm-master-key`, `litellm-salt-key`, `database-url` (+ `postgres-password` for bundled DB) | LiteLLM env |
| `hf-token-secret` | `dynamo-workloads` | `token` (optional) | warmer `HF_TOKEN`, DGD `hf-token-secret` ref |
| `cloudflare-secret` | Cloudflare ns | tunnel token | `cloudflared` |
| `registry-credentials` | all app ns | pull secret | multi-cluster images |

Installers pass credentials via kubectl stdin and strip them from artifacts;
`deploy_clusters.py` renders only `secretKeyRef`s.

## RBAC and network

- App ServiceAccount (`infra/control-plane-rbac.yaml`,
  `infra/control-plane-deployment.yaml`): manages DGDs + warmer Jobs in
  `dynamo-workloads`, reads EndpointSlices there, lists nodes/pods for inventory.
  Deploy identity additionally needs Secrets/CRDs/Deployments/Services/ConfigMaps/RBAC.
- Mock (`CONTROL_PLANE_MOCK=1`) is **refused in-cluster** by both `K8sClient`
  and `LiteLLMSync`.
- Edge: Ingress-NGINX ClusterIP-only (no NodePorts), egress-only Cloudflare
  Tunnel, Cloudflare Access in front of `control.<domain>`; SSE streaming needs
  `proxy-buffering: off` + keep-alives (Cloudflare ~100 s idle timeout).
