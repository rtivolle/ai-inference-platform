# LiteLLM installation and automatic configuration

Run from the repository root with Python 3.11, PyYAML, kubectl, and an explicit
kubeconfig context. Choose a tested LiteLLM proxy image that includes PostgreSQL
support, pinned to its SHA-256 digest:

```bash
python -m scripts.install_litellm \
  --context "$KUBE_CONTEXT" \
  --image "$LITELLM_IMAGE"
```

The installer creates `ai-gateway` and `dynamo-control-plane`, generates matching
LiteLLM master keys and a control-plane API token, and stores them in each
namespace's `inference-runtime` Secret. It generates a separate encryption salt
and preserves all credentials on subsequent runs. Existing installations without
a salt retain their master key as the salt, matching LiteLLM's previous encryption
fallback. Mismatched existing master keys and database changes fail before writes.
Credentials are sent to kubectl through stdin and are never printed or rendered
into local release files. Do not rotate the encryption salt after models exist.

By default, Postgres 16 runs as a single-instance StatefulSet with a 10Gi PVC and
the cluster's default StorageClass. Set `--storage-class` to use a specific class.
The database must become ready before LiteLLM is applied. PVCs retain database
state across pod replacements; backups and database high availability need to be
managed separately. An existing ephemeral `Deployment/postgres` is rejected;
back up and migrate its data before replacing it.

For a separately managed PostgreSQL database, supply `DATABASE_URL` through the
environment before the first installation. The installer skips bundled Postgres
and preserves that URL on later runs. Database URLs must use `postgresql://` or
`postgres://`; encode special characters in usernames/passwords.

LiteLLM runs behind the internal `litellm-service` on port 4000. Configuration is
loaded from `config.example.yaml`: models live in the database, the static model
list is empty, and each replica reloads models every 10 seconds. The existing
control-plane EndpointSlice watcher automatically registers ready Dynamo models
and removes them through LiteLLM's management API. A config checksum triggers a
rollout when the configuration changes. The installer waits up to 10 minutes for
each database/gateway rollout and stops if readiness fails.

The control-plane manifest reads the matching master key and API token from
`inference-runtime`; deploy the control plane separately after gateway readiness.
The installer does not replace a running control-plane Deployment. With private
images, provision appropriate pull credentials before installation.

To include installation in the cluster bootstrap, set `LITELLM_IMAGE` to a digest
reference. Optionally set `LITELLM_KUBE_CONTEXT` and `PYTHON_BIN` to a Python 3.11
interpreter with PyYAML installed. With no image set, bootstrap skips the gateway.

The multi-cluster release also enables database model storage and the reload
interval. Provision `litellm-salt-key` in the gateway runtime Secret before applying
that release, or run this installer with your external database first. Each
cluster uses its own database and credentials.

Validation covers manifest rendering and mocked Kubernetes operations. Verify
real image migrations, storage provisioning, and inference in the target cluster.
Recover a failed rollout by fixing the reported cluster events and rerunning with
the same database and Secrets. Restore a previous tested image digest if needed,
checking compatibility with any database migrations; the installer does not
roll back database schemas or delete PVCs.

References: [LiteLLM model management](https://docs.litellm.ai/docs/proxy/model_management),
[LiteLLM production guidance](https://docs.litellm.ai/docs/proxy/prod).
