const defaultApiBase = "http://127.0.0.1:8787";
const planOptions = ["3个月内", "6个月内", "1年之内", "3年之内", "5年之内"];
const statusLabels = {
  pending: "等待处理",
  capturing: "抓取截图",
  captured: "截图完成",
  analyzing: "复盘中",
  analyzed: "复盘完成",
  capture_failed: "截图失败",
  analysis_failed: "复盘失败",
};
const workflowLabels = {
  running: "执行中",
  completed: "已完成",
  partial_completed: "部分完成",
  failed: "失败",
};
const stageLabels = {
  capture: "截图",
  analyze: "个股复盘",
  summary: "组合摘要",
  render: "文档渲染",
  done: "完成",
};

const state = {
  apiBase: localStorage.getItem("reviewApiBase") || defaultApiBase,
  reviewDate: "",
  pollingTimer: null,
  currentDocument: "",
  documentUrl: null,
  manualBusy: false,
  workflowRunning: false,
};

const elements = {
  healthDot: document.getElementById("healthDot"),
  healthText: document.getElementById("healthText"),
  reviewDate: document.getElementById("reviewDate"),
  holdingRows: document.getElementById("holdingRows"),
  addHolding: document.getElementById("addHolding"),
  forceRun: document.getElementById("forceRun"),
  apiBase: document.getElementById("apiBase"),
  reviewProvider: document.getElementById("reviewProvider"),
  form: document.getElementById("reviewForm"),
  startRun: document.getElementById("startRun"),
  resumeRun: document.getElementById("resumeRun"),
  renderDocument: document.getElementById("renderDocument"),
  statusPill: document.getElementById("statusPill"),
  stagePill: document.getElementById("stagePill"),
  progressBar: document.getElementById("progressBar"),
  progressText: document.getElementById("progressText"),
  progressCount: document.getElementById("progressCount"),
  taskList: document.getElementById("taskList"),
  eventList: document.getElementById("eventList"),
  refreshStatus: document.getElementById("refreshStatus"),
  documentOutput: document.getElementById("documentOutput"),
  documentRaw: document.getElementById("documentRaw"),
  showRendered: document.getElementById("showRendered"),
  showRaw: document.getElementById("showRaw"),
  downloadDocument: document.getElementById("downloadDocument"),
  toast: document.getElementById("toast"),
};

function init() {
  elements.reviewDate.value = formatLocalDate(new Date());
  elements.apiBase.value = state.apiBase;
  state.reviewDate = elements.reviewDate.value;

  elements.addHolding.addEventListener("click", () => addHoldingRow());
  elements.form.addEventListener("submit", createAutomaticRun);
  elements.resumeRun.addEventListener("click", resumeFailedTasks);
  elements.renderDocument.addEventListener("click", renderCurrentDocument);
  elements.refreshStatus.addEventListener("click", refreshStatusManually);
  elements.showRendered.addEventListener("click", () => switchDocumentView("rendered"));
  elements.showRaw.addEventListener("click", () => switchDocumentView("raw"));
  elements.apiBase.addEventListener("change", () => {
    const value = normalizeApiBase(elements.apiBase.value);
    elements.apiBase.value = value;
    state.apiBase = value;
    localStorage.setItem("reviewApiBase", value);
    checkHealth();
  });
  elements.reviewDate.addEventListener("change", () => {
    stopPolling();
    state.reviewDate = elements.reviewDate.value;
    resetMonitor();
    loadExistingStatus(false);
  });

  addHoldingRow("600519", "1500", "100", "1年之内");
  addHoldingRow();
  checkHealth();
  loadExistingStatus(false);
}

function normalizeApiBase(value) {
  const trimmed = String(value || defaultApiBase).trim().replace(/\/+$/, "");
  return /^https?:\/\//i.test(trimmed) ? trimmed : defaultApiBase;
}

