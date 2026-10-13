"use strict";

const $ = (id) => document.getElementById(id);
const escapeHtml = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[char],
  );
const state = {
  token: "",
  connected: false,
  models: [],
  inventory: null,
  session: 0,
  refreshing: false,
};
const pendingRequests = new Set();
let deleteTarget = null;
let toastTimer;

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function api(path, { method = "GET", body, timeout = 20000 } = {}) {
  const controller = new AbortController();
  pendingRequests.add(controller);
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const headers = { Authorization: `Bearer ${state.token}` };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, {
      method,
      headers,
      signal: controller.signal,
      cache: "no-store",
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const data = await response.json();
    if (!response.ok) {
      let message = data.detail || `Request failed (${response.status}).`;
      if (Array.isArray(message)) {
        message = message
          .map(
            (error) =>
              `${error.loc?.slice(1).join(".") || "Request"}: ${error.msg}`,
          )
          .join("; ");
      }
      throw new ApiError(String(message), response.status);
    }
    return data;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error.name === "AbortError")
      throw new Error(
        "Request interrupted or timed out. Refresh to check the current deployment state before retrying.",
      );
    throw new Error(
      "Cannot reach the control plane. Check the service connection and try again.",
    );
  } finally {
    clearTimeout(timer);
    pendingRequests.delete(controller);
  }
}

function formError(id, message = "") {
  $(id).textContent = message;
  $(id).hidden = !message;
}

function toast(message) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => {
    $("toast").hidden = true;
  }, 6000);
}

function busyDialog(id, busy) {
  const dialog = $(id);
  dialog.dataset.busy = String(busy);
  dialog.querySelectorAll("button, input, select").forEach((element) => {
    element.disabled = busy;
  });
  // Keep the connection in place until an in-flight mutation finishes.
  $("connection-button").disabled = busy;
}

function openConnect() {
  formError("connect-error");
  $("api-token").value = "";
  $("connect-dialog").showModal();
}

function openDeploy() {
  if (!state.connected) return openConnect();
  formError("deploy-error");
  $("deploy-dialog").showModal();
}

function displayStatus(model) {
  const status = model.status || "unknown";
  if (status === "running" && model.ready)
    return { label: "Ready", className: "ready", group: "ready" };
  if (["failed", "readiness_timeout", "delete_failed"].includes(status))
    return {
      label:
        status === "readiness_timeout"
          ? "Readiness timeout"
          : status === "delete_failed"
            ? "Deletion failed"
            : "Failed",
      className: "failed",
      group: "failed",
    };
  if (status === "provisioning")
    return {
      label: "Provisioning",
      className: "provisioning",
      group: "provisioning",
    };
  if (status === "deleting")
    return { label: "Deleting", className: "neutral", group: "deleting" };
  if (status === "running")
    return {
      label: "Not ready",
      className: "provisioning",
      group: "provisioning",
    };
  return {
    label: status.replaceAll("_", " "),
    className: "neutral",
    group: status,
  };
}

function statusBadge(model) {
  const status = displayStatus(model);
  return `<span class="badge ${status.className}"><span class="status-dot ${status.className === "ready" ? "green" : status.className === "provisioning" ? "amber" : ""}"></span>${escapeHtml(status.label)}</span>`;
}

function modeLabel(mode) {
  return mode === "aggregated"
    ? "Aggregated"
    : mode === "disaggregated"
      ? "Prefill / decode"
      : "—";
}

function setConnectedControls() {
  for (const id of [
    "refresh-button",
    "new-deployment-button",
    "model-search",
    "status-filter",
  ])
    $(id).disabled = !state.connected;
  $("connection-dot").className =
    `status-dot${state.connected ? " green" : ""}`;
  $("connection-label").textContent = state.connected
    ? "Disconnect"
    : "Connect";
}

function resetWorkspace() {
  $("nav-count").textContent = "—";
  $("deployment-count").textContent = "0";
  ["stat-deployments", "stat-ready", "stat-available", "stat-nodes"].forEach(
    (id) => {
      $(id).textContent = "—";
    },
  );
  $("stat-total").textContent = "";
  $("stat-network").textContent = "Cluster inventory";
  $("gpu-pools").replaceChildren();
  $("gpu-empty").hidden = false;
  $("sync-dot").className = "status-dot";
  $("sync-label").textContent = "Not connected";
  $("last-updated").textContent = "Updates every 10 seconds";
  renderModels();
}

function disconnect() {
  state.session += 1;
  for (const controller of pendingRequests) controller.abort();
  state.token = "";
  state.connected = false;
  state.models = [];
  state.inventory = null;
  state.refreshing = false;
  $("model-search").value = "";
  $("status-filter").value = "all";
  $("error-notice").hidden = true;
  document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
  setConnectedControls();
  resetWorkspace();
}

