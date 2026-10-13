# Dynamo Deployments

Templates: `dynamo/templates/dgd-{disaggregated,aggregated}.j2` composed via
`dgd-components.yaml.j2`. API: `nvidia.com/v1beta1`, kind
`DynamoGraphDeployment`, `spec.backendFramework: vllm`.

## Disaggregated vs aggregated

| Aspect | Disaggregated | Aggregated |
|---|---|---|
| Template | `dgd-disaggregated.yaml.j2` | `dgd-aggregated.yaml.j2` |
| Components | `Frontend` + `prefill` + `decode` | `Frontend` + `worker` |
| vLLM flags | `--disaggregation-mode prefill\|decode`, `--kv-transfer-config {"kv_connector":"NixlConnector","kv_role":"kv_both"}` | no disaggregation flags |
| Prefill extras | `--max-num-batched-tokens`, `--enable-chunked-prefill`, `--kv-events-config` | — |
| Decode extras | `--max-num-seqs` | `--max-num-seqs` on worker |
| KEDA | two ScaledObjects (see Operations) | none |
| Frontend | `python3 -m dynamo.frontend`, `DYN_ROUTER_MODE=kv` | same |

Workers run `python3 -m dynamo.vllm` with `--model`, `--tensor-parallel-size`
(plus `--pipeline-parallel-size` when PP > 1), `--dtype` (always),
`--quantization` (only when non-null), `--max-model-len`,
`--gpu-memory-utilization`, `--download-dir=/mnt/nvme/model-cache`.

## Scheduling and resources

- Pod affinity pins workers to `target_node_names` (the warmed hostnames) +
  `nvidia.com/gpu.product` match; falls back to `nodeSelector` on SKU or single
  `target_node`.
- `resources.requests/limits: nvidia.com/gpu = TP`; prefill/decode carry
  `scalingAdapter: {}` so the operator creates a `DynamoGraphDeploymentScalingAdapter`
  per component for KEDA.
- `hostPath /mnt/nvme/model-cache` + `emptyDir /dev/shm`; pull secrets via
  `DYNAMO_RUNTIME_IMAGE_PULL_SECRET`.
- Images: `DYNAMO_RUNTIME_IMAGE` (default
  `nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.5.1`); `MODEL_WARMER_IMAGE` (+
  `..._PULL_SECRET`) for warmers.

## Warmer Jobs (`loader/warmer-job.yaml.j2`)

- The reconciler creates **one Job per eligible GPU hostname**
  (`nodeSelector: kubernetes.io/hostname`), named
  `<deploy>-warmer-<10-hex-suffix>`, and waits for **all** (30 min each).
- Container runs `model_warmer.py --model-id … --cache-dir /mnt/nvme/model-cache`
  with `HF_HUB_ENABLE_HF_TRANSFER=1`, `HF_HOME=/mnt/nvme/model-cache`, optional
  `HF_TOKEN` from `hf-token-secret`, `ttlSecondsAfterFinished: 600`.
- New GPU nodes are **not** picked up automatically — redeploy to warm them and
  refresh the affinity list.

## Rendering and validation

```bash
# Rendered DGD + warmer + KEDA are validated in CI:
python -m scripts.validate_manifests
```

Schema: `contracts/dynamo_crd.schema.json`. Tests:
`tests/test_crd_rendering.py`, `tests/test_reconciliation.py`,
`tests/e2e_deployment_flow.py` (mocked lifecycle).
