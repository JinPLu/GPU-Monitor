(() => {
  const liveState = document.getElementById("realtime-state");
  const dashboardNode = document.getElementById("dashboard-data");
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const dialogOpeners = new WeakMap();

  const setLiveState = (kind, text) => {
    if (!liveState) return;
    liveState.classList.remove("online", "error");
    if (kind) liveState.classList.add(kind);
    liveState.lastChild.textContent = text;
  };

  document.querySelectorAll("[data-open-dialog]").forEach((button) => {
    button.addEventListener("click", () => {
      const dialog = document.getElementById(button.dataset.openDialog);
      if (!dialog) return;
      dialogOpeners.set(dialog, button);
      dialog.showModal();
    });
  });
  document.querySelectorAll("[data-close-dialog]").forEach((button) => {
    button.addEventListener("click", () => button.closest("dialog")?.close());
  });
  document.querySelectorAll("dialog").forEach((dialog) => {
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
    dialog.addEventListener("close", () => {
      const opener = dialogOpeners.get(dialog);
      if (opener?.isConnected) opener.focus();
      dialogOpeners.delete(dialog);
    });
  });

  const sidebarToggle = document.querySelector("[data-toggle-sidebar]");
  sidebarToggle?.addEventListener("click", () => {
    const open = !document.body.classList.contains("sidebar-open");
    document.body.classList.toggle("sidebar-open", open);
    sidebarToggle.setAttribute("aria-expanded", String(open));
  });
  document.querySelectorAll(".sidebar-nav a").forEach((link) => {
    link.addEventListener("click", () => document.body.classList.remove("sidebar-open"));
  });

  const activateTab = (button, selector, panelAttribute) => {
    const tablist = button.closest('[role="tablist"]');
    if (!tablist) return;
    tablist.querySelectorAll('[role="tab"]').forEach((tab) => {
      const selected = tab === button;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      const panel = document.getElementById(tab.dataset[panelAttribute]);
      if (panel) panel.hidden = !selected;
    });
    if (selector) button.dataset[selector] && button.focus();
  };

  document.querySelectorAll("[data-dialog-tab]").forEach((button) => {
    button.addEventListener("click", () => activateTab(button, null, "dialogTab"));
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      const tabs = [...button.closest('[role="tablist"]').querySelectorAll('[role="tab"]')];
      const current = tabs.indexOf(button);
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
        : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      event.preventDefault();
      activateTab(tabs[next], "dialogTab", "dialogTab");
    });
  });

  const parseJsonResponse = async (response) => {
    let payload;
    try {
      payload = await response.json();
    } catch (_error) {
      throw new Error("服务器返回了无法识别的响应，请稍后重试。");
    }
    if (!response.ok || payload.error) {
      const message = payload.error?.message || `请求失败（${response.status}）`;
      const details = payload.error?.details;
      throw new Error(details ? `${message}：${typeof details === "string" ? details : JSON.stringify(details)}` : message);
    }
    return payload.data;
  };

  const requestSshPreview = async (command, csrf) => {
    const response = await fetch("/ui/endpoints/ssh/preview", {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ command, csrf }),
    });
    return parseJsonResponse(response);
  };

  const requestSshBatchPreview = async (commands, csrf) => {
    const response = await fetch("/ui/endpoints/ssh/batch/preview", {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ commands, csrf }),
    });
    return parseJsonResponse(response);
  };

  const commitSshEndpoint = async ({ command, previewToken, endpointId, projectIds, csrf }) => {
    const body = { command, preview_token: previewToken, csrf };
    if (endpointId) body.endpoint_id = endpointId;
    if (projectIds?.length) body.project_ids = projectIds;
    const response = await fetch("/ui/endpoints/ssh/commit", {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return parseJsonResponse(response);
  };

  const commitSshBatch = async ({ commands, previewToken, csrf }) => {
    const response = await fetch("/ui/endpoints/ssh/batch/commit", {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ commands, preview_token: previewToken, csrf }),
    });
    return parseJsonResponse(response);
  };

  const sshForm = document.getElementById("ssh-preview-form");
  if (sshForm) {
    const commandInput = document.getElementById("ssh-command");
    const previewSection = document.getElementById("ssh-preview");
    const previewFields = document.getElementById("ssh-preview-fields");
    const previewTitle = document.getElementById("ssh-preview-title");
    const errorNode = document.getElementById("ssh-error");
    const previewButton = document.getElementById("ssh-preview-button");
    const commitButton = document.getElementById("ssh-commit-button");
    const endpointIdField = document.getElementById("ssh-endpoint-id-field");
    const pasteButton = document.getElementById("paste-ssh-command");
    const clipboardStatus = document.getElementById("ssh-clipboard-status");
    const clipboardBridge = window.webkit?.messageHandlers?.gpuBrokerClipboard;
    let sshPreviewData = null;
    let sshBatchCommands = null;

    const setClipboardStatus = (message) => {
      if (!clipboardStatus) return;
      clipboardStatus.textContent = message;
      clipboardStatus.hidden = false;
    };
    if (clipboardBridge && pasteButton) {
      pasteButton.hidden = false;
      window.gpuBrokerSetSSHCommand = (command) => {
        commandInput.value = command;
        commandInput.focus();
        setClipboardStatus("已从系统剪贴板填入 SSH 命令；请检查后点击“检查命令”。");
      };
      window.gpuBrokerClipboardError = (message) => setClipboardStatus(message);
      pasteButton.addEventListener("click", () => {
        setClipboardStatus("正在从系统剪贴板读取…");
        clipboardBridge.postMessage({ action: "paste-ssh-command" });
      });
    }

    const showSshError = (error) => {
      errorNode.textContent = error instanceof Error ? error.message : String(error);
      errorNode.hidden = false;
    };
    const clearSshError = () => {
      errorNode.textContent = "";
      errorNode.hidden = true;
    };
    const previewEntries = (preview) => {
      const endpoint = preview.endpoint && typeof preview.endpoint === "object" ? preview.endpoint : preview;
      const labels = { ssh_user: "SSH 用户", user: "SSH 用户", host: "主机", port: "端口", endpoint_id: "服务器名称", identity_file: "身份文件", proxy_jump: "跳板主机" };
      return Object.entries(endpoint)
        .filter(([key, value]) => !["preview_token", "command", "project_ids"].includes(key) && value !== null && value !== undefined && typeof value !== "object")
        .map(([key, value]) => [labels[key] || key.replaceAll("_", " "), value]);
    };
    const renderBatchPreview = (entries) => {
      const labels = { new: "可登记", existing: "将更新", invalid: "格式无效", duplicate: "重复", id_collision: "名称冲突" };
      previewFields.innerHTML = `<table><thead><tr><th>行</th><th>服务器</th><th>结果</th></tr></thead><tbody>${entries.map((entry) => {
        const target = entry.endpoint ? `${entry.endpoint.ssh_user}@${entry.endpoint.host}:${entry.endpoint.port}` : entry.command;
        return `<tr><td>${entry.line}</td><td>${escapeForMarkup(target)}</td><td>${escapeForMarkup(entry.error || labels[entry.status] || entry.status)}</td></tr>`;
      }).join("")}</tbody></table>`;
    };

    sshForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      clearSshError();
      previewButton.disabled = true;
      previewButton.textContent = "正在检查…";
      try {
        const csrf = sshForm.elements.csrf.value;
        const commands = commandInput.value.split(/\r?\n/).map((command) => command.trim()).filter(Boolean);
        sshBatchCommands = commands.length > 1 ? commands : null;
        sshPreviewData = sshBatchCommands
          ? await requestSshBatchPreview(sshBatchCommands, csrf)
          : await requestSshPreview(commandInput.value, csrf);
        const entries = previewEntries(sshPreviewData || {});
        const statusTitles = {
          new: "确认新服务器信息",
          existing: "该服务器已登记，将更新配置",
          id_collision: "服务器名称冲突，请填写其他名称",
        };
        previewTitle.textContent = sshBatchCommands
          ? `检查结果：${sshPreviewData.valid_count} 台可登记`
          : statusTitles[sshPreviewData?.status] || "确认服务器信息";
        endpointIdField.hidden = Boolean(sshBatchCommands);
        document.getElementById("ssh-endpoint-id").value = sshPreviewData?.endpoint?.id || "";
        if (sshBatchCommands) renderBatchPreview(sshPreviewData.entries);
        else previewFields.innerHTML = `<dl>${entries.length
          ? entries.map(([label, value]) => `<dt>${escapeForMarkup(label)}</dt><dd>${escapeForMarkup(value)}</dd>`).join("")
          : "<dt>命令</dt><dd>格式有效，可继续注册</dd>"}</dl>`;
        sshForm.hidden = true;
        previewSection.hidden = false;
        commitButton.focus();
      } catch (error) {
        showSshError(error);
        commandInput.focus();
      } finally {
        previewButton.disabled = false;
        previewButton.textContent = "检查命令";
      }
    });

    document.querySelector("[data-edit-ssh-command]")?.addEventListener("click", () => {
      previewSection.hidden = true;
      sshForm.hidden = false;
      commandInput.focus();
    });

    commitButton.addEventListener("click", async () => {
      clearSshError();
      commitButton.disabled = true;
      commitButton.textContent = "正在注册…";
      try {
        const previewToken = sshPreviewData?.preview_token;
        if (!previewToken) throw new Error("预览已失效，请返回重新检查命令。");
        const result = sshBatchCommands
          ? await commitSshBatch({ commands: sshBatchCommands, previewToken, csrf: sshForm.elements.csrf.value })
          : await commitSshEndpoint({
            command: commandInput.value,
            previewToken,
            endpointId: document.getElementById("ssh-endpoint-id").value.trim(),
            projectIds: sshPreviewData.endpoint?.project_ids,
            csrf: sshForm.elements.csrf.value,
          });
        if (sshBatchCommands && result.entries.some((entry) => !["registered", "updated"].includes(entry.status))) {
          renderBatchPreview(result.entries);
          previewTitle.textContent = `已登记 ${result.registered_count} 台；其余行未写入`;
          commitButton.disabled = false;
          commitButton.textContent = "确认注册";
          return;
        }
        window.location.reload();
      } catch (error) {
        showSshError(error);
        commitButton.disabled = false;
        commitButton.textContent = "确认注册";
      }
    });

    document.getElementById("server-dialog")?.addEventListener("close", () => {
      previewSection.hidden = true;
      sshForm.hidden = false;
      clearSshError();
      sshPreviewData = null;
      sshBatchCommands = null;
      endpointIdField.hidden = false;
      commitButton.disabled = false;
      commitButton.textContent = "确认注册";
      const commandTab = document.getElementById("ssh-command-tab");
      if (commandTab) activateTab(commandTab, null, "dialogTab");
    });
  }

  function escapeForMarkup(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  if (!dashboardNode) {
    if (document.body.dataset.realtime === "on") setLiveState("online", "本机已连接");
    return;
  }

  const escapeHTML = escapeForMarkup;
  const clamp = (value) => Math.max(0, Math.min(100, Number(value) || 0));
  const stateLabels = {
    AVAILABLE: "空闲",
    BUSY_UNMANAGED: "占用（未登记）",
    RUNNING_MANAGED: "运行中",
    HELD: "已认领",
    LEASED_IDLE: "已认领",
    RESERVED: "已安排",
    MAINTENANCE: "维护",
    DISABLED: "停用",
    UNKNOWN_RECOVERING: "等待数据",
    UNKNOWN_STALE: "数据陈旧",
    UNHEALTHY: "不健康",
    CONFLICT: "归属冲突",
    ORPHANED_BUSY: "过期仍占用",
  };
  const monitorLabels = {
    ONLINE: "在线",
    ERROR: "连接错误",
    STALE: "数据陈旧",
    PENDING: "等待采集",
    DISABLED: "停用",
  };
  const claimedStates = new Set(["HELD", "LEASED_IDLE", "RUNNING_MANAGED", "ORPHANED_BUSY", "CONFLICT"]);
  const busyStates = new Set(["BUSY_UNMANAGED", "RUNNING_MANAGED"]);
  const abnormalStates = new Set(["UNKNOWN_RECOVERING", "UNKNOWN_STALE", "UNHEALTHY", "CONFLICT", "ORPHANED_BUSY"]);

  const formatMemory = (mib) => {
    if (mib === null || mib === undefined) return "—";
    const gib = Number(mib) / 1024;
    return `${gib >= 10 ? gib.toFixed(0) : gib.toFixed(1)} GiB`;
  };
  const formatDate = (value, includeDate = false) => {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return new Intl.DateTimeFormat("zh-CN", {
      month: includeDate ? "2-digit" : undefined,
      day: includeDate ? "2-digit" : undefined,
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(date);
  };

  let data = JSON.parse(dashboardNode.textContent);
  let snapshotRevision = Number(data.snapshot_revision || 0);
  let activeSideTab = "claims";
  let activeResourceFilter = "all";
  let resourceQuery = "";
  let allExpanded = false;
  let selectedGpuId = null;
  let selectedWindow = 3600;
  let chart = null;
  let chartAssets = null;
  let refreshing = false;
  const refreshIntervals = new Set([0, 5_000, 10_000, 30_000]);
  const refreshPreferenceKey = "gpu-broker.refresh-interval-ms";
  let savedRefreshInterval = Number.NaN;
  try {
    const savedRefreshValue = window.localStorage?.getItem(refreshPreferenceKey);
    savedRefreshInterval = savedRefreshValue === null ? Number.NaN : Number(savedRefreshValue);
  } catch (_error) {
    // Private browsing or an embedded webview can disable storage.
  }
  let refreshIntervalMs = refreshIntervals.has(savedRefreshInterval) ? savedRefreshInterval : 10_000;
  let refreshTimer = null;
  const expandedServers = new Set();
  const serverGroups = document.getElementById("server-groups");
  const dashboardLayout = document.getElementById("dashboard-layout");
  const coordinationToggle = document.getElementById("toggle-coordination");
  const coordinationReopen = document.getElementById("coordination-reopen");
  const refreshButton = document.getElementById("refresh-dashboard");
  const refreshIntervalSelect = document.getElementById("refresh-interval");

  const constraintsSummary = (constraints = {}) => {
    const parts = [`${constraints.gpu_count || 1} 块 GPU`];
    if (constraints.min_available_cpu_cores !== null && constraints.min_available_cpu_cores !== undefined) {
      parts.push(`CPU 可用 ${Number(constraints.min_available_cpu_cores).toLocaleString("zh-CN")} 核`);
    }
    if (constraints.min_available_memory_mib !== null && constraints.min_available_memory_mib !== undefined) {
      parts.push(`内存可用 ${formatMemory(constraints.min_available_memory_mib)}`);
    }
    if (constraints.min_total_vram_mib !== null && constraints.min_total_vram_mib !== undefined) {
      parts.push(`单卡总显存 ${formatMemory(constraints.min_total_vram_mib)}`);
    }
    if (constraints.min_free_vram_mib !== null && constraints.min_free_vram_mib !== undefined) {
      parts.push(`单卡可用显存 ${formatMemory(constraints.min_free_vram_mib)}`);
    }
    return parts.join(" · ");
  };

  const clusterMeter = (label, value, kind, detail = null) => {
    const rounded = Math.round(clamp(value));
    const level = rounded >= 85 ? "critical" : rounded >= 60 ? "elevated" : "normal";
    const valueText = detail || `${rounded}%`;
    return `<span class="cluster-meter ${kind} ${level}" role="img" aria-label="${label} ${valueText}，${rounded}%" title="${label} ${valueText} · ${rounded}%"><span class="cluster-meter-label"><span>${escapeHTML(label)}</span><strong>${escapeHTML(valueText)}</strong></span><span class="cluster-meter-track"><i style="--value:${rounded}%"></i></span></span>`;
  };

  const gpuRow = (gpu) => {
    const telemetry = gpu.telemetry || {};
    const memoryPct = gpu.total_vram_mib
      ? clamp((Number(telemetry.memory_used_mib || 0) * 100) / gpu.total_vram_mib)
      : 0;
    const utilization = telemetry.gpu_utilization_pct;
    const processes = gpu.processes || [];
    const lease = gpu.lease;
    const owner = lease?.actor_id || processes[0]?.username || "—";
    const task = lease?.task_ref || processes[0]?.executable || gpu.state_reason || "未登记任务";
    const stateIcon = gpu.state === "AVAILABLE" ? "ph-check"
      : busyStates.has(gpu.state) ? "ph-waveform"
        : claimedStates.has(gpu.state) ? "ph-user"
          : abnormalStates.has(gpu.state) ? "ph-warning"
            : "ph-clock";
    const stateClass = gpu.state.toLowerCase();
    return `
      <button class="gpu-tile state-${stateClass}" type="button" data-gpu-id="${escapeHTML(gpu.id)}" data-show-gpu="${escapeHTML(gpu.id)}" aria-label="查看 GPU ${gpu.gpu_index}，${escapeHTML(stateLabels[gpu.state] || gpu.state)}，详情">
        <span class="gpu-tile-top"><span class="gpu-tile-icon"><i class="ph ${stateIcon}" aria-hidden="true"></i></span><span class="gpu-tile-state">${escapeHTML(stateLabels[gpu.state] || gpu.state)}</span></span>
        <span class="gpu-tile-title"><strong>GPU ${gpu.gpu_index}</strong><small>${escapeHTML(gpu.name)}</small></span>
        <span class="gpu-tile-metrics"><span>显存 ${formatMemory(telemetry.memory_free_mib)} 可用</span><span>利用率 ${utilization ?? "—"}%</span></span>
        <span class="gpu-tile-owner"><strong>${escapeHTML(owner)}</strong><small title="${escapeHTML(task)}">${escapeHTML(task)}</small></span>
      </button>`;
  };

  const serverBlock = (endpoint) => {
    const gpus = data.gpus.filter((gpu) => gpu.endpoint_id === endpoint.id);
    const available = gpus.filter((gpu) => gpu.state === "AVAILABLE").length;
    const busy = gpus.filter((gpu) => busyStates.has(gpu.state)).length;
    const claimed = gpus.filter((gpu) => claimedStates.has(gpu.state)).length;
    const abnormal = gpus.filter((gpu) => abnormalStates.has(gpu.state)).length;
    const telemetry = gpus.map((gpu) => gpu.telemetry).filter(Boolean);
    const used = telemetry.reduce((sum, item) => sum + Number(item.memory_used_mib || 0), 0);
    const total = gpus.reduce((sum, gpu) => sum + Number(gpu.total_vram_mib || 0), 0);
    const memoryPct = total ? used * 100 / total : 0;
    const utilValues = telemetry.map((item) => item.gpu_utilization_pct).filter((value) => value !== null && value !== undefined);
    const util = utilValues.length ? utilValues.reduce((sum, value) => sum + Number(value), 0) / utilValues.length : 0;
    const host = endpoint.host_telemetry;
    const cpuLoadPct = host?.cpu_count ? clamp((Number(host.load_1m) * 100) / Number(host.cpu_count)) : 0;
    const availableCpu = host ? Math.max(0, Number(host.cpu_count) - Number(host.load_1m)) : null;
    const memoryUsedPct = host?.memory_total_mib
      ? clamp((1 - Number(host.memory_available_mib) / Number(host.memory_total_mib)) * 100)
      : 0;
    const cpuDetail = host ? `${availableCpu.toLocaleString("zh-CN", { maximumFractionDigits: 1 })}/${Number(host.cpu_count).toLocaleString("zh-CN")} 核可用` : "暂无数据";
    const memoryDetail = host ? `${formatMemory(host.memory_available_mib)}/${formatMemory(host.memory_total_mib)} 可用` : "暂无数据";
    const vramDetail = total ? `${formatMemory(used)}/${formatMemory(total)} 已用` : "暂无数据";
    const sshCommand = `ssh -p ${endpoint.port} ${endpoint.ssh_user}@${endpoint.host}`;
    const expanded = allExpanded || expandedServers.has(endpoint.id);
    const status = endpoint.monitor?.status || "PENDING";
    return `
      <section class="server-block" data-server-id="${escapeHTML(endpoint.id)}" data-expanded="${expanded}">
        <div class="server-summary">
          <span class="server-name"><i class="status-dot ${status.toLowerCase()}"></i><span><strong><code>${escapeHTML(sshCommand)}</code></strong><small>${escapeHTML(endpoint.id)} · ${escapeHTML(monitorLabels[status] || status)}</small></span></span>
          <span class="server-counts" aria-label="GPU 状态：共 ${gpus.length}，空闲 ${available}，占用 ${busy}，认领 ${claimed}，异常 ${abnormal}"><span title="总数"><strong>${gpus.length}</strong></span><span class="count-available" title="空闲"><strong>${available}</strong></span><span title="占用"><strong>${busy}</strong></span><span title="认领"><strong>${claimed}</strong></span><span class="${abnormal ? "count-alert" : ""}" title="异常"><strong>${abnormal}</strong></span></span>
          <span class="server-aggregate">${clusterMeter("CPU", cpuLoadPct, "cpu", cpuDetail)}${clusterMeter("内存", memoryUsedPct, "memory", memoryDetail)}${clusterMeter("显存", memoryPct, "memory", vramDetail)}${clusterMeter("GPU", util, "utilization", `${Math.round(util)}% 利用率`)}</span>
          <span class="server-actions">
            <button class="server-expand" type="button" data-toggle-server="${escapeHTML(endpoint.id)}" aria-expanded="${expanded}" title="${expanded ? "收起 GPU" : "展开 GPU"}">
              <span>${expanded ? "收起 GPU" : "展开 GPU"}</span><i class="ph ph-caret-down" aria-hidden="true"></i>
            </button>
            <form class="server-delete-form" method="post" action="/ui/action/delete-endpoint" data-delete-server-form data-server-label="${escapeHTML(endpoint.id)}">
              <input type="hidden" name="endpoint_id" value="${escapeHTML(endpoint.id)}">
              <input type="hidden" name="csrf" value="${escapeHTML(csrfToken)}">
              <input type="hidden" name="confirmed" value="yes">
              <button class="icon-button server-delete" type="submit" title="删除服务器" aria-label="删除服务器 ${escapeHTML(endpoint.id)}"><i class="ph ph-trash" aria-hidden="true"></i></button>
            </form>
          </span>
        </div>
        <div class="gpu-tiles">${expanded ? (gpus.length ? gpus.map(gpuRow).join("") : '<p class="empty-inline">尚未发现 GPU；该服务器不会参与分配。</p>') : ""}</div>
      </section>`;
  };

  const renderSummary = () => {
    const summary = data.summary || {};
    document.getElementById("kpi-servers").textContent = `${summary.online_servers || 0} / ${summary.total_servers || 0}`;
    document.getElementById("kpi-total").textContent = summary.total_gpus || 0;
    document.getElementById("kpi-available").textContent = summary.available_gpus || 0;
    document.getElementById("kpi-busy").textContent = summary.busy_gpus || 0;
    document.getElementById("kpi-claimed").textContent = summary.claimed_gpus || 0;
    document.getElementById("kpi-abnormal").textContent = summary.abnormal_gpus || 0;
    document.getElementById("data-age").textContent = data.data_age_seconds === null || data.data_age_seconds === undefined
      ? "等待首次采集"
      : `最旧数据 ${Math.round(data.data_age_seconds)} 秒`;
  };

  const endpointMatches = (endpoint) => {
    const gpus = data.gpus.filter((gpu) => gpu.endpoint_id === endpoint.id);
    const status = endpoint.monitor?.status || "PENDING";
    const searchable = `${endpoint.id} ${endpoint.ssh_user} ${endpoint.host} ${endpoint.port}`.toLowerCase();
    if (resourceQuery && !searchable.includes(resourceQuery)) return false;
    if (activeResourceFilter === "available") return gpus.some((gpu) => gpu.state === "AVAILABLE");
    if (activeResourceFilter === "busy") return gpus.some((gpu) => busyStates.has(gpu.state));
    if (activeResourceFilter === "claimed") return gpus.some((gpu) => claimedStates.has(gpu.state));
    if (activeResourceFilter === "attention") {
      return ["ERROR", "STALE", "DISABLED"].includes(status)
        || gpus.some((gpu) => abnormalStates.has(gpu.state));
    }
    return true;
  };

  const renderServers = () => {
    const endpoints = data.endpoints.filter(endpointMatches);
    if (!data.endpoints.length) {
      serverGroups.innerHTML = '<p class="empty-inline">还没有服务器。点击“添加服务器”开始只读监控。</p>';
      return;
    }
    serverGroups.innerHTML = endpoints.length
      ? endpoints.map(serverBlock).join("")
      : '<p class="empty-inline">没有符合当前筛选条件的服务器。</p>';
  };

  const sideItems = () => {
    if (activeSideTab === "claims") {
      return (data.leases || []).map((lease) => `
        <article class="coordination-item"><header><strong>${escapeHTML(lease.actor_id)}</strong><span class="badge">${escapeHTML(lease.state)}</span></header><p>${escapeHTML(lease.task_ref || lease.purpose || "未填写任务")}</p><p>${lease.gpu_ids.length} 块 GPU · ${escapeHTML(lease.project_id)} · 安全截止 ${formatDate(lease.expires_at, true)}</p></article>`).join("");
    }
    if (activeSideTab === "queue") {
      return (data.requests || []).map((request) => `
        <article class="coordination-item"><header><strong>${escapeHTML(request.actor_id)}</strong><span class="badge">排队</span></header><p>${escapeHTML(request.task_ref)}</p><p>${escapeHTML(constraintsSummary(request.constraints))}</p><p>${escapeHTML(request.blocked_reason || "等待可用资源")}</p></article>`).join("");
    }
    return (data.reservations || []).map((reservation) => `
      <article class="coordination-item"><header><strong>${escapeHTML(reservation.actor_id)}</strong><span class="badge">已安排</span></header><p>${escapeHTML(reservation.reason)}</p><p>${formatDate(reservation.start_at, true)} → ${formatDate(reservation.end_at, true)} · ${reservation.gpu_ids.length} 块 GPU</p></article>`).join("");
  };

  const renderCoordination = () => {
    document.getElementById("claims-count").textContent = (data.leases || []).length;
    document.getElementById("queue-count").textContent = (data.requests || []).length;
    document.getElementById("schedule-count").textContent = (data.reservations || []).length;
    document.getElementById("coordination-content").innerHTML = sideItems()
      || `<p class="empty-inline">${activeSideTab === "claims" ? "当前没有认领" : activeSideTab === "queue" ? "当前没有排队" : "当前没有未来安排"}</p>`;
  };

  const render = () => {
    renderSummary();
    renderServers();
    renderCoordination();
  };

  serverGroups.addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-toggle-server]");
    if (toggle) {
      const serverId = toggle.dataset.toggleServer;
      const block = toggle.closest(".server-block");
      const expanded = block.dataset.expanded !== "true";
      if (allExpanded) {
        allExpanded = false;
        data.endpoints.forEach((endpoint) => expandedServers.add(endpoint.id));
        document.getElementById("toggle-all-servers").innerHTML = '<i class="ph ph-arrows-out-line-vertical" aria-hidden="true"></i>展开全部';
      }
      block.dataset.expanded = String(expanded);
      toggle.setAttribute("aria-expanded", String(expanded));
      if (expanded) expandedServers.add(serverId); else expandedServers.delete(serverId);
      renderServers();
      return;
    }
    const detail = event.target.closest("[data-show-gpu]");
    if (detail) {
      dialogOpeners.set(document.getElementById("gpu-detail"), detail);
      openGpuDetail(detail.dataset.showGpu);
    }
  });

  serverGroups.addEventListener("submit", (event) => {
    const form = event.target.closest("[data-delete-server-form]");
    if (!form) return;
    const label = form.dataset.serverLabel || "该服务器";
    const confirmed = window.confirm(`删除服务器 ${label}？这会移除本机监控登记和该服务器的当前 GPU/遥测记录，不会停止远端任务。`);
    if (!confirmed) event.preventDefault();
  });

  document.getElementById("toggle-all-servers").addEventListener("click", (event) => {
    allExpanded = !allExpanded;
    event.currentTarget.innerHTML = allExpanded
      ? '<i class="ph ph-arrows-in-line-vertical" aria-hidden="true"></i>全部收起'
      : '<i class="ph ph-arrows-out-line-vertical" aria-hidden="true"></i>展开全部';
    if (!allExpanded) expandedServers.clear();
    if (allExpanded) expandedServers.clear();
    renderServers();
  });

  document.querySelectorAll("[data-resource-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      activeResourceFilter = button.dataset.resourceFilter;
      document.querySelectorAll("[data-resource-filter]").forEach((item) => {
        item.setAttribute("aria-pressed", String(item === button));
      });
      renderServers();
    });
  });

  document.getElementById("resource-search")?.addEventListener("input", (event) => {
    resourceQuery = event.currentTarget.value.trim().toLowerCase();
    renderServers();
  });

  const coordinationPreferenceKey = "gpu-broker.coordination-collapsed";
  const setCoordinationCollapsed = (collapsed, { focus = false } = {}) => {
    dashboardLayout.classList.toggle("coordination-collapsed", collapsed);
    coordinationToggle.setAttribute("aria-expanded", String(!collapsed));
    coordinationToggle.setAttribute("aria-label", collapsed ? "协作安排已收起" : "收起协作安排");
    coordinationToggle.title = collapsed ? "协作安排已收起" : "收起协作安排";
    coordinationToggle.innerHTML = `<i class="ph ${collapsed ? "ph-caret-left" : "ph-caret-right"}" aria-hidden="true"></i>`;
    coordinationReopen.hidden = !collapsed;
    coordinationReopen.setAttribute("aria-expanded", String(collapsed));
    if (focus) (collapsed ? coordinationReopen : coordinationToggle).focus();
    try {
      window.localStorage?.setItem(coordinationPreferenceKey, String(collapsed));
    } catch (_error) {
      // Private browsing or an embedded webview can disable storage.
    }
  };
  let coordinationCollapsed = false;
  try {
    coordinationCollapsed = window.localStorage?.getItem(coordinationPreferenceKey) === "true";
  } catch (_error) {
    // Private browsing or an embedded webview can disable storage.
  }
  coordinationToggle.addEventListener("click", () => setCoordinationCollapsed(true, { focus: true }));
  coordinationReopen.addEventListener("click", () => setCoordinationCollapsed(false, { focus: true }));
  setCoordinationCollapsed(coordinationCollapsed);

  document.querySelectorAll("[data-side-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      activeSideTab = button.dataset.sideTab;
      document.querySelectorAll("[data-side-tab]").forEach((item) => {
        item.setAttribute("aria-selected", String(item === button));
        item.tabIndex = item === button ? 0 : -1;
      });
      document.getElementById("coordination-content").setAttribute("aria-labelledby", button.id);
      renderCoordination();
    });
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      const tabs = [...document.querySelectorAll("[data-side-tab]")];
      const current = tabs.indexOf(button);
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
        : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      event.preventDefault();
      tabs[next].click();
      tabs[next].focus();
    });
  });

  const loadChartAssets = () => {
    if (chartAssets) return chartAssets;
    chartAssets = new Promise((resolve, reject) => {
      if (!document.querySelector('link[data-uplot]')) {
        const style = document.createElement("link");
        style.rel = "stylesheet";
        style.href = "/static/vendor/uPlot.min.css";
        style.dataset.uplot = "true";
        document.head.appendChild(style);
      }
      if (window.uPlot) {
        resolve(window.uPlot);
        return;
      }
      const script = document.createElement("script");
      script.src = "/static/vendor/uPlot.iife.min.js";
      script.async = true;
      script.onload = () => resolve(window.uPlot);
      script.onerror = reject;
      document.head.appendChild(script);
    });
    return chartAssets;
  };

  const updateDetailText = (gpu) => {
    const telemetry = gpu.telemetry || {};
    document.getElementById("detail-server").textContent = gpu.endpoint_id;
    document.getElementById("detail-title").textContent = `GPU ${gpu.gpu_index} · ${gpu.name}`;
    document.getElementById("detail-state").textContent = stateLabels[gpu.state] || gpu.state;
    document.getElementById("detail-observed").textContent = telemetry.observed_at ? `观测于 ${formatDate(telemetry.observed_at, true)}` : "尚无观测";
    document.getElementById("detail-memory").textContent = `${formatMemory(telemetry.memory_used_mib)} / ${formatMemory(gpu.total_vram_mib)}`;
    document.getElementById("detail-util").textContent = `${telemetry.gpu_utilization_pct ?? "—"}%`;
    document.getElementById("detail-temp").textContent = telemetry.temperature_c === null || telemetry.temperature_c === undefined ? "—" : `${telemetry.temperature_c} °C`;
    document.getElementById("detail-power").textContent = telemetry.power_watts === null || telemetry.power_watts === undefined ? "—" : `${Number(telemetry.power_watts).toFixed(0)} W`;
    const lease = gpu.lease;
    const processes = gpu.processes || [];
    document.getElementById("detail-ownership").innerHTML = lease
      ? `<strong>${escapeHTML(lease.actor_id)}</strong> · ${escapeHTML(lease.task_ref || lease.purpose || "未填写任务")}<br>${processes.length} 个计算进程 · 安全截止 ${formatDate(lease.expires_at, true)}`
      : processes.length
        ? `${processes.length} 个未登记进程 · ${escapeHTML(processes.map((item) => item.executable).join("、"))}`
        : "暂无认领，也没有计算进程";
  };

  const renderChart = async () => {
    if (!selectedGpuId) return;
    const container = document.getElementById("gpu-chart");
    container.innerHTML = '<p class="muted">正在读取历史数据…</p>';
    try {
      const [uPlot, response] = await Promise.all([
        loadChartAssets(),
        fetch(`/api/v1/gpus/${encodeURIComponent(selectedGpuId)}/history?window_seconds=${selectedWindow}&points=120`, { headers: { Accept: "application/json" } }),
      ]);
      if (!response.ok) throw new Error(`history ${response.status}`);
      const payload = await response.json();
      if (selectedGpuId !== payload.data.gpu_id) return;
      const points = payload.data.points || [];
      chart?.destroy();
      chart = null;
      container.innerHTML = "";
      if (!points.length) {
        container.innerHTML = '<p class="muted">这个时间范围还没有历史点。</p>';
        return;
      }
      const series = [
        points.map((point) => new Date(point.observed_at).getTime() / 1000),
        points.map((point) => point.gpu_utilization_pct),
        points.map((point) => point.memory_used_pct),
      ];
      chart = new uPlot({
        width: Math.max(320, Math.floor(container.clientWidth)),
        height: 260,
        cursor: { drag: { x: true, y: false } },
        scales: { x: { time: true }, pct: { range: [0, 100] } },
        series: [
          {},
          { label: "GPU 利用率", scale: "pct", stroke: "#2c67b8", width: 2 },
          { label: "显存使用", scale: "pct", stroke: "#7669c4", width: 2 },
        ],
        axes: [{ stroke: "#7b8798", grid: { stroke: "#edf0f4" } }, { scale: "pct", stroke: "#7b8798", grid: { stroke: "#edf0f4" }, values: (_u, values) => values.map((value) => `${value}%`) }],
      }, series, container);
    } catch (_error) {
      container.innerHTML = '<p class="muted">历史曲线暂时无法读取；当前值仍可正常查看。</p>';
      setLiveState("error", "历史读取失败");
    }
  };

  function openGpuDetail(gpuId) {
    const gpu = data.gpus.find((item) => item.id === gpuId);
    if (!gpu) return;
    selectedGpuId = gpuId;
    selectedWindow = 3600;
    document.querySelectorAll("[data-history-window]").forEach((button) => {
      button.setAttribute("aria-pressed", String(Number(button.dataset.historyWindow) === selectedWindow));
    });
    updateDetailText(gpu);
    document.getElementById("gpu-detail").showModal();
    requestAnimationFrame(renderChart);
  }

  document.querySelectorAll("[data-history-window]").forEach((button) => {
    button.addEventListener("click", () => {
      selectedWindow = Number(button.dataset.historyWindow);
      document.querySelectorAll("[data-history-window]").forEach((item) => {
        item.setAttribute("aria-pressed", String(item === button));
      });
      renderChart();
    });
  });

  document.getElementById("gpu-detail").addEventListener("close", () => {
    selectedGpuId = null;
    chart?.destroy();
    chart = null;
    document.getElementById("gpu-chart").innerHTML = '<p class="muted">打开详情后才加载历史曲线。</p>';
  });

  const refresh = async () => {
    if (refreshing || document.hidden) return;
    refreshing = true;
    refreshButton.disabled = true;
    refreshButton.setAttribute("aria-busy", "true");
    refreshButton.innerHTML = '<i class="ph ph-spinner-gap" aria-hidden="true"></i>';
    try {
      const response = await fetch("/api/v1/snapshot", { headers: { Accept: "application/json" }, cache: "no-store" });
      if (!response.ok) throw new Error(`snapshot ${response.status}`);
      const payload = await response.json();
      const nextRevision = Number(payload.snapshot_revision || 0);
      if (nextRevision !== snapshotRevision) {
        data = { ...data, ...payload.data, snapshot_revision: nextRevision, server_time: payload.server_time };
        snapshotRevision = nextRevision;
        render();
      } else {
        data.data_age_seconds = payload.data.data_age_seconds;
        renderSummary();
      }
      setLiveState("online", `已更新 ${formatDate(payload.server_time)}`);
    } catch (_error) {
      setLiveState("error", "连接中断，正在重试");
    } finally {
      refreshing = false;
      refreshButton.disabled = false;
      refreshButton.removeAttribute("aria-busy");
      refreshButton.innerHTML = '<i class="ph ph-arrow-clockwise" aria-hidden="true"></i>';
    }
  };

  const scheduleRefresh = () => {
    if (refreshTimer !== null) window.clearInterval(refreshTimer);
    refreshTimer = refreshIntervalMs > 0 ? window.setInterval(refresh, refreshIntervalMs) : null;
  };

  refreshIntervalSelect.value = String(refreshIntervalMs);
  refreshButton.addEventListener("click", refresh);
  refreshIntervalSelect.addEventListener("change", (event) => {
    const interval = Number(event.currentTarget.value);
    refreshIntervalMs = refreshIntervals.has(interval) ? interval : 10_000;
    try {
      window.localStorage?.setItem(refreshPreferenceKey, String(refreshIntervalMs));
    } catch (_error) {
      // Private browsing or an embedded webview can disable storage; the setting still applies now.
    }
    scheduleRefresh();
  });

  render();
  setLiveState("online", `已更新 ${formatDate(data.server_time)}`);
  scheduleRefresh();
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && refreshIntervalMs > 0) refresh();
  });
})();
