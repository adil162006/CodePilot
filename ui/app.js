const API_BASE_KEY = "codepilot.apiBase";
const MAX_FILES = 100;
const MAX_UPLOAD_BYTES = 9 * 1024 * 1024;
const POLL_INTERVAL_MS = 1800;

const elements = {
  apiBase: document.querySelector("#apiBase"),
  checkConnection: document.querySelector("#checkConnection"),
  connectionStatus: document.querySelector("#connectionStatus"),
  connectionLabel: document.querySelector(".connection-label"),
  dropZone: document.querySelector("#dropZone"),
  fileInput: document.querySelector("#fileInput"),
  folderInput: document.querySelector("#folderInput"),
  browseFiles: document.querySelector("#browseFiles"),
  browseFolder: document.querySelector("#browseFolder"),
  selectedFiles: document.querySelector("#selectedFiles"),
  fileCount: document.querySelector("#fileCount"),
  clearFiles: document.querySelector("#clearFiles"),
  runWorkflow: document.querySelector("#runWorkflow"),
  uploadError: document.querySelector("#uploadError"),
  jobStatus: document.querySelector("#jobStatus"),
  workflowTitle: document.querySelector("#workflowTitle"),
  workflowDescription: document.querySelector("#workflowDescription"),
  pipelineSteps: [...document.querySelectorAll(".pipeline-step")],
  jobId: document.querySelector("#jobId"),
  approvalPanel: document.querySelector("#approvalPanel"),
  approvalNote: document.querySelector("#approvalNote"),
  patchList: document.querySelector("#patchList"),
  selectAllPatches: document.querySelector("#selectAllPatches"),
  rejectPatches: document.querySelector("#rejectPatches"),
  approvePatches: document.querySelector("#approvePatches"),
  responseCount: document.querySelector("#responseCount"),
  responseEmpty: document.querySelector("#responseEmpty"),
  responseContent: document.querySelector("#responseContent"),
  jobError: document.querySelector("#jobError"),
  outputCount: document.querySelector("#outputCount"),
  filesEmpty: document.querySelector("#filesEmpty"),
  fileResults: document.querySelector("#fileResults"),
  resultFileList: document.querySelector("#resultFileList"),
  previewPath: document.querySelector("#previewPath"),
  codePreview: document.querySelector("#codePreview code"),
  changedBadge: document.querySelector("#changedBadge"),
  copyFile: document.querySelector("#copyFile"),
  downloadFile: document.querySelector("#downloadFile"),
  omittedFiles: document.querySelector("#omittedFiles"),
  toast: document.querySelector("#toast"),
};

const state = {
  uploads: new Map(),
  originalContents: new Map(),
  outputs: [],
  selectedOutput: null,
  jobId: null,
  jobApiBase: null,
  pollTimer: null,
  pollInFlight: false,
  submitting: false,
  uploadLocked: false,
  loadedFilesJob: null,
  lastJobStatus: null,
  toastTimer: null,
};

const stages = [
  { key: "debugger", label: "Debugger" },
  { key: "code_quality", label: "Code quality" },
  { key: "documentation", label: "Documentation" },
];

function normalizeApiBase(value = elements.apiBase.value) {
  const url = new URL(value.trim());
  if (!(["http:", "https:"].includes(url.protocol))) {
    throw new Error("The backend address must use HTTP or HTTPS.");
  }
  return url.toString().replace(/\/$/, "");
}

function setConnection(connected, label) {
  const status = connected ? "is-online" : label === "Checking API" ? "is-checking" : "is-offline";
  elements.connectionStatus.className = `connection-badge ${status}`;
  elements.connectionLabel.textContent = label;
}

async function apiRequest(base, path, options = {}) {
  const response = await fetch(`${base}${path}`, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `Request failed (${response.status}).`);
  }
  return payload;
}

