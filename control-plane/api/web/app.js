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
  user: null,
  csrf: "",
  authConfig: null,
  workload: null,
  workloadRequest: 0,
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
    const headers = state.token
      ? { Authorization: `Bearer ${state.token}` }
      : {};
    if (!["GET", "HEAD"].includes(method) && state.csrf)
      headers["X-CSRF-Token"] = state.csrf;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, {
      method,
      headers,
      signal: controller.signal,
      cache: "no-store",
      credentials: "same-origin",
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
    if (busy) {
      element.dataset.wasDisabled = String(element.disabled);
      element.disabled = true;
    } else {
      element.disabled = element.dataset.wasDisabled === "true";
      delete element.dataset.wasDisabled;
    }
  });
  // Keep the connection in place until an in-flight mutation finishes.
  $("connection-button").disabled = busy;
}

async function openConnect() {
  if (!state.authConfig) await checkAuthentication();
  formError("connect-error");
  $("api-token").value = "";
  $("admin-password").value = "";
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
  $("connection-label").textContent = state.connected ? "Sign out" : "Sign in";
  $("user-label").hidden = !state.user;
  $("user-label").textContent = state.user?.username || "";
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
  resetOperations();
}

function disconnect() {
  state.session += 1;
  for (const controller of pendingRequests) controller.abort();
  state.token = "";
  state.connected = false;
  state.models = [];
  state.inventory = null;
  state.refreshing = false;
  state.user = null;
  state.csrf = "";
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
  updateWorkloadChoices();
  refreshOperations();
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
        "Your session expired or access was revoked. Sign in again.",
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
  const passwordLogin = state.authConfig?.login_enabled;
  if (!passwordLogin) {
    const token = $("api-token").value.trim();
    if (!token)
      return formError("connect-error", "Enter your development API token.");
    state.token = token;
  }
  const session = ++state.session;
  busyDialog("connect-dialog", true);
  formError("connect-error");
  $("connect-submit").textContent = "Signing in…";
  try {
    if (passwordLogin) {
      const identity = await api("/auth/login", {
        method: "POST",
        body: {
          username: $("admin-username").value.trim(),
          password: $("admin-password").value,
        },
      });
      state.user = identity;
      state.csrf = identity.csrf_token;
      state.connected = true;
      setConnectedControls();
      $("connect-dialog").close();
      await refreshWorkspace();
      return;
    }
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
        ? passwordLogin
          ? "Invalid username or password."
          : "That development token was rejected."
        : error.message,
    );
  } finally {
    busyDialog("connect-dialog", false);
    $("admin-password").value = "";
    $("connect-submit").textContent = passwordLogin
      ? "Sign in →"
      : "Connect to mock environment →";
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
    signOut();
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
    if (dialog.id === "connect-dialog") {
      $("api-token").value = "";
      $("admin-password").value = "";
    }
  });
});

