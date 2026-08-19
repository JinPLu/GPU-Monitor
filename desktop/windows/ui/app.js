(() => {
  "use strict";

  const view = {
    snapshot: null,
    current: null,
    selectedEndpointID: null,
    filter: "all",
    sort: { key: "free", direction: "desc" },
  };
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));
  const byID = (id) => document.getElementById(id);
  const escapeHTML = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
  const clamp = (value, lower = 0, upper = 100) => Math.min(Math.max(Number(value) || 0, lower), upper);
  const percent = (value) => value === null || value === undefined || Number.isNaN(Number(value))
    ? "—" : `${Math.round(clamp(value))}%`;
  const plural = (count, unit) => `${Number(count) || 0} ${unit}`;

  function bridge() {
    return window.pywebview && window.pywebview.api;
  }

  async function call(method, ...args) {
    if (!bridge() || typeof bridge()[method] !== "function") {
      return { ok: false, error: { code: "desktop_bridge_unavailable", message: "Windows 桌面桥接尚未就绪。" } };
    }
    try {
      return await bridge()[method](...args);
    } catch (_) {
      return { ok: false, error: { code: "desktop_bridge_failed", message: "Windows 桌面桥接没有返回有效结果。" } };
    }
  }

  function showNotice(message, isError = false) {
    const notice = byID("notice");
    notice.textContent = message;
    notice.classList.toggle("error", isError);
    notice.hidden = false;
    window.clearTimeout(showNotice.timer);
    showNotice.timer = window.setTimeout(() => { notice.hidden = true; }, isError ? 7000 : 4600);
  }

  function failure(result) {
    const error = result && result.error;
    showNotice(error && error.message ? error.message : "操作没有完成。", true);
  }

  function stateCurrent(envelope) {
    return envelope && envelope.data && envelope.data.current ? envelope.data.current : null;
  }

  function endpointGPUs(endpointID) {
    return (view.current?.gpus || []).filter((gpu) => gpu.endpoint_id === endpointID);
  }

  function recentGPUValue(gpu, key) {
    const telemetry = gpu.telemetry || {};
    const recent = telemetry.recent_average || {};
    const value = recent[key];
    return typeof value === "number" ? value : null;
  }

  function average(values) {
    const present = values.filter((value) => typeof value === "number" && Number.isFinite(value));
    return present.length ? present.reduce((total, value) => total + value, 0) / present.length : null;
  }

  function hostMetric(endpoint, key) {
    const recent = endpoint.host_telemetry?.recent_average || {};
    const value = recent[key];
    if (typeof value !== "number") return null;
    return key === "cpu_load_fraction" ? value * 100 : value;
  }

  function gpuMemoryPercent(gpu) {
    const used = gpu.telemetry?.memory_used_mib;
    const total = gpu.total_vram_mib;
    if (typeof used !== "number" || typeof total !== "number" || total <= 0) return null;
    return used * 100 / total;
  }

  function gpuMemoryLabel(gpu) {
    const used = gpu.telemetry?.memory_used_mib;
    const total = gpu.total_vram_mib;
    if (typeof used !== "number" || typeof total !== "number" || total <= 0) return "—";
    return `${Math.round(used / 1024)} / ${Math.max(1, Math.round(total / 1024))} GB`;
  }

  function isAvailable(gpu) {
    return gpu.publicly_available === true || gpu.state === "AVAILABLE";
  }

  function isKeepalive(gpu) {
    return gpu.keepalive?.actual === "ON" || gpu.keepalive?.state === "ON" || gpu.state === "KEEPALIVE";
  }

  function isErrorGPU(gpu) {
    return ["UNKNOWN_STALE", "UNKNOWN_RECOVERING", "UNHEALTHY", "CONFLICT", "ORPHANED_BUSY"].includes(gpu.state);
  }

  function gpuStatus(gpu) {
    if (isErrorGPU(gpu)) return { label: "错误", color: "#ef4d57" };
    if (isKeepalive(gpu)) return { label: "占卡", color: "#7e6ced" };
    if (isAvailable(gpu)) return { label: "空闲", color: "#39b967" };
    return { label: "繁忙", color: "#e8ad00" };
  }

  function endpointStatus(endpoint) {
    const monitor = endpoint.monitor || {};
    if (monitor.status === "ONLINE") return { label: "在线", className: "online" };
    if (endpoint.enabled === false || endpoint.lifecycle_state === "draining") return { label: "已暂停", className: "disabled" };
    return { label: "连接失败", className: "error" };
  }

  function endpointSsh(endpoint) {
    return `ssh -p ${endpoint.port || 22} ${endpoint.ssh_user || "root"}@${endpoint.host || "—"}`;
  }

  function endpointDetails(endpoint) {
    const gpus = endpointGPUs(endpoint.id);
    const leaseGPU = gpus.find((gpu) => gpu.lease && (gpu.lease.project_id || gpu.lease.task_ref));
    return {
      endpoint,
      gpus,
      free: gpus.filter(isAvailable).length,
      gpuUtil: average(gpus.map((gpu) => recentGPUValue(gpu, "gpu_utilization_pct"))),
      memory: average(gpus.map((gpu) => recentGPUValue(gpu, "memory_used_pct"))),
      cpu: hostMetric(endpoint, "cpu_load_fraction"),
      systemMemory: hostMetric(endpoint, "memory_used_pct"),
      project: leaseGPU?.lease?.project_id || "—",
      task: leaseGPU?.lease?.task_ref || (gpus.length ? "无 GPU 任务" : "CPU 节点"),
      status: endpointStatus(endpoint),
    };
  }

  function metricTone(value) {
    if (value === null || value === undefined) return "";
    if (value >= 80) return "high";
    if (value >= 60) return "medium";
    return "";
  }

  function metric(value) {
    return `<div class="metric ${metricTone(value)}"><span>${percent(value)}</span><span class="metric-bar"><i style="--value:${clamp(value)}%"></i></span></div>`;
  }

  function filterDetails(details) {
    const query = byID("search").value.trim().toLowerCase();
    const content = [endpointSsh(details.endpoint), details.project, details.task, ...details.gpus.map((gpu) => gpu.name)].join(" ").toLowerCase();
    if (query && !content.includes(query)) return false;
    if (view.filter === "available") return details.free > 0;
    if (view.filter === "busy") return details.gpus.some((gpu) => !isAvailable(gpu) && !isKeepalive(gpu) && !isErrorGPU(gpu));
    if (view.filter === "keepalive") return details.gpus.some(isKeepalive);
    if (view.filter === "error") return details.status.className === "error" || details.gpus.some(isErrorGPU);
    return true;
  }

  function sortDetails(items) {
    const direction = view.sort.direction === "asc" ? 1 : -1;
    const key = view.sort.key;
    const value = (item) => {
      if (key === "server") return endpointSsh(item.endpoint).toLowerCase();
      if (key === "task") return `${item.project} ${item.task}`.toLowerCase();
      if (key === "gpu") return item.gpus[0]?.name || "";
      if (key === "free") return item.free;
      return item[key] ?? -1;
    };
    return items.sort((left, right) => {
      const leftValue = value(left); const rightValue = value(right);
      if (typeof leftValue === "string") return direction * leftValue.localeCompare(rightValue, "zh-CN");
      return direction * (Number(leftValue) - Number(rightValue));
    });
  }

  function renderSummary() {
    const summary = view.current?.summary || {};
    byID("summary-servers").textContent = summary.total_servers ?? 0;
    byID("summary-gpus").textContent = summary.total_gpus ?? 0;
    byID("summary-free").textContent = summary.available_gpus ?? 0;
    const models = new Map();
    (view.current?.gpus || []).forEach((gpu) => models.set(gpu.name || "未知 GPU", (models.get(gpu.name || "未知 GPU") || 0) + 1));
    byID("summary-models").textContent = models.size
      ? Array.from(models.entries()).map(([name, count]) => `${name} × ${count}`).join(" · ")
      : "尚未发现 GPU";
  }

  function renderRows() {
    const rows = byID("server-rows");
    if (!view.current) { rows.innerHTML = `<div class="empty-state">正在读取本机资源状态。</div>`; return; }
    const details = sortDetails((view.current.endpoints || []).map(endpointDetails).filter(filterDetails));
    if (!details.length) { rows.innerHTML = `<div class="empty-state">没有符合当前筛选条件的服务器。</div>`; return; }
    rows.innerHTML = details.map((item) => {
      const gpu = item.gpus[0];
      const model = gpu ? gpu.name : "无 GPU";
      const config = gpu ? `${plural(item.gpus.length, "张")} · ${Math.round((gpu.total_vram_mib || 0) / 1024)} GB/卡` : "CPU 节点";
      const selected = item.endpoint.id === view.selectedEndpointID ? "selected" : "";
      return `<div class="server-row ${selected}" data-endpoint-id="${escapeHTML(item.endpoint.id)}" role="row">
        <div class="endpoint-cell"><div class="primary-line"><i class="status-dot ${item.status.className}"></i>${escapeHTML(endpointSsh(item.endpoint))}</div><div class="secondary-line">${escapeHTML(item.status.label)} · ${escapeHTML(item.endpoint.workspace_path || "未设置工作区")}</div></div>
        <div class="task-cell"><div class="primary-line">${escapeHTML(item.project)}</div><div class="secondary-line">${escapeHTML(item.task)}</div></div>
        <div class="gpu-cell"><div class="primary-line">${escapeHTML(model)}</div><div class="secondary-line">${escapeHTML(config)}</div></div>
        <div class="free-count">${item.gpus.length ? `${item.free}/${item.gpus.length}` : "—"}</div>
        ${metric(item.gpuUtil)}${metric(item.memory)}${metric(item.cpu)}${metric(item.systemMemory)}
        <button class="detail-open" type="button" data-open-detail="${escapeHTML(item.endpoint.id)}" aria-label="查看 ${escapeHTML(item.endpoint.id)} 详情">›</button>
      </div>`;
    }).join("");
  }

  function usageRecords() {
    const claims = view.current?.resource_claims || [];
    const activeClaims = claims.filter((claim) => String(claim.state || "").toLowerCase() === "active");
    if (activeClaims.length) return activeClaims.map((claim) => ({
      project: claim.project_id || "—",
      task: claim.task_ref || "—",
      state: claim.runtime_state || claim.state || "—",
      quantities: claim.quantities || {},
    }));
    return (view.current?.leases || []).filter((lease) => lease.state !== "RELEASED").map((lease) => ({
      project: lease.project_id || "—",
      task: lease.task_ref || "—",
      state: lease.runtime_state || lease.state || "—",
      quantities: { gpu_count: (lease.gpu_ids || []).length },
    }));
  }

  function renderUsage() {
    const records = usageRecords();
    byID("usage-projects").textContent = new Set(records.map((item) => item.project)).size;
    byID("usage-tasks").textContent = records.length;
    byID("usage-gpus").textContent = records.reduce((total, item) => total + (Number(item.quantities.gpu_count) || 0), 0);
    const rows = byID("usage-rows");
    if (!records.length) { rows.innerHTML = `<div class="empty-state">当前没有资源分配。</div>`; return; }
    rows.innerHTML = records.map((item) => {
      const quantities = item.quantities;
      const memory = Number(quantities.memory_mib) || 0;
      const cpu = Number(quantities.cpu_cores) || 0;
      const gpu = Number(quantities.gpu_count) || 0;
      return `<article class="usage-row"><div class="usage-project"><strong>${escapeHTML(item.project)}</strong><span>当前项目</span></div><div class="usage-task"><strong>${escapeHTML(item.task)}</strong><span>${escapeHTML(item.state)}</span></div><div class="usage-quantity"><strong>${gpu}</strong><span>GPU</span></div><div class="usage-quantity"><strong>${cpu || "—"}</strong><span>CPU 核</span></div><div class="usage-quantity"><strong>${memory ? `${Math.round(memory / 1024)} GiB` : "—"}</strong><span>内存</span></div></article>`;
    }).join("");
  }

  function renderUpdatedAt() {
    const time = view.snapshot?.server_time;
    if (!time) { byID("updated-at").textContent = "尚未收到资源状态"; return; }
    const date = new Date(time);
    byID("updated-at").textContent = Number.isNaN(date.getTime()) ? "已更新" : `更新于 ${date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}`;
  }

  function render() {
    renderUpdatedAt(); renderSummary(); renderRows(); renderUsage();
    $$(".table-head button").forEach((button) => button.classList.toggle("active", button.dataset.sort === view.sort.key));
  }

  async function refresh({ quiet = false } = {}) {
    const button = byID("refresh"); button.disabled = true;
    const result = await call("snapshot");
    button.disabled = false;
    if (!result.ok) { failure(result); byID("connection-dot").className = "live-dot error"; byID("connection-label").textContent = "本机服务不可用"; return; }
    view.snapshot = result.data;
    view.current = stateCurrent(result.data);
    if (!view.current) { showNotice("本机服务返回的资源快照无法读取。", true); return; }
    byID("connection-dot").className = "live-dot online";
    byID("connection-label").textContent = "本机服务已连接";
    render();
    if (!quiet) showNotice("资源状态已刷新。");
  }

  function memoryColor(value) { return value === null ? "#9aa1aa" : value >= 80 ? "#ef4d57" : value >= 60 ? "#e8ad00" : "#39b967"; }

  function statusCounts(gpus) {
    const counts = { "空闲": 0, "占卡": 0, "繁忙": 0, "错误": 0 };
    gpus.forEach((gpu) => { counts[gpuStatus(gpu).label] += 1; });
    return Object.entries(counts).map(([label, count]) => `<span class="status-chip" style="--status-color:${({ "空闲": "#39b967", "占卡": "#7e6ced", "繁忙": "#e8ad00", "错误": "#ef4d57" })[label]}"><i></i>${label} <strong>${count}</strong></span>`).join("");
  }

  function gpuCard(gpu) {
    const value = gpuMemoryPercent(gpu); const status = gpuStatus(gpu); const ring = memoryColor(value);
    return `<article class="gpu-card"><div class="memory-ring" style="--percent:${clamp(value)}%;--ring-color:${ring}"><span>${escapeHTML(gpu.gpu_index ?? "?")}</span></div><div class="gpu-card-info"><div class="gpu-card-title"><strong>GPU ${escapeHTML(gpu.gpu_index ?? "?")}</strong><span class="state-badge" style="--state-color:${status.color}">${status.label}</span></div><p>${escapeHTML(gpuMemoryLabel(gpu))}</p></div><div class="gpu-card-value" style="--ring-color:${ring}">${percent(value)}</div></article>`;
  }

  function valuesForChart(history, kind) {
    const data = history?.data || history || {};
    if (kind === "cpu") return (data.points || []).map((point) => (
      typeof point.cpu_utilization_pct === "number" ? point.cpu_utilization_pct : null
    ));
    if (kind === "systemMemory") return (data.points || []).map((point) => point.memory_used_pct ?? null);
    const field = kind === "gpuUtil" ? "gpu_utilization_pct" : "memory_used_pct";
    const series = data.gpu_series || [];
    const count = Math.max(0, ...series.map((item) => (item.points || []).length));
    return Array.from({ length: count }, (_, index) => average(series.map((item) => item.points?.[index]?.[field] ?? null)));
  }

  function sparkline(values, color) {
    const present = values.filter((value) => typeof value === "number" && Number.isFinite(value));
    if (!present.length) return `<div class="chart-empty">暂无资源历史</div>`;
    const points = values.map((value, index) => {
      if (typeof value !== "number" || !Number.isFinite(value)) return null;
      const x = values.length < 2 ? 50 : index * 100 / (values.length - 1);
      const y = 96 - clamp(value) * .86;
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    }).filter(Boolean).join(" ");
    return `<svg class="sparkline" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true"><path d="M0 96H100" stroke="#e9edf1" stroke-width="1" fill="none"/><path d="M0 53H100" stroke="#f1f3f5" stroke-width="1" fill="none"/><polyline points="${points}" stroke="${color}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>`;
  }

  function charts(history) {
    const cards = [
      ["CPU 使用率", "CPU", "cpu", "#4f7ff0"],
      ["内存占用率", "内存", "systemMemory", "#22a18f"],
      ["GPU 利用率", "GPU", "gpuUtil", "#8e70ea"],
      ["显存占用率", "显存", "memory", "#e8ad00"],
    ];
    return cards.map(([title, label, kind, color]) => {
      const values = valuesForChart(history, kind); const latest = [...values].reverse().find((value) => typeof value === "number");
      return `<section class="chart-card"><h4>${title}</h4><p>${label}：最新 ${percent(latest)}</p>${sparkline(values, color)}</section>`;
    }).join("");
  }

  function renderDetail(endpointID, history = null, range = 3600) {
    const endpoint = (view.current?.endpoints || []).find((item) => item.id === endpointID); if (!endpoint) return;
    const gpus = endpointGPUs(endpointID); const keepalive = endpoint.keepalive?.policy === "idle_keepalive";
    byID("detail-content").innerHTML = `<div class="detail-sheet"><header class="detail-heading"><div class="detail-title"><span class="server-glyph">▤</span><div><h2>服务器详情</h2><p>${escapeHTML(endpointSsh(endpoint))}</p></div></div><div class="detail-actions"><button class="button primary" type="button" id="detail-claim">申请 GPU</button><button class="button secondary" type="button" id="detail-keepalive">${keepalive ? "结束占卡" : "开始占卡"}</button><button class="icon-button close" type="button" id="close-detail" aria-label="关闭">×</button></div></header><div class="workspace-path">▣ 远端工作区：${escapeHTML(endpoint.workspace_path || "未设置")}</div><section class="gpu-status-panel"><div class="section-heading"><h3>GPU 显存状态</h3><p>当前显存 · ${gpus.length} 张 GPU</p></div><div class="status-counts">${statusCounts(gpus)}</div><div class="gpu-cards">${gpus.length ? gpus.sort((left, right) => (left.gpu_index || 0) - (right.gpu_index || 0)).map(gpuCard).join("") : `<div class="chart-empty">此服务器没有 GPU。</div>`}</div></section><section class="history-section"><div class="history-header"><h3>历史</h3><div class="range-switch">${[[3600, "1h"], [21600, "6h"], [86400, "24h"]].map(([seconds, label]) => `<button type="button" class="${range === seconds ? "active" : ""}" data-history-range="${seconds}">${label}</button>`).join("")}</div></div><div class="chart-grid">${charts(history)}</div></section></div>`;
    byID("close-detail").addEventListener("click", () => byID("detail-dialog").close());
    byID("detail-claim").addEventListener("click", () => openClaim(endpointID));
    byID("detail-keepalive").addEventListener("click", async () => {
      const enabled = !keepalive;
      if (!window.confirm(enabled ? "开始只对这台服务器的空闲 GPU 执行占卡？不会启动或停止已有任务。" : "结束这台服务器的空闲 GPU 占卡？不会停止正在运行的任务。")) return;
      const result = await call("set_keepalive", endpointID, enabled);
      if (!result.ok) { failure(result); return; }
      showNotice(enabled ? "已提交开始占卡，正在确认 GPU 状态。" : "已提交结束占卡，正在确认 GPU 状态。");
      await refresh({ quiet: true }); renderDetail(endpointID, history, range);
    });
    $$('[data-history-range]').forEach((button) => button.addEventListener("click", () => loadHistory(endpointID, Number(button.dataset.historyRange))));
  }

  async function loadHistory(endpointID, range = 3600) {
    renderDetail(endpointID, null, range);
    const result = await call("endpoint_history", endpointID, range);
    if (!result.ok) { failure(result); return; }
    renderDetail(endpointID, result.data, range);
  }

  function openDetail(endpointID) {
    view.selectedEndpointID = endpointID; renderRows();
    const dialog = byID("detail-dialog"); if (!dialog.open) dialog.showModal();
    loadHistory(endpointID);
  }

  function openClaim(endpointID = "") {
    const select = byID("claim-endpoint");
    select.innerHTML = `<option value="">自动选择</option>${(view.current?.endpoints || []).map((endpoint) => `<option value="${escapeHTML(endpoint.id)}">${escapeHTML(endpointSsh(endpoint))}</option>`).join("")}`;
    select.value = endpointID;
    if (!byID("claim-dialog").open) byID("claim-dialog").showModal();
  }

  async function loadSettings() {
    const [info, settings] = await Promise.all([call("app_info"), call("collector_settings")]);
    if (info.ok) {
      byID("service-url").textContent = info.data.base_url;
      byID("data-directory").textContent = info.data.data_dir;
      byID("app-version").textContent = info.data.version;
    }
    if (settings.ok) byID("collector-interval").value = String(settings.data.data?.settings?.interval_seconds || settings.data.data?.interval_seconds || 10);
  }

  async function openSettings() {
    await loadSettings();
    if (!byID("settings-dialog").open) byID("settings-dialog").showModal();
  }

  function setPage(page) {
    if (page === "settings") { openSettings(); return; }
    byID("servers-page").hidden = page !== "servers";
    byID("usage-page").hidden = page !== "usage";
    $$('[data-page]').forEach((button) => {
      const active = button.dataset.page === page;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
  }

  function bindEvents() {
    $$(".form-dialog .close, .form-dialog .button.secondary[value=cancel]").forEach((button) => {
      button.addEventListener("click", () => button.closest("dialog").close());
    });
    byID("refresh").addEventListener("click", () => refresh());
    byID("search").addEventListener("input", renderRows);
    $$(".filter").forEach((button) => button.addEventListener("click", () => { view.filter = button.dataset.filter; $$(".filter").forEach((item) => item.classList.toggle("selected", item === button)); renderRows(); }));
    $$(".table-head button").forEach((button) => button.addEventListener("click", () => { const key = button.dataset.sort; view.sort = { key, direction: view.sort.key === key && view.sort.direction === "desc" ? "asc" : "desc" }; renderRows(); $$(".table-head button").forEach((item) => item.classList.toggle("active", item === button)); }));
    byID("server-rows").addEventListener("click", (event) => { const target = event.target.closest("[data-open-detail]"); if (target) openDetail(target.dataset.openDetail); });
    byID("open-claim").addEventListener("click", () => openClaim());
    byID("open-add-server").addEventListener("click", () => byID("add-dialog").showModal());
    $$('[data-page]').forEach((button) => button.addEventListener("click", () => setPage(button.dataset.page)));
    byID("usage-claim").addEventListener("click", () => openClaim());

    byID("claim-form").addEventListener("submit", async (event) => {
      event.preventDefault(); const form = new FormData(event.currentTarget); const endpoint = form.get("endpoint");
      const constraints = { gpu_count: Number(form.get("gpuCount")), placement: "pack" };
      if (endpoint) constraints.endpoint_ids = [endpoint];
      const memory = form.get("minimumFreeVRAM"); if (memory) constraints.min_free_vram_mib = Number(memory) * 1024;
      const result = await call("claim", { project_id: form.get("project"), task_ref: form.get("task"), purpose: form.get("purpose"), constraints });
      if (!result.ok) { failure(result); return; }
      byID("claim-dialog").close(); showNotice("已提交 GPU 申请，正在刷新资源状态。"); await refresh({ quiet: true });
    });

    byID("add-form").addEventListener("submit", async (event) => {
      event.preventDefault(); const form = new FormData(event.currentTarget);
      const result = await call("create_endpoint", { id: form.get("id"), host: form.get("host"), port: Number(form.get("port")), ssh_user: form.get("sshUser"), workspace_path: form.get("workspace"), observation_profile: "server-script-v1", keepalive_adapter_id: "server-script-v1", labels: ["desktop-windows"], owner_project_id: form.get("ownerProject") || null });
      if (!result.ok) { failure(result); return; }
      byID("add-dialog").close(); showNotice("已添加服务器，正在等待首次只读采集确认状态。"); await refresh({ quiet: true });
    });

    byID("settings-form").addEventListener("submit", async (event) => {
      event.preventDefault(); const result = await call("update_collector_interval", Number(byID("collector-interval").value));
      if (!result.ok) { failure(result); return; }
      byID("settings-dialog").close(); showNotice("数据采集间隔已更新。"); await refresh({ quiet: true });
    });
  }

  async function boot() {
    bindEvents(); await refresh({ quiet: true });
    window.setInterval(() => { if (!document.hidden) refresh({ quiet: true }); }, 10_000);
  }

  window.addEventListener("pywebviewready", boot, { once: true });
})();