async function checkConnection() {
  setConnection(false, "Checking API");
  try {
    const base = normalizeApiBase();
    localStorage.setItem(API_BASE_KEY, base);
    await apiRequest(base, "/api/health");
    setConnection(true, "API connected");
  } catch (error) {
    setConnection(false, "API unavailable");
    if (error instanceof TypeError) {
      showToast("Could not reach the API. Start api.py and check the backend address.", true);
    } else {
      showToast(error.message, true);
    }
  }
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function fileExtension(path) {
  const name = path.split("/").pop() || "file";
  const extension = name.includes(".") ? name.split(".").pop() : "txt";
  return extension.slice(0, 5).toLowerCase();
}

function makeFileIcon(path, className = "file-type-icon") {
  const icon = document.createElement("span");
  icon.className = className;
  icon.textContent = fileExtension(path);
  icon.setAttribute("aria-hidden", "true");
  return icon;
}

function uploadSize() {
  let total = 0;
  for (const entry of state.uploads.values()) total += entry.file.size;
  return total;
}

function showUploadError(message) {
  elements.uploadError.textContent = message;
  elements.uploadError.hidden = !message;
}

function addFiles(fileList) {
  const incoming = [...fileList];
  if (!incoming.length) return;

  const next = new Map(state.uploads);
  for (const file of incoming) {
    const path = (file.webkitRelativePath || file.name).replace(/\\/g, "/");
    if (!path || path.split("/").some((part) => ["", ".", ".."].includes(part))) {
      showUploadError(`Unsupported file path: ${path || file.name}`);
      return;
    }
    next.set(path, { path, file });
  }

  if (next.size > MAX_FILES) {
    showUploadError(`A workflow can include up to ${MAX_FILES} files.`);
    return;
  }

  let total = 0;
  for (const entry of next.values()) total += entry.file.size;
  if (total > MAX_UPLOAD_BYTES) {
    showUploadError("Keep uploaded text files below 9 MB so the JSON request fits the backend limit.");
    return;
  }

  state.uploads = next;
  showUploadError("");
  renderSelectedFiles();
}

function renderSelectedFiles() {
  elements.fileCount.textContent = String(state.uploads.size);
  elements.clearFiles.disabled = state.uploads.size === 0 || state.uploadLocked;
  elements.runWorkflow.disabled = state.uploads.size === 0 || state.submitting || state.uploadLocked;
  elements.selectedFiles.replaceChildren();

  if (!state.uploads.size) {
    const empty = document.createElement("div");
    empty.className = "empty-files";
    empty.textContent = "Your selected files will appear here.";
    elements.selectedFiles.append(empty);
    return;
  }

  for (const [path, entry] of state.uploads) {
    const row = document.createElement("div");
    row.className = "upload-file-row";
    row.append(makeFileIcon(path));

    const copy = document.createElement("div");
    copy.className = "upload-file-copy";
    const name = document.createElement("strong");
    name.textContent = path;
    name.title = path;
    const size = document.createElement("span");
    size.textContent = formatBytes(entry.file.size);
    copy.append(name, size);

    const remove = document.createElement("button");
    remove.className = "remove-file";
    remove.type = "button";
    remove.textContent = "×";
    remove.title = `Remove ${path}`;
    remove.setAttribute("aria-label", `Remove ${path}`);
    remove.disabled = state.uploadLocked;
    remove.addEventListener("click", () => {
      state.uploads.delete(path);
      renderSelectedFiles();
    });

    row.append(copy, remove);
    elements.selectedFiles.append(row);
  }
}

function showToast(message, isError = false) {
  clearTimeout(state.toastTimer);
  elements.toast.textContent = message;
  elements.toast.className = `toast is-visible${isError ? " is-error" : ""}`;
  state.toastTimer = setTimeout(() => {
    elements.toast.className = "toast";
  }, 3600);
}

function setUploadLocked(locked) {
  state.uploadLocked = locked;
  elements.browseFiles.disabled = locked;
  elements.browseFolder.disabled = locked;
  elements.fileInput.disabled = locked;
  elements.folderInput.disabled = locked;
  renderSelectedFiles();
}

async function startWorkflow() {
  if (state.submitting || state.uploads.size === 0) return;

  state.submitting = true;
  elements.runWorkflow.disabled = true;
  showUploadError("");
  setUploadLocked(true);
  elements.runWorkflow.innerHTML = '<span class="button-spinner" aria-hidden="true"></span><span>Preparing files</span>';

  try {
    const base = normalizeApiBase();
    const fileEntries = [];
    const originalContents = new Map();

    for (const entry of state.uploads.values()) {
      const content = await entry.file.text();
      if (content.includes("\u0000")) {
        throw new Error(`${entry.path} looks like a binary file. Upload text or source files only.`);
      }
      fileEntries.push({ path: entry.path, content });
      originalContents.set(entry.path, content);
    }

    const body = JSON.stringify({ files: fileEntries });
    if (new Blob([body]).size > 9.5 * 1024 * 1024) {
      throw new Error("The encoded request is too large. Remove some files and try again.");
    }

    elements.runWorkflow.innerHTML = '<span class="button-spinner" aria-hidden="true"></span><span>Starting agents</span>';
    const created = await apiRequest(base, "/api/jobs", {
      method: "POST",
      body,
    });

    state.jobId = created.job_id;
    state.jobApiBase = base;
    state.originalContents = originalContents;
    state.outputs = [];
    state.selectedOutput = null;
    state.loadedFilesJob = null;
    state.lastJobStatus = null;
    elements.jobId.textContent = created.job_id;
    elements.jobId.title = created.job_id;
    elements.responseContent.replaceChildren();
    elements.responseContent.hidden = true;
    elements.responseEmpty.hidden = false;
    elements.jobError.hidden = true;
    elements.filesEmpty.hidden = false;
    elements.fileResults.hidden = true;
    elements.outputCount.textContent = "0 FILES";
    elements.responseCount.textContent = "RUNNING";
    setWorkflowStatus("queued", "queued");
    setConnection(true, "API connected");
    showToast("Workflow started. You can follow each agent stage here.");
    startPolling();
  } catch (error) {
    showUploadError(error.message || "Could not start the workflow.");
    setUploadLocked(false);
  } finally {
    state.submitting = false;
    elements.runWorkflow.innerHTML = '<span>Run full workflow</span><svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M4 10h11m0 0-4.5-4.5M15 10l-4.5 4.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" /></svg>';
    renderSelectedFiles();
  }
}

function statusLabel(status) {
  return {
    queued: "Queued",
    running: "Running",
    retrying: "Retrying",
    awaiting_approval: "Review patches",
    completed: "Completed",
    failed: "Failed",
  }[status] || "Ready";
}

function stageLabel(stage) {
  return stages.find((item) => item.key === stage)?.label || "the workflow";
}

function setWorkflowStatus(status, stage) {
  elements.jobStatus.className = `status-pill status-${status || "idle"}`;
  elements.jobStatus.querySelector("span:last-child").textContent = statusLabel(status);

  if (status === "completed") {
    elements.workflowTitle.textContent = "Workflow complete";
    elements.workflowDescription.textContent = "Your agent response and project files are ready.";
  } else if (status === "failed") {
    elements.workflowTitle.textContent = "Workflow stopped";
    elements.workflowDescription.textContent = "Review the error below and check the backend logs.";
  } else if (status === "awaiting_approval") {
    elements.workflowTitle.textContent = "Your review is needed";
    elements.workflowDescription.textContent = "Select validated patches to apply, or reject them all.";
  } else if (status === "retrying") {
    elements.workflowTitle.textContent = "Waiting for API capacity";
    elements.workflowDescription.textContent = "The current agent step will resume automatically.";
  } else if (status === "running") {
    elements.workflowTitle.textContent = `Working on ${stageLabel(stage)}`;
    elements.workflowDescription.textContent = "The workflow is running in the background.";
  } else if (status === "queued") {
    elements.workflowTitle.textContent = "Queued for analysis";
    elements.workflowDescription.textContent = "Your project is saved and waiting for an agent slot.";
  } else {
    elements.workflowTitle.textContent = "Waiting for a project";
    elements.workflowDescription.textContent = "Your agents will move through each stage in sequence.";
  }

  const currentIndex = stages.findIndex((item) => item.key === stage);
  for (const [index, step] of elements.pipelineSteps.entries()) {
    const key = step.dataset.stage;
    step.classList.remove("is-active", "is-done", "is-failed");
    const stateText = step.querySelector(".pipeline-state");

    if (status === "completed") {
      step.classList.add("is-done");
      stateText.textContent = "Complete";
    } else if (status === "failed" && index === currentIndex) {
      step.classList.add("is-failed");
      stateText.textContent = "Failed";
    } else if (currentIndex >= 0 && index < currentIndex) {
      step.classList.add("is-done");
      stateText.textContent = "Complete";
    } else if (currentIndex >= 0 && index === currentIndex && status !== "queued") {
      step.classList.add("is-active");
      stateText.textContent = status === "awaiting_approval" ? "Review" : status === "retrying" ? "Retrying" : "In progress";
    } else {
      stateText.textContent = "Waiting";
    }
  }
}

function renderMessages(job) {
  const messages = Array.isArray(job.messages) ? job.messages : [];
  elements.responseContent.replaceChildren();
  elements.jobError.hidden = !job.error;
  elements.jobError.textContent = job.error || "";

  if (!messages.length) {
    elements.responseContent.hidden = true;
    elements.responseEmpty.hidden = false;
    elements.responseCount.textContent = job.error ? "ERROR" : "NO RESPONSE";
    return;
  }

  elements.responseEmpty.hidden = true;
  elements.responseContent.hidden = false;
  elements.responseCount.textContent = `${messages.length} UPDATE${messages.length === 1 ? "" : "S"}`;

  for (const [index, message] of messages.entries()) {
    const card = document.createElement("article");
    card.className = "response-card";
    const heading = document.createElement("div");
    heading.className = "response-card-heading";
    const label = document.createElement("span");
    label.textContent = message.type || `Update ${index + 1}`;
    const number = document.createElement("span");
    number.textContent = String(index + 1).padStart(2, "0");
    heading.append(label, number);
    const content = document.createElement("pre");
    content.className = "response-card-content";
    content.textContent = typeof message.content === "string"
      ? message.content
      : JSON.stringify(message.content, null, 2);
    card.append(heading, content);
    elements.responseContent.append(card);
  }
}

function renderApproval(job) {
  const request = job.approval_request;
  if (job.status !== "awaiting_approval" || !request) {
    elements.approvalPanel.hidden = true;
    return;
  }

  elements.approvalPanel.hidden = false;
  elements.approvalNote.textContent = request.note || request.message || "Choose which validated patches to apply.";
  elements.patchList.replaceChildren();
  const patches = Array.isArray(request.patches) ? request.patches : [];

  for (const patch of patches) {
    const card = document.createElement("div");
    card.className = "patch-card";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = patch.bug_identifier || "";
    checkbox.id = `patch-${CSS.escape(checkbox.value)}`;
    checkbox.addEventListener("change", updateApprovalButton);

    const label = document.createElement("label");
    label.htmlFor = checkbox.id;
    const title = document.createElement("span");
    title.className = "patch-title";
    title.textContent = patch.bug_summary || patch.explanation || "Proposed code fix";
    const identifier = document.createElement("span");
    identifier.className = "patch-id";
    identifier.textContent = `${patch.bug_identifier || "Unknown issue"}${patch.target_file_path ? ` · ${patch.target_file_path}` : ""}`;
    label.append(title, identifier);

    if (patch.explanation && patch.bug_summary) {
      const description = document.createElement("span");
      description.className = "patch-description";
      description.textContent = patch.explanation;
      label.append(description);
    }

    if (patch.proposed_code_change) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.className = "patch-description";
      summary.textContent = "View proposed diff";
      const code = document.createElement("pre");
      code.className = "patch-code";
      code.textContent = patch.proposed_code_change;
      details.append(summary, code);
      label.append(details);
    }

    card.append(checkbox, label);
    elements.patchList.append(card);
  }
  updateApprovalButton();
}