function renderModels() {
  const query = $("model-search").value.toLowerCase().trim();
  const filter = $("status-filter").value;
  const models = state.models.filter((model) => {
    const matchesQuery = `${model.name} ${model.model_id}`
      .toLowerCase()
      .includes(query);
    return (
      matchesQuery &&
      (filter === "all" || displayStatus(model).group === filter)
    );
  });
  $("deployment-rows").innerHTML = models
    .map((model) => {
      const name = escapeHtml(model.name);
      return `<tr><td><div class="model-cell"><span class="model-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5"/></svg></span><div><button class="model-name" data-action="details" data-name="${name}">${name}</button><span class="model-id mono">${escapeHtml(model.model_id)}</span></div></div></td><td>${statusBadge(model)}</td><td>${escapeHtml(modeLabel(model.serving_mode))}</td><td><div class="row-actions"><button class="text-button" data-action="details" data-name="${name}" aria-label="View ${name}">Details</button><button class="text-button delete" data-action="delete" data-name="${name}" aria-label="Delete ${name}" ${model.status === "deleting" ? "disabled" : ""}>Delete</button></div></td></tr>`;
    })
    .join("");
  $("empty-state").hidden = models.length > 0;
  const isFiltered = state.connected && state.models.length > 0;
  $("empty-title").textContent = !state.connected
    ? "Connect to your control plane"
    : isFiltered
      ? "No matching deployments"
      : "Your next model starts here";
  $("empty-description").textContent = !state.connected
    ? "Enter your API token to view models and GPU capacity."
    : isFiltered
      ? "Try a different model name or clear the status filter."
      : "Deploy a Hugging Face model. The auto-tuner will find a configuration for your GPUs.";
  $("empty-action").textContent = !state.connected
    ? "Connect to platform"
    : isFiltered
      ? "Clear filters"
      : "Deploy your first model";
  $("results-caption").textContent = state.connected
    ? `${models.length} of ${state.models.length} deployment${state.models.length === 1 ? "" : "s"}`
    : "No data loaded";
}

function renderWorkspace() {
  const pools = state.inventory?.gpu_pools || [];
  const total = pools.reduce((sum, pool) => sum + pool.total_count, 0);
  const available = pools.reduce((sum, pool) => sum + pool.available_count, 0);
  $("nav-count").textContent = state.models.length;
  $("deployment-count").textContent = state.models.length;
  $("stat-deployments").textContent = state.models.length;
  $("stat-ready").textContent = state.models.filter(
    (model) => displayStatus(model).group === "ready",
  ).length;
  $("stat-available").textContent = available;
  $("stat-total").textContent = `/ ${total}`;
  $("stat-nodes").textContent = state.inventory.nodes_count;
  $("stat-network").textContent = state.inventory.rdma_network_active
    ? "RDMA network active"
    : "RDMA network inactive";
  $("gpu-empty").hidden = pools.length > 0;
  $("gpu-empty").querySelector("p").textContent =
    "The cluster reports no GPU pools.";
  $("gpu-pools").innerHTML = pools
    .map((pool) => {
      const percent =
        pool.total_count > 0
          ? Math.max(
              0,
              Math.min(100, (pool.available_count / pool.total_count) * 100),
            )
          : 0;
      return `<article class="gpu-card"><div class="gpu-card-heading"><div><h3>${escapeHtml(pool.gpu_sku)}</h3><p>${escapeHtml(pool.vram_gb)} GB VRAM per GPU</p></div><span class="badge ${pool.rdma_supported ? "ready" : "muted"}">${pool.rdma_supported ? "RDMA supported" : "No RDMA"}</span></div><div class="gpu-capacity"><strong>${escapeHtml(pool.available_count)}</strong> / ${escapeHtml(pool.total_count)} GPUs available</div><div class="capacity-bar" role="meter" aria-label="Available GPUs in ${escapeHtml(pool.gpu_sku)}" aria-valuemin="0" aria-valuemax="${escapeHtml(Math.max(1, pool.total_count))}" aria-valuenow="${escapeHtml(pool.available_count)}"><span style="width:${percent}%"></span></div><div class="gpu-legend"><span><span class="status-dot green"></span>${escapeHtml(pool.available_count)} available</span><span><span class="status-dot"></span>${escapeHtml(pool.total_count - pool.available_count)} allocated</span></div></article>`;
    })
    .join("");
  renderModels();
}

async function loadWorkspace() {
  const [inventory, models] = await Promise.all([
    api("/api/v1/cluster/gpus"),
    api("/api/v1/models"),
  ]);
  return { inventory, models };
}

