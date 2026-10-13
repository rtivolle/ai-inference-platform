# Operations & Autoscaling

## KEDA autoscaling

Template: `monitoring/keda-scaledobjects.yaml.j2`. Targets are the operator-owned
`DynamoGraphDeploymentScalingAdapter` (`nvidia.com/v1beta1`), **not** the DGD:

- `<deploy>-prefill-scaler` → `<deploy>-prefill`: TTFT p95 > `0.35` s
  (`dynamo_frontend_time_to_first_token_seconds_bucket`) OR waiting requests > `5`.
  Cooldown 120 s; HPA: scale-up +100 %/15 s, scale-down −25 %/30 s (60 s window).
- `<deploy>-decode-scaler` → `<deploy>-decode`: `avg(vllm:gpu_cache_usage_perc)`
  > `0.80`. Cooldown 180 s; scale-up +50 %/15 s, scale-down −10 %/60 s (120 s window).

`minReplicaCount` = plan replicas; `maxReplicaCount` = 4× (overridable via
`prefill_max_replicas` / `decode_max_replicas`). Applied by the reconciler on
deploy (disaggregated only), deleted before the DGD on teardown. Do not enable
the Dynamo `planner` component on the same adapters (thrashing). Polling 15 s;
server: `prometheus-kube-prometheus-prometheus.monitoring.svc:9090`.

## Monitoring

- `monitoring/prometheus-rules.yaml`: recording rules
  (`vllm:num_requests_waiting`, `vllm:gpu_cache_usage_perc`, `dcgm:gpu_utilization_avg`)
  + alerts `DynamoKVCacheExhaustion` (>95 %/30 s, critical),
  `DynamoHighQueueWaiting` (>20/1 m, warning), `GpuTemperatureHigh` (>85 °C/2 m).
- Usage API: `GET /api/v1/metrics/usage?window_minutes=5|15|60`
  (`control-plane/api/metrics.py`, needs `PROMETHEUS_URL` + optional
  `PROMETHEUS_BEARER_TOKEN`): request/output-token rates, running/waiting,
  TTFT p95, GPU util/memory, KV-cache max. Drain checks reuse
  `num_requests_running`.

## Day-2 tasks

- **Scale bounds**: edit plan replica defaults or pass max-replica overrides;
  watch TTFT p95 + queue depth before raising thresholds.
- **Upgrades**: republish digests, redeploy per [Multi-Cluster](multi-cluster.md);
  redeploy models to refresh `target_node_names` after adding GPU nodes.
- **Postgres**: StatefulSet PVCs survive pod restarts; HA/backups are external.
  Never rotate `litellm-salt-key` with live models.
- **Probes**: control plane `/health*` (Deployment probes, 5 s/10 s); LiteLLM
  `/health/liveliness|readiness`; DGD frontend readiness gates LiteLLM registration.