function updateApprovalButton() {
  const selected = elements.patchList.querySelectorAll("input[type='checkbox']:checked").length;
  elements.approvePatches.disabled = selected === 0;
  elements.approvePatches.textContent = selected ? `Approve selected (${selected})` : "Approve selected";
}

async function submitApproval(approvedIdentifiers) {
  if (!state.jobId || !state.jobApiBase) return;
  elements.approvePatches.disabled = true;
  elements.rejectPatches.disabled = true;
  elements.selectAllPatches.disabled = true;
  try {
    await apiRequest(state.jobApiBase, `/api/jobs/${encodeURIComponent(state.jobId)}/approval`, {
      method: "POST",
      body: JSON.stringify({ approved_bug_identifiers: approvedIdentifiers }),
    });
    elements.approvalPanel.hidden = true;
    showToast(approvedIdentifiers.length ? "Patch decision sent. Workflow resumed." : "Patches rejected. Workflow resumed.");
    await pollJob();
  } catch (error) {
    showToast(error.message || "Could not send patch decision.", true);
    updateApprovalButton();
    elements.rejectPatches.disabled = false;
    elements.selectAllPatches.disabled = false;
  }
}

async function loadOutputFiles(jobId) {
  if (state.loadedFilesJob === jobId) return;
  state.loadedFilesJob = jobId;
  try {
    const payload = await apiRequest(state.jobApiBase, `/api/jobs/${encodeURIComponent(jobId)}/files`);
    state.outputs = (payload.files || []).map((file) => ({
      ...file,
      changed: state.originalContents.has(file.path)
        ? state.originalContents.get(file.path) !== file.content
        : true,
    }));
    renderOutputFiles(payload.omitted_files || []);
  } catch (error) {
    state.loadedFilesJob = null;
    showToast(`Could not load output files: ${error.message}`, true);
  }
}