function addHoldingRow(name = "", cost = "", shares = "", plan = "6个月内") {
  const row = document.createElement("div");
  row.className = "holding-row";

  const nameLabel = document.createElement("label");
  nameLabel.setAttribute("aria-label", "股票名称或代码");
  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.name = "name";
  nameInput.value = name;
  nameInput.placeholder = "名称 / 代码";
  nameInput.required = true;
  nameLabel.appendChild(nameInput);

  const costLabel = document.createElement("label");
  costLabel.setAttribute("aria-label", "成本价");
  const costInput = document.createElement("input");
  costInput.type = "number";
  costInput.name = "cost";
  costInput.value = cost;
  costInput.min = "0.01";
  costInput.step = "0.01";
  costInput.placeholder = "成本";
  costInput.required = true;
  costLabel.appendChild(costInput);

  const sharesLabel = document.createElement("label");
  sharesLabel.setAttribute("aria-label", "持股数量");
  const sharesInput = document.createElement("input");
  sharesInput.type = "number";
  sharesInput.name = "shares";
  sharesInput.value = shares;
  sharesInput.min = "1";
  sharesInput.step = "1";
  sharesInput.placeholder = "股数";
  sharesInput.required = true;
  sharesLabel.appendChild(sharesInput);

  const planLabel = document.createElement("label");
  planLabel.setAttribute("aria-label", "计划持有时间");
  const planSelect = document.createElement("select");
  planSelect.name = "plan";
  for (const option of planOptions) {
    const optionElement = document.createElement("option");
    optionElement.value = option;
    optionElement.textContent = option;
    planSelect.appendChild(optionElement);
  }
  planSelect.value = plan;
  planLabel.appendChild(planSelect);

  const removeButton = document.createElement("button");
  removeButton.type = "button";
  removeButton.className = "remove-button";
  removeButton.textContent = "×";
  removeButton.setAttribute("aria-label", "删除持仓");
  removeButton.addEventListener("click", () => {
    row.remove();
    if (!elements.holdingRows.children.length) addHoldingRow();
  });

  row.append(nameLabel, costLabel, sharesLabel, planLabel, removeButton);
  elements.holdingRows.appendChild(row);
}

function collectHoldings() {
  const holdings = [];
  const seenNames = new Set();
  const rows = Array.from(elements.holdingRows.querySelectorAll(".holding-row"));

  for (let index = 0; index < rows.length; index += 1) {
    const row = rows[index];
    const name = row.querySelector('[name="name"]').value.trim();
    const cost = row.querySelector('[name="cost"]').value.trim();
    const shares = row.querySelector('[name="shares"]').value.trim();
    const plan = row.querySelector('[name="plan"]').value;

    if (!name) throw new Error(`第 ${index + 1} 行请填写股票名称或代码`);
    if (seenNames.has(name.toLowerCase())) throw new Error(`持仓重复：${name}`);
    seenNames.add(name.toLowerCase());
    if (!cost || Number(cost) <= 0) throw new Error(`${name} 的成本价必须大于 0`);
    if (!shares || !/^\d+$/.test(shares) || Number(shares) <= 0) {
      throw new Error(`${name} 的持股数量必须是正整数`);
    }

    holdings.push({ name, cost: Number(cost), shares: Number(shares), plan });
  }

  if (!holdings.length) throw new Error("请至少添加一条持仓");
  return holdings;
}

