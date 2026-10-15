# Cloudflare Tunnel — mila-api.ca

Tunnel: `ai-inference-platform` (`178f9ecb-1c18-472c-82da-e5022014c51b`).
Account: `287a983589aa84ca84bed74e3a61d8d1`.

The remotely managed tunnel routes `llm.mila-api.ca` and
`control.mila-api.ca` to
`http://ingress-nginx-controller.ingress-nginx.svc.cluster.local:80`.
Both proxied CNAME records point to
`178f9ecb-1c18-472c-82da-e5022014c51b.cfargotunnel.com`.
Unmatched hostnames return 404. The existing `Dev` tunnel is independent.

## Activate in Kubernetes

Select the intended kubeconfig/context first. Install Ingress-NGINX using
`ingress-nginx-values.yaml` and deploy the gateway and control plane.
Obtain the connector token for this tunnel from Cloudflare Zero Trust,
Networks → Connectors → Cloudflare Tunnels. Keep it out of source control.
In Bash, create the connector Secret without printing the token:

```bash
kubectl create namespace cloudflare --dry-run=client -o yaml | kubectl apply -f -
read -rsp 'Tunnel token: ' tunnel_token; echo
printf '%s' "$tunnel_token" | kubectl -n cloudflare create secret generic cloudflare-tunnel-credentials \
  --from-file=tunnel-token=/dev/stdin --dry-run=client -o yaml | kubectl apply -f -
unset tunnel_token
kubectl apply -f infra/cloudflare/ingress-rules.yaml
kubectl apply -f infra/cloudflare/cloudflared-deployment.yaml
kubectl -n cloudflare rollout status deployment/cloudflared --timeout=180s
```

Cloudflare manages public hostname routing; the Deployment does not use a
local credentials JSON or ConfigMap. Two connector replicas provide redundancy
within the same cluster. Do not attach unrelated clusters to this tunnel.

ExternalDNS is not needed for these two records, which are already provisioned.
If enabled separately, it requires its own DNS API token configuration; the
connector token does not authorize DNS API operations.

Verify both application endpoints over HTTPS after the connector is healthy.
Until then the tunnel is inactive and these endpoints cannot serve traffic.

## GitHub Actions

The `cloudflare-production` GitHub Environment holds:

- Secret `CLOUDFLARE_TUNNEL_TOKEN`: connector token for this tunnel.
- Variables `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_ZONE_ID`, `CLOUDFLARE_DOMAIN`,
  `CLOUDFLARE_TUNNEL_ID`, `CLOUDFLARE_TUNNEL_NAME`,
  `CLOUDFLARE_LLM_HOSTNAME`, and `CLOUDFLARE_CONTROL_HOSTNAME`: resource inventory.
  The deployed hostnames are defined in the versioned manifests and remote
  tunnel configuration; changing these variables alone does not change routing.

Before running **Deploy Cloudflare Connector**, add the raw kubeconfig YAML as
its `KUBECONFIG` environment secret and set `KUBECTL_VERSION` to a version
compatible with the target cluster. Supply the exact kubeconfig context when
starting the workflow. Set `DEPLOY_RUNNER` if the API needs a private runner.
The target cluster must already have Ingress-NGINX and the application services.
The deploy identity needs namespace creation and permission to manage the
connector Secret/Deployment and ingress resources.

The existing repository `CLOUDFLARE_API_TOKEN` is preserved. This workflow uses
only the connector token and does not need DNS API access. It installs the
committed routing configuration and does not provision Kubernetes or applications.