function renderOutputFiles(omittedFiles = []) {
  elements.outputCount.textContent = `${state.outputs.length} FILE${state.outputs.length === 1 ? "" : "S"}`;
  elements.resultFileList.replaceChildren();
  elements.omittedFiles.hidden = omittedFiles.length === 0;
  elements.omittedFiles.textContent = omittedFiles.length
    ? `${omittedFiles.length} file${omittedFiles.length === 1 ? " was" : "s were"} omitted because it was binary or exceeded the output size limit.`
    : "";

  if (!state.outputs.length) {
    elements.fileResults.hidden = true;
    elements.filesEmpty.hidden = false;
    elements.filesEmpty.querySelector("strong").textContent = "No text files to preview";
    elements.filesEmpty.querySelector("span:last-child").textContent = "Check the job status for details or upload text-based source files.";
    return;
  }

  elements.filesEmpty.hidden = true;
  elements.fileResults.hidden = false;
  for (const file of state.outputs) {
    const button = document.createElement("button");
    button.className = `result-file-button${state.selectedOutput === file.path ? " is-selected" : ""}`;
    button.type = "button";
    button.title = file.path;
    button.append(makeFileIcon(file.path, "file-mini-icon"));
    const name = document.createElement("span");
    name.className = "result-file-name";
    name.textContent = file.path;
    button.append(name);
    if (file.changed) {
      const dot = document.createElement("span");
      dot.className = "file-change-dot";
      dot.title = "Changed by the workflow";
      button.append(dot);
    }
    button.addEventListener("click", () => {
      state.selectedOutput = file.path;
      renderOutputFiles(omittedFiles);
    });
    elements.resultFileList.append(button);
  }

  const selected = state.outputs.find((file) => file.path === state.selectedOutput) || state.outputs[0];
  state.selectedOutput = selected.path;
  elements.previewPath.textContent = selected.path;
  elements.previewPath.title = selected.path;
  elements.codePreview.textContent = selected.content;
  elements.changedBadge.hidden = !selected.changed;
  elements.copyFile.disabled = false;
  elements.downloadFile.disabled = false;
}