function navigate() {
  const view = ["gpus", "metrics", "kubernetes"].includes(
    location.hash.slice(1),
  )
    ? location.hash.slice(1)
    : "deployments";
  for (const name of ["deployments", "gpus", "metrics", "kubernetes"])
    $(`${name}-view`).hidden = name !== view;
  const headings = {
    deployments: [
      "Model deployments",
      "Deploy, monitor, and manage your inference workloads.",
    ],
    gpus: [
      "GPU inventory",
      "Understand your cluster capacity before your next deployment.",
    ],
    metrics: [
      "Live usage",
      "Monitor inference throughput, request load, and GPU usage.",
    ],
    kubernetes: [
      "Kubernetes",
      "Inspect your cluster and operate managed inference workloads.",
    ],
  };
  $("page-title").textContent = headings[view][0];
  $("page-description").textContent = headings[view][1];
  $("breadcrumb-view").textContent = $("page-title").textContent;
  document.querySelectorAll("[data-view]").forEach((link) => {
    const active = link.dataset.view === view;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  if (state.connected) refreshOperations();
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

let scaleTarget = null;
let replacementTarget = null;
let logsTarget = null;
let metricsRequest = 0;

async function checkAuthentication() {
  try {
    const response = await fetch("/auth/config", {
      cache: "no-store",
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw new Error("Authentication service unavailable.");
    state.authConfig = await response.json();
    const passwordLogin = state.authConfig.login_enabled;
    const development = !passwordLogin && state.authConfig.mock_token_enabled;
    $("username-field").hidden = !passwordLogin;
    $("password-field").hidden = !passwordLogin;
    $("login-session-help").hidden = !passwordLogin;
    $("admin-username").disabled = !passwordLogin;
    $("admin-password").disabled = !passwordLogin;
    $("api-token").disabled = !development;
    $("development-access").hidden = !development;
    $("login-setup-notice").hidden = passwordLogin || development;
    $("connect-submit").disabled = !passwordLogin && !development;
    $("connect-submit").textContent = passwordLogin
      ? "Sign in →"
      : development
        ? "Connect to mock environment →"
        : "Sign-in unavailable";
  } catch {
    state.authConfig = { login_enabled: false, mock_token_enabled: false };
    $("connect-submit").disabled = true;
    $("login-setup-notice").hidden = false;
    $("login-setup-notice").textContent =
      "The authentication service is unavailable.";
  }
}

async function restoreSession() {
  try {
    const identity = await api("/auth/session");
    state.user = identity;
    state.csrf = identity.csrf_token;
    state.connected = true;
    setConnectedControls();
    await refreshWorkspace();
  } catch {
    /* A new tab can remain signed out without presenting an error. */
  }
}

async function signOut() {
  $("connection-button").disabled = true;
  try {
    if (state.user) await api("/auth/logout", { method: "POST" });
    disconnect();
    toast("Signed out. Your session has been revoked.");
  } catch (error) {
    if (error.status === 401) disconnect();
    else {
      $("error-notice").textContent = `Sign-out failed: ${error.message}`;
      $("error-notice").hidden = false;
    }
  } finally {
    $("connection-button").disabled = false;
  }
}

function resetOperations() {
  state.workload = null;
  state.workloadRequest += 1;
  metricsRequest += 1;
  $("workload-select").innerHTML =
    '<option value="">Select a deployment</option>';
  $("workload-select").disabled = true;
  $("node-cards").replaceChildren();
  $("component-controls").replaceChildren();
  $("pod-rows").replaceChildren();
  $("pod-count").textContent = "0";
  $("pod-empty").hidden = false;
  $("workload-notice").hidden = false;
  $("workload-notice").textContent =
    "Select a model deployment to inspect its workload.";
  $("nodes-notice").hidden = true;
  $("usage-cards").replaceChildren();
  $("metrics-notice").hidden = false;
  $("metrics-notice").textContent = "Sign in to view live usage.";
  for (const name of ["requests", "tokens"]) {
    $(`${name}-chart`).innerHTML =
      '<p class="chart-empty">No samples loaded</p>';
    $(`${name}-chart-status`).textContent = "No data";
  }
  $("metrics-updated").textContent =
    "Values are shown only when monitoring returns a usable sample.";
}

function updateWorkloadChoices() {
  const selected = $("workload-select").value;
  $("workload-select").innerHTML =
    '<option value="">Select a deployment</option>' +
    state.models
      .map(
        (model) =>
          `<option value="${escapeHtml(model.name)}">${escapeHtml(model.name)}</option>`,
      )
      .join("");
  if (state.models.some((model) => model.name === selected))
    $("workload-select").value = selected;
  else {
    state.workload = null;
    $("component-controls").replaceChildren();
    $("pod-rows").replaceChildren();
    $("pod-empty").hidden = false;
  }
  $("workload-select").disabled = !state.connected || !state.models.length;
}

function metricValue(metric) {
  if (!Number.isFinite(metric.value)) return "—";
  if (metric.unit === "seconds")
    return `${(metric.value * 1000).toLocaleString(undefined, { maximumFractionDigits: 0 })} ms`;
  return `${metric.value.toLocaleString(undefined, { maximumFractionDigits: metric.unit === "requests" ? 0 : 1 })}${metric.unit === "%" ? "%" : ""}`;
}

function chart(name, metric, windowMinutes) {
  const target = $(`${name}-chart`);
  const points = metric.history || [];
  const finite = points.filter(
    ([time, value]) => Number.isFinite(time) && Number.isFinite(value),
  );
  $(`${name}-chart-status`).textContent = finite.length
    ? "Prometheus"
    : "No samples";
  if (!finite.length) {
    target.innerHTML =
      '<p class="chart-empty">No usable samples in this time window</p>';
    return;
  }
  const end = points[points.length - 1][0];
  const start = end - windowMinutes * 60;
  const maximum = Math.max(1, ...finite.map(([, value]) => value)) * 1.08;
  const x = (time) => 50 + ((time - start) / (end - start)) * 530;
  const y = (value) => 190 - (Math.max(0, value) / maximum) * 160;
  const segments = [];
  let segment = [];
  let previous = null;
  const step = windowMinutes < 60 ? 15 : 60;
  for (const [time, value] of points) {
    if (
      !Number.isFinite(time) ||
      !Number.isFinite(value) ||
      (previous !== null && time - previous > step * 1.5)
    ) {
      if (segment.length) segments.push(segment);
      segment = [];
    }
    if (Number.isFinite(time) && Number.isFinite(value))
      segment.push(`${x(time).toFixed(2)},${y(value).toFixed(2)}`);
    previous = time;
  }
  if (segment.length) segments.push(segment);
  const date = (time) =>
    new Date(time * 1000).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
  target.innerHTML = `<svg viewBox="0 0 610 235" role="img" aria-label="${escapeHtml(metric.label)} over ${windowMinutes} minutes. Latest value ${escapeHtml(metricValue(metric))}."><line class="chart-gridline" x1="50" x2="580" y1="30" y2="30"/><line class="chart-gridline" x1="50" x2="580" y1="110" y2="110"/><line class="chart-gridline" x1="50" x2="580" y1="190" y2="190"/><text x="43" y="34" text-anchor="end">${maximum.toFixed(maximum < 10 ? 1 : 0)}</text><text x="43" y="114" text-anchor="end">${(maximum / 2).toFixed(maximum < 10 ? 1 : 0)}</text><text x="43" y="194" text-anchor="end">0</text>${segments.map((segment) => `<polyline class="chart-line" points="${segment.join(" ")}"/>`).join("")}<text x="50" y="220">${escapeHtml(date(start))}</text><text x="580" y="220" text-anchor="end">${escapeHtml(date(end))}</text></svg>`;
}

async function refreshMetrics() {
  const session = state.session;
  const request = ++metricsRequest;
  try {
    const data = await api(
      `/api/v1/metrics/usage?window_minutes=${$("metrics-window").value}`,
    );
    if (session !== state.session || request !== metricsRequest) return;
    const notices = {
      ready:
        "Live telemetry from Prometheus. Updated every 10 seconds while this view is open.",
      partial:
        "Some metrics have no usable samples. Missing values are shown as —.",
      unavailable: "Monitoring is unavailable. Check the telemetry connection.",
      not_configured:
        "Monitoring is not connected. Configure the Prometheus endpoint to enable live usage.",
    };
    $("metrics-notice").textContent =
      notices[data.status] || "Monitoring status is unknown.";
    $("usage-cards").innerHTML = Object.values(data.metrics)
      .map(
        (metric) =>
          `<article class="stat-card usage-card"><span class="stat-label">${escapeHtml(metric.label)}</span><div class="stat-value">${escapeHtml(metricValue(metric))}</div><span class="stat-caption">${metric.status === "ready" ? escapeHtml(metric.unit === "seconds" ? "p95 latency" : metric.unit) : metric.status === "partial" ? "Partial telemetry" : "No usable sample"}</span></article>`,
      )
      .join("");
    chart("requests", data.metrics.request_rate, data.window_minutes);
    chart("tokens", data.metrics.output_tokens, data.window_minutes);
    $("metrics-updated").textContent =
      `Source: Prometheus · ${data.namespace} · Checked ${new Date(data.collected_at).toLocaleTimeString()}`;
  } catch (error) {
    if (session === state.session && request === metricsRequest) {
      $("metrics-notice").textContent = error.message;
      $("usage-cards").replaceChildren();
      for (const name of ["requests", "tokens"])
        $(`${name}-chart`).innerHTML =
          '<p class="chart-empty">Monitoring request failed</p>';
    }
  }
}

async function refreshNodes() {
  const session = state.session;
  try {
    const nodes = await api("/api/v1/cluster/nodes");
    if (session !== state.session) return;
    $("nodes-notice").hidden = true;
    $("node-cards").innerHTML = nodes
      .map(
        (node) =>
          `<article class="gpu-card node-card"><div class="gpu-card-heading"><div><h3>${escapeHtml(node.name)}</h3><p>${escapeHtml(node.gpu_sku || "CPU node")}</p></div><span class="badge ${node.ready ? "ready" : "failed"}">${node.ready ? "Ready" : "Not ready"}</span></div><dl class="detail-grid"><div><dt>GPUs</dt><dd>${escapeHtml(node.gpu_count)}</dd></div><div><dt>Scheduling</dt><dd>${node.schedulable ? "Enabled" : "Cordoned"}</dd></div><div><dt>CPU capacity</dt><dd>${escapeHtml(node.cpu || "Unknown")}</dd></div><div><dt>Memory capacity</dt><dd>${escapeHtml(node.memory || "Unknown")}</dd></div></dl>${node.pressure.length ? `<p class="danger-text">${escapeHtml(node.pressure.join(", "))}</p>` : ""}</article>`,
      )
      .join("");
  } catch (error) {
    if (session === state.session) {
      $("nodes-notice").textContent = error.message;
      $("nodes-notice").hidden = false;
    }
  }
}

async function refreshWorkload() {
  const name = $("workload-select").value;
  const session = state.session;
  const request = ++state.workloadRequest;
  if (!name) {
    $("component-controls").replaceChildren();
    $("pod-rows").replaceChildren();
    $("pod-empty").hidden = false;
    $("pod-count").textContent = "0";
    return;
  }
  try {
    const workload = await api(
      `/api/v1/models/${encodeURIComponent(name)}/workload`,
    );
    if (
      session !== state.session ||
      request !== state.workloadRequest ||
      name !== $("workload-select").value
    )
      return;
    state.workload = workload;
    $("workload-notice").hidden = true;
    $("component-controls").innerHTML = workload.components
      .map(
        (component) =>
          `<article class="panel component-card"><div><h3>${escapeHtml(component.name)}</h3><p>${escapeHtml(component.desired_replicas)} desired replicas${component.current_replicas !== null ? ` · ${escapeHtml(component.current_replicas)} observed` : ""}</p><span class="stat-caption">${escapeHtml(component.gpu_per_replica)} GPUs per replica</span></div><button class="button secondary" data-component="${escapeHtml(component.name)}" ${!component.can_scale ? "disabled" : ""}>Change replicas</button>${component.blocked_reason ? `<p class="component-reason">${escapeHtml(component.blocked_reason)}</p>` : ""}</article>`,
      )
      .join("");
    $("pod-count").textContent = workload.pods.length;
    $("pod-empty").hidden = workload.pods.length > 0;
    $("pod-rows").innerHTML = workload.pods
      .map(
        (pod) =>
          `<tr><td><strong class="pod-name">${escapeHtml(pod.name)}</strong><span class="model-id">${escapeHtml(pod.component)}</span></td><td><span class="badge ${pod.ready ? "ready" : pod.phase === "Failed" ? "failed" : "provisioning"}">${pod.ready ? "Ready" : escapeHtml(pod.phase)}</span></td><td>${escapeHtml(pod.node || "Unscheduled")}</td><td>${escapeHtml(pod.restarts)}</td><td><div class="row-actions"><button class="text-button" data-pod-action="logs" data-pod="${escapeHtml(pod.name)}">Logs</button><button class="text-button delete" data-pod-action="replace" data-pod="${escapeHtml(pod.name)}">Replace</button></div></td></tr>`,
      )
      .join("");
  } catch (error) {
    if (session === state.session && request === state.workloadRequest) {
      $("workload-notice").hidden = false;
      $("workload-notice").textContent = error.message;
      $("component-controls").replaceChildren();
      $("pod-rows").replaceChildren();
      state.workload = null;
    }
  }
}

function refreshOperations() {
  if (!state.connected || document.hidden) return;
  if (location.hash === "#metrics") refreshMetrics();
  if (location.hash === "#kubernetes") {
    refreshNodes();
    refreshWorkload();
  }
}

$("metrics-window").addEventListener("change", () => {
  if (state.connected) refreshMetrics();
});
$("workload-select").addEventListener("change", refreshWorkload);
$("component-controls").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-component]");
  if (!button || button.disabled) return;
  scaleTarget = state.workload.components.find(
    (component) => component.name === button.dataset.component,
  );
  if (!scaleTarget) return;
  $("scale-description").textContent =
    `${$("workload-select").value} · ${scaleTarget.name} currently requests ${scaleTarget.desired_replicas} replicas. Each replica requires ${scaleTarget.gpu_per_replica} GPUs.`;
  $("scale-replicas").value = scaleTarget.desired_replicas;
  $("scale-replicas").min = scaleTarget.minimum_replicas;
  formError("scale-error");
  $("scale-dialog").showModal();
});
$("scale-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!scaleTarget) return;
  const name = $("workload-select").value;
  busyDialog("scale-dialog", true);
  try {
    await api(
      `/api/v1/models/${encodeURIComponent(name)}/components/${encodeURIComponent(scaleTarget.name)}/scale`,
      {
        method: "POST",
        body: {
          replicas: Number($("scale-replicas").value),
          resource_version: scaleTarget.resource_version,
        },
      },
    );
    $("scale-dialog").close();
    toast("Replica change accepted by the control plane.");
    await refreshWorkspace();
  } catch (error) {
    formError("scale-error", error.message);
  } finally {
    busyDialog("scale-dialog", false);
  }
});
$("pod-rows").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-pod-action]");
  if (!button || !state.workload) return;
  const pod = state.workload.pods.find(
    (pod) => pod.name === button.dataset.pod,
  );
  if (!pod) return;
  const deployment = $("workload-select").value;
  if (button.dataset.podAction === "replace") {
    replacementTarget = { deployment, ...pod };
    $("replacement-pod-name").textContent = pod.name;
    formError("replacement-error");
    $("replacement-dialog").showModal();
  } else {
    logsTarget = { deployment, ...pod };
    $("logs-title").textContent = pod.name;
    $("logs-container").innerHTML = pod.containers
      .map((name) => `<option>${escapeHtml(name)}</option>`)
      .join("");
    $("logs-dialog").showModal();
    loadLogs();
  }
});
$("replacement-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!replacementTarget) return;
  busyDialog("replacement-dialog", true);
  try {
    await api(
      `/api/v1/models/${encodeURIComponent(replacementTarget.deployment)}/pods/${encodeURIComponent(replacementTarget.name)}/evict`,
      { method: "POST", body: { uid: replacementTarget.uid } },
    );
    $("replacement-dialog").close();
    toast("Pod replacement requested. Kubernetes will reconcile the workload.");
    await refreshWorkload();
  } catch (error) {
    formError("replacement-error", error.message);
  } finally {
    busyDialog("replacement-dialog", false);
  }
});
async function loadLogs() {
  if (!logsTarget) return;
  const session = state.session;
  $("logs-content").textContent = "Loading container logs…";
  $("logs-refresh").disabled = true;
  try {
    const data = await api(
      `/api/v1/models/${encodeURIComponent(logsTarget.deployment)}/pods/${encodeURIComponent(logsTarget.name)}/logs?container=${encodeURIComponent($("logs-container").value)}&tail_lines=100`,
    );
    if (session === state.session && $("logs-dialog").open)
      $("logs-content").textContent =
        data.text || "No log lines were returned.";
  } catch (error) {
    if (session === state.session)
      $("logs-content").textContent = error.message;
  } finally {
    $("logs-refresh").disabled = false;
  }
}
$("logs-refresh").addEventListener("click", loadLogs);
$("logs-container").addEventListener("change", loadLogs);

navigate();
setConnectedControls();
resetWorkspace();
checkService();
checkAuthentication().then(restoreSession);