function applyWorkspace(data) {
  state.models = data.models;
  state.inventory = data.inventory;
  $("error-notice").hidden = true;
  $("sync-dot").className = "status-dot green";
  $("sync-label").textContent = "Auto-refresh on";
  $("last-updated").textContent =
    `Updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
  renderWorkspace();
}

async function refreshWorkspace() {
  if (!state.connected || state.refreshing) return;
  state.refreshing = true;
  const session = state.session;
  $("refresh-button").disabled = true;
  try {
    const data = await loadWorkspace();
    if (session === state.session) applyWorkspace(data);
  } catch (error) {
    if (session !== state.session) return;
    if (error.status === 401) {
      disconnect();
      openConnect();
      formError(
        "connect-error",
        "Your token was rejected. Enter a valid token to reconnect.",
      );
    } else {
      $("error-notice").textContent =
        `${error.message} Showing the last successfully loaded data.`;
      $("error-notice").hidden = false;
      $("sync-dot").className = "status-dot amber";
      $("sync-label").textContent = "Updates interrupted";
    }
  } finally {
    if (session === state.session) {
      state.refreshing = false;
      $("refresh-button").disabled = !state.connected;
    }
  }
}

$("connect-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = $("api-token").value.trim();
  if (!token) return formError("connect-error", "Enter your API token.");
  state.token = token;
  const session = ++state.session;
  busyDialog("connect-dialog", true);
  formError("connect-error");
  $("connect-submit").textContent = "Connecting…";
  try {
    const data = await loadWorkspace();
    if (session !== state.session) return;
    state.connected = true;
    applyWorkspace(data);
    setConnectedControls();
    $("api-token").value = "";
    $("connect-dialog").close();
  } catch (error) {
    state.token = "";
    formError(
      "connect-error",
      error.status === 401
        ? "That token was rejected. Check your API token and try again."
        : error.message,
    );
  } finally {
    busyDialog("connect-dialog", false);
    $("connect-submit").textContent = "Connect to platform →";
  }
});

$("deploy-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const payload = {
    model_id: form.get("model_id").trim(),
    profile: form.get("profile"),
    dtype: form.get("dtype"),
    quantization: form.get("quantization") || null,
    max_model_len: Number(form.get("max_model_len")),
    target_concurrency: Number(form.get("target_concurrency")),
    gpu_memory_utilization: Number(form.get("gpu_memory_percent")) / 100,
  };
  const name = form.get("deployment_name").trim();
  if (name) payload.deployment_name = name;
  if (state.models.some((model) => model.name === name))
    return formError(
      "deploy-error",
      "That deployment name already exists. Choose a new name.",
    );
  if (
    !name &&
    state.models.some((model) => model.model_id === payload.model_id)
  )
    return formError(
      "deploy-error",
      "This model already has a deployment. Enter a unique deployment name to add another.",
    );
  busyDialog("deploy-dialog", true);
  formError("deploy-error");
  $("deploy-submit").textContent = "Scheduling deployment…";
  try {
    // Cache warming can take up to 30 minutes in a live cluster.
    const data = await api("/api/v1/models/deploy", {
      method: "POST",
      body: payload,
      timeout: 1860000,
    });
    $("deploy-dialog").close();
    $("deploy-form").reset();
    toast(`${data.deployment_name} accepted. Provisioning is in progress.`);
    location.hash = "deployments";
    await refreshWorkspace();
  } catch (error) {
    formError("deploy-error", error.message);
  } finally {
    busyDialog("deploy-dialog", false);
    $("deploy-submit").textContent = "Deploy model →";
  }
});

async function showDetails(name) {
  $("detail-title").textContent = name;
  $("detail-content").textContent = "Loading deployment details…";
  $("detail-dialog").showModal();
  const session = state.session;
  try {
    const model = await api(
      `/api/v1/models/${encodeURIComponent(name)}/status`,
    );
    if (
      session !== state.session ||
      !$("detail-dialog").open ||
      $("detail-title").textContent !== name
    )
      return;
    const plan = model.plan || {};
    const metadata = model.k8s_cr?.metadata || {};
    const mode =
      model.serving_mode ||
      plan.serving_mode ||
      metadata.labels?.["dynamo.nvidia.com/serving-mode"];
    const pairs = [
      ["Hugging Face model", model.model_id],
      ["Namespace", model.namespace || metadata.namespace],
      ["Serving mode", modeLabel(mode)],
      ["Endpoint ready", model.ready ? "Yes" : "No"],
      ["Weight dtype", plan.dtype],
      ["Quantization", model.plan ? (plan.quantization ?? "None") : null],
      ["Max context tokens", plan.max_model_len],
      [
        "GPU memory allocation",
        plan.gpu_memory_utilization
          ? `${Math.round(plan.gpu_memory_utilization * 100)}%`
          : null,
      ],
    ];
    const cliques = [
      ["worker_clique", "Worker"],
      ["prefill_clique", "Prefill"],
      ["decode_clique", "Decode"],
    ].filter(([key]) => plan[key]);
    const cliqueHtml = cliques.length
      ? `<h3 class="detail-section-title">Auto-tuned parallelism</h3>${cliques.map(([key, label]) => `<div class="clique"><strong>${label}</strong><span>${escapeHtml(plan[key].replicas)} replicas · TP ${escapeHtml(plan[key].tensor_parallel_size)} · PP ${escapeHtml(plan[key].pipeline_parallel_size)}</span></div>`).join("")}`
      : "";
    $("detail-content").innerHTML =
      `${statusBadge(model)}<dl class="detail-grid">${pairs
        .filter(([, value]) => value !== undefined && value !== null)
        .map(
          ([label, value]) =>
            `<div><dt>${label}</dt><dd>${escapeHtml(value)}</dd></div>`,
        )
        .join(
          "",
        )}</dl>${cliqueHtml}${model.error ? `<p class="form-error">${escapeHtml(model.error)}</p>` : ""}<details class="json-details"><summary>View API response</summary><pre>${escapeHtml(JSON.stringify(model, null, 2))}</pre></details>`;
  } catch (error) {
    if (session === state.session)
      $("detail-content").textContent = error.message;
  }
}

function confirmDelete(name) {
  deleteTarget = name;
  $("delete-name").textContent = name;
  formError("delete-error");
  $("delete-dialog").showModal();
}

$("delete-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!deleteTarget) return;
  busyDialog("delete-dialog", true);
  formError("delete-error");
  $("delete-submit").textContent = "Requesting deletion…";
  try {
    await api(`/api/v1/models/${encodeURIComponent(deleteTarget)}`, {
      method: "DELETE",
    });
    $("delete-dialog").close();
    toast(
      `Deletion requested for ${deleteTarget}. The control plane will drain and remove the workload.`,
    );
    deleteTarget = null;
    await refreshWorkspace();
  } catch (error) {
    formError("delete-error", error.message);
  } finally {
    busyDialog("delete-dialog", false);
    $("delete-submit").textContent = "Delete deployment";
  }
});

$("deployment-rows").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button || button.disabled) return;
  if (button.dataset.action === "details") showDetails(button.dataset.name);
  if (button.dataset.action === "delete") confirmDelete(button.dataset.name);
});
$("model-search").addEventListener("input", renderModels);
$("status-filter").addEventListener("change", renderModels);
$("refresh-button").addEventListener("click", refreshWorkspace);
$("new-deployment-button").addEventListener("click", openDeploy);
$("connection-button").addEventListener("click", () => {
  if (state.connected) {
    disconnect();
    toast("Disconnected. Your token has been cleared.");
  } else openConnect();
});
$("empty-action").addEventListener("click", () => {
  if (!state.connected) return openConnect();
  if (state.models.length) {
    $("model-search").value = "";
    $("status-filter").value = "all";
    renderModels();
  } else openDeploy();
});
document
  .querySelectorAll("[data-close]")
  .forEach((button) =>
    button.addEventListener("click", () => $(button.dataset.close).close()),
  );
document.querySelectorAll("dialog").forEach((dialog) => {
  dialog.addEventListener("cancel", (event) => {
    if (dialog.dataset.busy === "true") event.preventDefault();
  });
  dialog.addEventListener("close", () => {
    if (dialog.id === "connect-dialog") $("api-token").value = "";
  });
});

function navigate() {
  const view = location.hash === "#gpus" ? "gpus" : "deployments";
  $("deployments-view").hidden = view !== "deployments";
  $("gpus-view").hidden = view !== "gpus";
  $("page-title").textContent =
    view === "gpus" ? "GPU inventory" : "Model deployments";
  $("page-description").textContent =
    view === "gpus"
      ? "Understand your cluster capacity before your next deployment."
      : "Deploy, monitor, and manage your inference workloads.";
  $("breadcrumb-view").textContent = $("page-title").textContent;
  document.querySelectorAll("[data-view]").forEach((link) => {
    const active = link.dataset.view === view;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}
window.addEventListener("hashchange", navigate);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshWorkspace();
});
setInterval(() => {
  if (!document.hidden) refreshWorkspace();
}, 10000);

async function checkService() {
  try {
    const response = await fetch("/health", {
      cache: "no-store",
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw new Error("Service unavailable");
    const health = await response.json();
    const mock = health.mode === "mock";
    $("mode-badge").textContent = mock
      ? "Mock environment"
      : "Live environment";
    $("mode-badge").className = `badge ${mock ? "mock" : "muted"}`;
    $("mock-notice").hidden = !mock;
    $("mock-login-hint").hidden = !mock;
  } catch {
    $("mode-badge").textContent = "Service unavailable";
  }
}

navigate();
setConnectedControls();
resetWorkspace();
checkService();