function renderJob(job) {
  state.lastJobStatus = job.status;
  elements.jobId.textContent = job.job_id || state.jobId || "—";
  elements.jobId.title = elements.jobId.textContent;
  setWorkflowStatus(job.status, job.stage);
  renderMessages(job);
  renderApproval(job);

  if (["completed", "failed"].includes(job.status)) {
    stopPolling();
    setUploadLocked(false);
    loadOutputFiles(job.job_id || state.jobId);
  }
}

async function pollJob() {
  if (state.pollInFlight || !state.jobId || !state.jobApiBase) return;
  state.pollInFlight = true;
  try {
    const job = await apiRequest(state.jobApiBase, `/api/jobs/${encodeURIComponent(state.jobId)}`);
    renderJob(job);
  } catch (error) {
    showToast(`Could not refresh job status: ${error.message}`, true);
  } finally {
    state.pollInFlight = false;
  }
}

function startPolling() {
  stopPolling();
  pollJob();
  state.pollTimer = setInterval(pollJob, POLL_INTERVAL_MS);
}

function stopPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
}

function clearUploads() {
  if (state.uploadLocked) return;
  state.uploads.clear();
  elements.fileInput.value = "";
  elements.folderInput.value = "";
  showUploadError("");
  renderSelectedFiles();
}

