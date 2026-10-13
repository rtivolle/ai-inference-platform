# Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` | Missing/wrong Bearer; expired session; bad CSRF/Origin | Check `CONTROL_PLANE_API_TOKEN`; re-login; send `X-CSRF-Token` + matching `Origin` |
| `404 model could not be resolved` | Typo, private repo without `HF_TOKEN`, or HF outage | Verify id; set `HF_TOKEN`/`hf-token-secret`; use a `VERIFIED_MODELS` id offline |
| `409 insufficient capacity` | `M_total` needs more GPUs/VRAM than `available_count` | Lower `max_model_len`/concurrency/`gpu_memory_utilization`, use `fp8`, add nodes, check `GET /cluster/gpus` (allocated = existing DGDs) |
| `422 validation` | Bad `deployment_name` (RFC 1123), dtype/quant mix, window not 5/15/60 | Fix fields; `quantization` only `fp8/awq/gptq/null` |
| Deploy stuck `provisioning` | Warmer still running / EndpointSlice never Ready | `kubectl -n dynamo-workloads get jobs,dgd,endpointslices`; check warmer logs (HF 429/auth), DGD events, image pull secrets |
| `Model cache warming failed` | Warmer Job Failed/timeout on ≥1 node | `kubectl describe job`; verify `/mnt/nvme` space, `hf-token-secret`, network; redeploy (new nodes need a fresh deploy) |
| `LiteLLM registration/deregistration failed` | Wrong `LITELLM_PROXY_URL`/key, DB down, no `store_model_in_db` | Check Secret match in both namespaces, `DATABASE_URL`, Postgres readiness, proxy logs; deregistration failure intentionally blocks DGD deletion — fix gateway then retry `DELETE` |
| 502s after delete | Replicas still serving from stale DB cache | Keep `PROXY_CONFIG_RELOAD_INTERVAL_SECONDS=10`; drain waits for `num_requests_running==0` (needs `PROMETHEUS_URL`) |
| KEDA not scaling | Prometheus unreachable, wrong labels, planner conflict | Check ScaledObject queries/labels, Prometheus svc, one controller per adapter |
| `CUDA OOM` at startup | Plan under-sized for real ctx/concurrency | Raise TP via smaller `max_model_len`/concurrency or `fp8`; verify `H_kv % TP==0`; check `estimated_vram_gb_per_replica` vs GPU |
| SSE/stream stalls via Cloudflare | 100 s idle timeout | Enable keep-alive/chunked comments; `proxy-read-timeout: 3600`, `proxy-buffering: off` |
| `Mock forbidden in-cluster` | `CONTROL_PLANE_MOCK=1` with serviceaccount token | Unset mock; supply kubeconfig/Secrets |
| SSE/`kubectl` context errors in releases | Wrong inventory context | Validate `deploy/clusters.json` contexts; never hand-edit current-context mid-run |

Collect: control-plane logs, `kubectl -n dynamo-workloads describe dgd|job|scaledobject`,
`kubectl -n ai-gateway logs deploy/litellm-proxy`, Prometheus
`vllm:num_requests_waiting|running`, `vllm:gpu_cache_usage_perc`, DCGM temp/util.
