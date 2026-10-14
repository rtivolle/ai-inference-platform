# Dispatch for Spec Miner Survey 3

## Assigned Scope: Contracts, Schemas, Scripts, Loader, Infra & Monitoring
Read:
- `/home/rtivolle/ai-inference-platform/.agents/teamwork/ORIGINAL_REQUEST.md`
- `/home/rtivolle/ai-inference-platform/contracts/`
- `/home/rtivolle/ai-inference-platform/scripts/`
- `/home/rtivolle/ai-inference-platform/loader/`
- `/home/rtivolle/ai-inference-platform/infra/`
- `/home/rtivolle/ai-inference-platform/monitoring/`
- `/home/rtivolle/ai-inference-platform/AGENTS.md`


## 2026-10-13T21:58:09Z
You are Spec Miner Survey 3 for the AI Inference Platform Codebase Audit.
Your working directory is /home/rtivolle/ai-inference-platform/.agents/teamwork/spec_miner_survey_3/.
You MUST read /home/rtivolle/ai-inference-platform/.agents/teamwork/ORIGINAL_REQUEST.md before starting work.
Authoritative guidelines: /home/rtivolle/ai-inference-platform/AGENTS.md.

Your objective in this Survey phase is to systematically mine specifications and audit:
- `contracts/`: JSON schemas (dynamo_crd, model_spec), OpenAPI specifications, and template alignment (dynamo/templates/).
- `scripts/`: Cluster bootstrap, deployment automation, LiteLLM installer, manifest validation (scripts/*.sh).
- `loader/`: Model download/cache warmer job template, container runner, Dockerfile, scripts.
- `infra/` and `monitoring/`: Kubernetes manifests, RBAC policies, KEDA scalers, Prometheus alert rules and dashboards.

Tasks:
1. Extract all specifications, schemas, CRD contracts, manifest requirements, and shell script expectations.
2. Identify contract mismatches between contracts/, API schemas, and Jinja2 templates (dynamo/templates/).
3. Identify defects in scripts (shell bugs, unquoted variables, missing error traps, security risks), loader (cache corruption, concurrency, failure modes), infra manifests (RBAC overpermission or missing permissions, selector mismatches, volume mount issues), and monitoring (syntax errors in PromQL, metric label mismatches).
4. Note exact file paths, line numbers, contract discrepancies, and preliminary severity.
5. Write your complete report to /home/rtivolle/ai-inference-platform/.agents/teamwork/spec_miner_survey_3/report.md and handoff.md.
6. Send a message to orchestrator (conversation ID: fbda79c3-ec32-408a-a403-9cabb555b0a1) when complete.