function bindEvents() {
  elements.checkConnection.addEventListener("click", checkConnection);
  elements.apiBase.addEventListener("change", () => {
    try {
      const base = normalizeApiBase();
      elements.apiBase.value = base;
      localStorage.setItem(API_BASE_KEY, base);
      checkConnection();
    } catch (error) {
      setConnection(false, "Invalid API URL");
      showToast(error.message, true);
    }
  });

  elements.browseFiles.addEventListener("click", () => elements.fileInput.click());
  elements.browseFolder.addEventListener("click", () => elements.folderInput.click());
  elements.fileInput.addEventListener("change", (event) => {
    addFiles(event.target.files);
    event.target.value = "";
  });
  elements.folderInput.addEventListener("change", (event) => {
    addFiles(event.target.files);
    event.target.value = "";
  });
  elements.dropZone.addEventListener("click", (event) => {
    if (!event.target.closest("button, input")) elements.fileInput.click();
  });
  elements.dropZone.addEventListener("keydown", (event) => {
    if ((event.key === "Enter" || event.key === " ") && !event.target.closest("button")) {
      event.preventDefault();
      elements.fileInput.click();
    }
  });
  for (const eventName of ["dragenter", "dragover"]) {
    elements.dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      elements.dropZone.classList.add("is-dragging");
    });
  }
  for (const eventName of ["dragleave", "drop"]) {
    elements.dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      elements.dropZone.classList.remove("is-dragging");
    });
  }
  elements.dropZone.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));
  elements.clearFiles.addEventListener("click", clearUploads);
  elements.runWorkflow.addEventListener("click", startWorkflow);

  elements.selectAllPatches.addEventListener("click", () => {
    elements.patchList.querySelectorAll("input[type='checkbox']").forEach((checkbox) => {
      checkbox.checked = true;
    });
    updateApprovalButton();
  });
  elements.approvePatches.addEventListener("click", () => {
    const approved = [...elements.patchList.querySelectorAll("input[type='checkbox']:checked")]
      .map((checkbox) => checkbox.value);
    submitApproval(approved);
  });
  elements.rejectPatches.addEventListener("click", () => submitApproval([]));

  elements.copyFile.addEventListener("click", async () => {
    const file = state.outputs.find((item) => item.path === state.selectedOutput);
    if (!file) return;
    try {
      await navigator.clipboard.writeText(file.content);
      showToast("File contents copied.");
    } catch {
      showToast("Clipboard access is unavailable in this browser context.", true);
    }
  });
  elements.downloadFile.addEventListener("click", () => {
    const file = state.outputs.find((item) => item.path === state.selectedOutput);
    if (!file) return;
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([file.content], { type: "text/plain;charset=utf-8" }));
    link.download = file.path.split("/").pop() || "output.txt";
    link.click();
    URL.revokeObjectURL(link.href);
  });
}

function initialize() {
  const savedBase = localStorage.getItem(API_BASE_KEY);
  if (savedBase) elements.apiBase.value = savedBase;
  bindEvents();
  renderSelectedFiles();
  checkConnection();
}

initialize();