async function request(path, options = {}) {
  const response = await fetch(`${state.apiBase}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const contentType = response.headers.get("Content-Type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    const message = typeof payload === "object" && payload.error
      ? payload.error
      : `请求失败：HTTP ${response.status}`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return payload;
}

async function createAutomaticRun(event) {
  event.preventDefault();
  try {
    const holdings = collectHoldings();
    const date = elements.reviewDate.value;
    if (!date) throw new Error("请选择复盘日期");

    setBusy(true, "正在创建自动复盘任务...");
    const result = await request("/api/review-runs", {
      method: "POST",
      body: JSON.stringify({
        date,
        holdings,
        provider: elements.reviewProvider.value,
        execute: true,
        force: elements.forceRun.checked,
      }),
    });
    state.reviewDate = date;
    renderStatus(result);
    startPolling();
    elements.forceRun.checked = false;
    showToast("任务已创建，后端开始自动复盘");
  } catch (error) {
    if (error.status === 409) {
      showToast("该日期已有任务；如确认覆盖请勾选“覆盖同日已有任务”。", true);
    } else {
      showToast(error.message, true);
    }
  } finally {
    setBusy(false);
  }
}

async function resumeFailedTasks() {
  try {
    if (!state.reviewDate) throw new Error("请先选择复盘日期");
    setBusy(true, "正在恢复失败任务...");
    const result = await request(`/api/review-runs/${state.reviewDate}/resume`, {
      method: "POST",
      body: JSON.stringify({
        retry_failed: true,
        execute: true,
        provider: elements.reviewProvider.value,
      }),
    });
    renderStatus(result);
    startPolling();
    showToast("已恢复失败任务");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function renderCurrentDocument() {
  try {
    if (!state.reviewDate) throw new Error("请先选择复盘日期");
    setBusy(true, "正在重新渲染文档...");
    const result = await request(`/api/review-runs/${state.reviewDate}/render`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    renderStatus(result);
    await loadDocument();
    showToast("文档已重新渲染");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function refreshStatusManually() {
  await loadExistingStatus(true);
}

async function loadExistingStatus(showError) {
  try {
    const result = await request(`/api/review-runs/${state.reviewDate}`);
    renderStatus(result);
    if (result.status === "running") startPolling();
    if (result.output_path) await loadDocument();
  } catch (error) {
    if (showError) showToast(error.message, true);
  }
}

function startPolling() {
  stopPolling();
  state.pollingTimer = window.setInterval(async () => {
    try {
      const result = await request(`/api/review-runs/${state.reviewDate}`);
      renderStatus(result);
      if (result.status !== "running") {
        stopPolling();
        if (result.output_path) await loadDocument();
        setBusy(false);
      }
    } catch (error) {
      stopPolling();
      showToast(`状态轮询失败：${error.message}`, true);
    }
  }, 2000);
}

function stopPolling() {
  if (state.pollingTimer) {
    window.clearInterval(state.pollingTimer);
    state.pollingTimer = null;
  }
}

function renderStatus(payload) {
  const status = payload.status || "unknown";
  state.workflowRunning = status === "running";
  const stage = payload.stage || "";
  elements.statusPill.textContent = workflowLabels[status] || status;
  elements.statusPill.className = `pill ${status}`;
  elements.stagePill.textContent = `阶段：${stageLabels[stage] || stage || "—"}`;

  const progress = payload.progress || { total: 0, analyzed: 0, failed: 0 };
  const finished = progress.analyzed + progress.failed;
  const percent = progress.total ? Math.round((finished / progress.total) * 100) : 0;
  elements.progressBar.style.width = status === "completed" ? "100%" : `${percent}%`;
  elements.progressCount.textContent = `${finished} / ${progress.total}`;
  elements.progressText.textContent = status === "running"
    ? `成功 ${progress.analyzed} · 失败 ${progress.failed} · 待处理 ${progress.remaining || 0}`
    : `成功 ${progress.analyzed} · 失败 ${progress.failed}`;

  renderTasks(payload.stocks || []);
  renderEvents(payload.events || []);
  syncControls();
}

function renderTasks(stocks) {
  elements.taskList.replaceChildren();
  if (!stocks.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "创建任务后，这里将显示每只股票的截图与 AI 状态。";
    elements.taskList.appendChild(empty);
    return;
  }

  for (const item of stocks) {
    const holding = item.holding || {};
    const card = document.createElement("article");
    card.className = "task-card";

    const top = document.createElement("div");
    top.className = "task-top";
    const name = document.createElement("div");
    name.className = "task-name";
    name.textContent = holding.name || "未知股票";
    const badge = document.createElement("span");
    badge.className = `pill ${item.status}`;
    badge.textContent = statusLabels[item.status] || item.status || "未知";
    top.append(name, badge);

    const meta = document.createElement("p");
    meta.className = "task-meta";
    meta.textContent = [
      holding.cost ? `成本 ${holding.cost}` : "",
      holding.shares ? `${holding.shares} 股` : "",
      holding.plan || "",
      item.individual_images ? `个股图 ${item.individual_images}` : "",
      item.board_images ? `板块图 ${item.board_images}` : "",
    ].filter(Boolean).join(" · ");

    card.append(top, meta);
    if (item.error) {
      const error = document.createElement("p");
      error.className = "task-error";
      error.textContent = item.error;
      card.appendChild(error);
    }
    elements.taskList.appendChild(card);
  }
}

function renderEvents(events) {
  elements.eventList.replaceChildren();
  for (const event of events.slice(-10).reverse()) {
    const item = document.createElement("li");
    item.textContent = `${event.at || ""} ${event.message || ""}`;
    if (event.level === "error") item.style.color = "var(--red)";
    elements.eventList.appendChild(item);
  }
}

async function loadDocument() {
  try {
    const markdown = await request(`/api/review-runs/${state.reviewDate}/document`);
    state.currentDocument = markdown;
    elements.documentRaw.textContent = markdown || "文档为空。";
    elements.documentOutput.replaceChildren();
    if (markdown) {
      elements.documentOutput.appendChild(renderMarkdown(markdown));
    } else {
      showEmptyDocument();
    }
    const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
    if (state.documentUrl) URL.revokeObjectURL(state.documentUrl);
    state.documentUrl = URL.createObjectURL(blob);
    elements.downloadDocument.href = state.documentUrl;
    elements.downloadDocument.download = `${state.reviewDate.replace(/-/g, "")}_持股个股复盘.md`;
  } catch (error) {
    showEmptyDocument(error.message);
  }
}

function showEmptyDocument(message = "任务完成后，最终复盘将显示在这里。") {
  elements.documentRaw.textContent = "";
  const empty = document.createElement("p");
  empty.className = "empty-state";
  empty.textContent = message;
  elements.documentOutput.replaceChildren(empty);
}

function switchDocumentView(mode) {
  elements.documentOutput.classList.toggle("hidden", mode === "raw");
  elements.documentRaw.classList.toggle("hidden", mode !== "raw");
}

function setBusy(busy, message) {
  state.manualBusy = busy;
  syncControls();
  elements.startRun.textContent = busy && message ? message : "一键自动复盘";
}

function syncControls() {
  const busy = state.manualBusy || state.workflowRunning;
  elements.startRun.disabled = busy;
  elements.resumeRun.disabled = busy;
  elements.renderDocument.disabled = busy;
  if (!state.manualBusy) elements.startRun.textContent = "一键自动复盘";
}

function formatLocalDate(value) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

async function checkHealth() {
  setHealth("checking", "检查 API 中");
  try {
    const payload = await request("/api/health", { method: "GET" });
    setHealth(payload.ok ? "online" : "offline", payload.ok ? "API 在线" : "API 异常");
  } catch (error) {
    setHealth("offline", "API 未连接");
  }
}

function setHealth(status, text) {
  elements.healthText.textContent = text;
  elements.healthDot.className = `health-dot ${status === "online" ? "online" : status === "offline" ? "offline" : ""}`;
}

function resetMonitor() {
  elements.statusPill.textContent = "未开始";
  elements.statusPill.className = "pill";
  elements.stagePill.textContent = "阶段：—";
  elements.progressBar.style.width = "0";
  elements.progressText.textContent = "等待任务创建";
  elements.progressCount.textContent = "0 / 0";
  elements.taskList.replaceChildren();
  const empty = document.createElement("div");
  empty.className = "empty-state";
  empty.textContent = "创建任务后，这里将显示每只股票的截图与 AI 状态。";
  elements.taskList.appendChild(empty);
  elements.eventList.replaceChildren();
  showEmptyDocument();
}

let toastTimer;
function showToast(message, isError = false) {
  window.clearTimeout(toastTimer);
  elements.toast.textContent = message;
  elements.toast.className = `toast show ${isError ? "error" : ""}`;
  toastTimer = window.setTimeout(() => {
    elements.toast.className = "toast";
  }, 4200);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderMarkdown(markdown) {
  const container = document.createElement("div");
  const lines = markdown.replace(/\r\n/g, "\n").split("\n");
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (line.startsWith("```")) {
      const codeLines = [];
      index += 1;
      while (index < lines.length && !lines[index].startsWith("```")) {
        codeLines.push(lines[index]);
        index += 1;
      }
      index += 1;
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      code.textContent = codeLines.join("\n");
      pre.appendChild(code);
      container.appendChild(pre);
      continue;
    }

    if (/^\s*\|.*\|\s*$/.test(line) && isTableSeparator(lines[index + 1] || "")) {
      const headers = splitTableRow(line);
      index += 2;
      const rows = [];
      while (index < lines.length && /^\s*\|.*\|\s*$/.test(lines[index])) {
        rows.push(splitTableRow(lines[index]));
        index += 1;
      }
      container.appendChild(buildTable(headers, rows));
      continue;
    }

    if (/^#{1,6}\s+/.test(line)) {
      const level = Math.min(line.match(/^#+/)[0].length, 6);
      const heading = document.createElement(`h${level}`);
      heading.innerHTML = renderInline(line.replace(/^#+\s+/, ""));
      container.appendChild(heading);
      index += 1;
      continue;
    }

    if (/^\s*(?:[-*+]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const list = document.createElement(ordered ? "ol" : "ul");
      while (index < lines.length && /^\s*(?:[-*+]|\d+[.)])\s+/.test(lines[index])) {
        const item = document.createElement("li");
        item.innerHTML = renderInline(lines[index].replace(/^\s*(?:[-*+]|\d+[.)])\s+/, ""));
        list.appendChild(item);
        index += 1;
      }
      container.appendChild(list);
      continue;
    }

    if (/^\s*(?:---|\*\*\*)\s*$/.test(line)) {
      container.appendChild(document.createElement("hr"));
      index += 1;
      continue;
    }

    if (!line.trim()) {
      index += 1;
      continue;
    }

    const paragraphLines = [];
    while (index < lines.length && lines[index].trim() && !/^(#{1,6}\s|```|\s*\|)/.test(lines[index])) {
      paragraphLines.push(lines[index]);
      index += 1;
    }
    const paragraph = document.createElement("p");
    paragraph.innerHTML = renderInline(paragraphLines.join("\n"));
    container.appendChild(paragraph);
  }

  return container;
}

function isTableSeparator(line) {
  return /^\s*\|?\s*:?-{3,}\s*(?:\|\s*:?-{3,}\s*)+\|?\s*$/.test(line);
}

function splitTableRow(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}

function buildTable(headers, rows) {
  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const headerRow = document.createElement("tr");
  for (const header of headers) {
    const cell = document.createElement("th");
    cell.innerHTML = renderInline(header);
    headerRow.appendChild(cell);
  }
  thead.appendChild(headerRow);
  const tbody = document.createElement("tbody");
  for (const row of rows) {
    const tr = document.createElement("tr");
    for (const value of row) {
      const cell = document.createElement("td");
      cell.innerHTML = renderInline(value);
      tr.appendChild(cell);
    }
    tbody.appendChild(tr);
  }
  table.append(thead, tbody);
  return table;
}

function renderInline(value) {
  const escaped = escapeHtml(value);
  return escaped
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/\n/g, "<br>");
}

document.addEventListener("DOMContentLoaded", init);
