# Layouts

## Authenticated application shell

- Source: `src/gpu_broker/web/templates/base.html`
- Description: The visual shell: ambient backdrop, sidebar navigation, compact top bar, workspace, and global static assets.

```html
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  {% if csrf %}<meta name="csrf-token" content="{{ csrf }}">{% endif %}
  <title>GPU Broker</title>
  <link rel="stylesheet" href="/static/vendor/phosphor/style.css?v=2.1.2">
  <link rel="stylesheet" href="/static/app.css?v=20260720-home-clarity">
</head>
<body data-realtime="{{ 'on' if actor else 'off' }}" data-page="{{ page }}">
  <div class="environment-backdrop" aria-hidden="true">
    <img src="/static/assets/server-room-background.jpg" alt="">
  </div>
  <div class="app-shell{% if not actor %} signed-out{% endif %}">
    {% if actor %}
    <aside class="sidebar" id="app-sidebar" aria-label="GPU Broker 工作区导航">
      <a class="brand" href="/" aria-label="GPU Broker 资源总览">
        <span class="brand-mark" aria-hidden="true"><i class="ph ph-graphics-card"></i></span><span>GPU Broker</span>
      </a>
      <nav class="sidebar-nav" aria-label="主要导航">
        <p class="nav-label">工作区</p>
        <a href="/" {% if page == 'overview' %}aria-current="page"{% endif %}><i class="ph ph-squares-four" aria-hidden="true"></i>资源总览</a>
        <a href="/ui/gpus" {% if page in ['gpus', 'gpu-detail'] %}aria-current="page"{% endif %}><i class="ph ph-grid-four" aria-hidden="true"></i>GPU 状态</a>
        <a href="/ui/requests" {% if page in ['requests', 'leases'] %}aria-current="page"{% endif %}><i class="ph ph-arrows-left-right" aria-hidden="true"></i>认领与队列</a>
        <a href="/ui/reservations" {% if page == 'reservations' %}aria-current="page"{% endif %}><i class="ph ph-calendar-dots" aria-hidden="true"></i>使用安排</a>
        <p class="nav-label">管理</p>
        <a href="/ui/identities" {% if page == 'identities' %}aria-current="page"{% endif %}><i class="ph ph-users" aria-hidden="true"></i>身份与预设</a>
        <a href="/ui/maintenance" {% if page == 'maintenance' %}aria-current="page"{% endif %}><i class="ph ph-wrench" aria-hidden="true"></i>维护窗口</a>
        <a href="/ui/alerts" {% if page == 'alerts' %}aria-current="page"{% endif %}><i class="ph ph-warning" aria-hidden="true"></i>告警</a>
        <a href="/ui/audit" {% if page == 'audit' %}aria-current="page"{% endif %}><i class="ph ph-clock-counter-clockwise" aria-hidden="true"></i>审计与历史</a>
        <a href="/ui/doctor" {% if page == 'doctor' %}aria-current="page"{% endif %}><i class="ph ph-gear" aria-hidden="true"></i>设置与 Doctor</a>
      </nav>
      <div class="sidebar-footer">
        <span id="realtime-state" class="live-state"><i aria-hidden="true"></i><span>数据连接中</span></span>
        <p>仅本机控制面 · 不执行远端任务</p>
      </div>
    </aside>
    {% endif %}
    <div class="workspace">
      {% if actor %}
      <header class="topbar">
        <button class="sidebar-toggle icon-button" type="button" data-toggle-sidebar aria-expanded="true" aria-controls="app-sidebar" aria-label="显示或隐藏导航"><i class="ph ph-list" aria-hidden="true"></i></button>
        <div class="topbar-title"><span>共享 GPU 工作区</span><small><i class="ph ph-shield-check" aria-hidden="true"></i> 本机只读监控 · 协调状态</small></div>
        <form class="actor-switcher" method="post" action="/ui/actor">
          <label><span>当前操作者</span><input name="actor_id" value="{{ actor.id }}" pattern="[A-Za-z][A-Za-z0-9_.-]{1,127}" required aria-label="当前人类或 Agent 名称"></label>
          <button class="quiet" type="submit">切换</button>
        </form>
      </header>
      {% endif %}
      <main id="main-content" tabindex="-1">
        {% if notice %}<p class="notice" role="status">{{ notice }}</p>{% endif %}
        {% block content %}{% endblock %}
      </main>
    </div>
  </div>
  <script src="/static/app.js" defer></script>
</body>
</html>

```

## All non-dashboard pages

- Source: `src/gpu_broker/web/templates/page.html`
- Description: The generic management-page layout, cards, forms, dialogs, and data table patterns used across GPU, requests, schedules, alerts, and settings.

```html
{% extends "base.html" %}
{% block content %}
<section class="page-heading">
  <div>
    <p class="eyebrow">共享服务器池 · 实时状态 · 协作认领</p>
    <h1>
      {% if page == 'overview' %}服务器总览
      {% elif page == 'gpus' %}GPU 实时状态
      {% elif page == 'gpu-detail' %}GPU 详情
      {% elif page == 'requests' %}认领 GPU
      {% elif page == 'leases' %}当前认领
      {% elif page == 'reservations' %}安排使用时间
      {% elif page == 'identities' %}身份与预设任务
      {% elif page == 'maintenance' %}维护窗口
      {% elif page == 'alerts' %}告警
      {% elif page == 'audit' %}审计与历史
      {% else %}设置与 Doctor{% endif %}
    </h1>
  </div>
  <p class="muted">这里仅监控和协调归属，不会启动、停止或抢占远端任务。</p>
</section>

{% if page == 'overview' %}
<section class="quick-actions" aria-label="常用操作">
  <a class="button-link" href="#add-server">添加服务器</a>
  <a class="button-link quiet-link" href="/ui/requests#new-request">认领 GPU</a>
  <a class="button-link quiet-link" href="/ui/reservations">安排使用时间</a>
</section>

<section class="grid endpoints" aria-label="Endpoint inventory">
{% for endpoint in payload.endpoints %}
  <article class="card">
    <h2>{{ endpoint.id }}</h2>
    <p><code>{{ endpoint.ssh_user }}@{{ endpoint.host }}:{{ endpoint.port }}</code></p>
    <p>监控：<strong class="monitor-{{ endpoint.monitor.status|lower }}">{{ endpoint.monitor.status }}</strong></p>
    <p>已发现 GPU：{{ endpoint.monitor.gpu_count }}{% if endpoint.expected_gpu_count %} / 期望 {{ endpoint.expected_gpu_count }}{% endif %}</p>
    <p class="muted">最近成功：{{ endpoint.monitor.last_success_at or '等待首次采集' }}</p>
    {% if endpoint.monitor.last_error %}<p class="warning-text">{{ endpoint.monitor.last_error }}</p>{% endif %}
  </article>
{% else %}
  <p class="empty">还没有服务器。用下方表单添加第一台即可开始监控。</p>
{% endfor %}
</section>

<section class="grid gpu-grid" aria-label="GPU status grid">
{% for gpu in payload.gpus %}
  <article class="card gpu-card state-{{ gpu.state|lower }}">
    <a href="/ui/gpus/{{ gpu.id }}"><strong>{{ gpu.endpoint_id }} / GPU {{ gpu.gpu_index }}</strong></a>
    <p>状态：<span class="status">{{ gpu.state }}</span></p>
    <p>显存：{{ gpu.telemetry.memory_used_mib if gpu.telemetry else '—' }} / {{ gpu.total_vram_mib }} MiB</p>
    <p>利用率：{{ gpu.telemetry.gpu_utilization_pct if gpu.telemetry else '—' }}%</p>
    <p class="muted">{{ gpu.state_reason or '可参与调度' }}</p>
  </article>
{% else %}
  <p class="empty">尚未从只读 collector 发现 GPU。此状态不会被当成可用资源。</p>
{% endfor %}
</section>

<section id="add-server" class="actions" aria-labelledby="add-server-title">
  <h2 id="add-server-title">添加服务器监控</h2>
  <p class="muted">填写能从本机 SSH 连接的地址。GPU Broker 会定时只读运行固定的 nvidia-smi 查询，不执行任务命令。</p>
  <form method="post" action="/ui/action/endpoint" class="stack form-grid">
    <label>服务器名称（可留空）<input name="id" placeholder="例如：training-a"></label>
    <label>主机或 SSH 地址<input name="host" required placeholder="10.0.0.1 或 gpu-host"></label>
    <label>SSH 端口<input name="port" type="number" min="1" max="65535" value="22" required></label>
    <label>SSH 用户<input name="ssh_user" value="root" required></label>
    <label>期望 GPU 数（可留空）<input name="expected_gpu_count" type="number" min="1" placeholder="自动发现"></label>
    <input type="hidden" name="labels" value="gpu">
    <input type="hidden" name="enabled" value="true">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <input type="hidden" name="confirmed" value="yes">
    <button type="submit">添加并开始监控</button>
  </form>
</section>

<section class="list-section" aria-labelledby="active-claims-title">
  <h2 id="active-claims-title">当前认领</h2>
  <div class="stacked-cards">
  {% for lease in payload.leases if lease.state in ['HELD', 'ACTIVE', 'ORPHANED_BUSY', 'CONFLICT'] %}
    <article class="card">
      <div class="card-title"><strong>{{ lease.actor_id }}</strong><span class="badge">{{ lease.state }}</span></div>
      <p>GPU：{% for gpu_id in lease.gpu_ids %}<code>{{ gpu_id }}</code>{% if not loop.last %}、{% endif %}{% endfor %}</p>
      <p class="muted">项目 {{ lease.project_id }} · 安全截止 {{ lease.expires_at }}</p>
    </article>
  {% else %}<p class="empty">当前没有人或 Agent 认领 GPU。</p>{% endfor %}
  </div>
  <p><a class="button-link quiet-link" href="/ui/requests">管理认领与队列</a></p>
</section>

{% elif page == 'gpus' %}
<section class="grid gpu-grid" aria-label="GPU inventory">
{% for gpu in payload.gpus %}
  <article class="card gpu-card state-{{ gpu.state|lower }}">
    <a href="/ui/gpus/{{ gpu.id }}"><strong>{{ gpu.endpoint_id }} / GPU {{ gpu.gpu_index }}</strong></a>
    <p>状态：<span class="status">{{ gpu.state }}</span></p>
    <p>UUID：<code>{{ gpu.gpu_uuid }}</code></p>
    <p>显存 / 利用率：{{ gpu.telemetry.memory_used_mib if gpu.telemetry else '—' }} MiB / {{ gpu.telemetry.gpu_utilization_pct if gpu.telemetry else '—' }}%</p>
    <p>进程：{{ gpu.processes|length }}；租约：{{ gpu.lease.id if gpu.lease else '无' }}</p>
  </article>
{% else %}
  <p class="empty">尚未发现 GPU。</p>
{% endfor %}
</section>

{% elif page == 'gpu-detail' %}
{% set gpu = payload.gpu %}
<section class="data-panel detail-grid">
  <div><span class="field-label">GPU</span><strong>{{ gpu.endpoint_id }} / GPU {{ gpu.gpu_index }}</strong></div>
  <div><span class="field-label">状态</span><strong>{{ gpu.state }}</strong></div>
  <div><span class="field-label">UUID</span><code>{{ gpu.gpu_uuid }}</code></div>
  <div><span class="field-label">显存</span>{{ gpu.telemetry.memory_used_mib if gpu.telemetry else '—' }} / {{ gpu.total_vram_mib }} MiB</div>
  <div><span class="field-label">GPU 利用率</span>{{ gpu.telemetry.gpu_utilization_pct if gpu.telemetry else '—' }}%</div>
  <div><span class="field-label">温度</span>{{ gpu.telemetry.temperature_c if gpu.telemetry else '—' }} °C</div>
  <div class="span-all"><span class="field-label">调度说明</span>{{ gpu.state_reason or '可参与调度' }}</div>
</section>
<section class="data-panel">
  <h2>进程</h2>
  {% for process in gpu.processes %}
  <p><code>PID {{ process.pid }}</code> · {{ process.executable }} · {{ process.used_memory_mib }} MiB · {{ process.username or 'unknown user' }}</p>
  {% else %}<p class="muted">没有新鲜的 compute process 观测。</p>{% endfor %}
</section>
<p><a class="button-link quiet-link" href="/ui/gpus">返回 GPU 列表</a></p>

{% elif page == 'requests' %}
{% if actor.role in ['allocator', 'operator', 'admin'] %}
<section id="new-request" class="actions" aria-labelledby="request-title">
  <h2 id="request-title">认领 GPU</h2>
  <p>认领者：<strong>{{ actor.id }}</strong></p>
  <p class="muted">可直接填写任意项目标识和任务；完成后释放租约，空闲资源会立即归到这个名称下，不足时自动进入共享队列。</p>
  {% if payload.claimable_workload_profiles %}
  <form method="post" action="/ui/action/profile-claim" class="stack form-grid quick-claim-form">
    <label>预设任务<select name="profile_id" required>{% for profile in payload.claimable_workload_profiles %}<option value="{{ profile.id }}">{{ profile.display_name }} · {{ profile.project_id }} · {{ profile.constraints.gpu_count }} GPU</option>{% endfor %}</select></label>
    <label>任务<input name="task_ref" required placeholder="例如：WAN 视频评测"></label>
    <p class="field-help span-all">按配置认领会直接登记为使用中，但不会启动远端任务。</p>
    <input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="confirmed" value="yes">
    <button type="submit">按配置认领</button>
  </form>
  {% else %}
  <p class="field-help">还没有可用的预设任务；临时需求可直接使用下方一次性认领。</p>
  {% endif %}
  <details>
    <summary>一次性认领（需明确资源）</summary>
    <form method="post" action="/ui/action/quick-claim" class="stack form-grid quick-claim-form">
      <label>项目标识<input name="project_id" required placeholder="例如：storyboard"></label>
      <label>任务<input name="task_ref" required placeholder="例如：WAN 视频评测"></label>
      <label>服务器<select name="endpoint_id"><option value="">自动选择</option>{% for endpoint in payload.endpoints %}<option value="{{ endpoint.id }}">{{ endpoint.id }} · {{ endpoint.host }}:{{ endpoint.port }}</option>{% endfor %}</select></label>
      <label>GPU 数量<input name="gpu_count" type="number" min="1" value="1" required></label>
      <details class="span-all"><summary>高级：指定精确 GPU</summary><label>GPU（可多选）<select name="gpu_ids" multiple size="6">{% for gpu in payload.gpus %}<option value="{{ gpu.id }}">{{ gpu.endpoint_id }} / GPU {{ gpu.gpu_index }} · {{ gpu.state }}</option>{% endfor %}</select></label><p class="field-help">选择后以所选 GPU 数量为准。</p></details>
      <p class="field-help span-all">临时任务会把任务说明写入审计用途；完成后请释放租约。认领成功同样不会启动远端任务。</p>
      <input type="hidden" name="placement" value="pack"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="confirmed" value="yes">
      <button type="submit">一次性认领</button>
    </form>
  </details>
</section>
{% endif %}

<section class="list-section" aria-labelledby="queue-title">
  <h2 id="queue-title">共享请求队列</h2>
  <div class="stacked-cards">
  {% for item in payload.requests %}
    <article class="card request-card state-{{ item.state|lower }}">
      <div class="card-title"><div><strong>{{ item.task_ref }}</strong><p class="muted">{{ item.project_id }} · {{ item.actor_id }}</p></div><span class="badge">{{ item.state }}</span></div>
      <p>{{ item.purpose }}</p>
      <p>资源：{{ item.constraints.gpu_count }} GPU</p>
      {% if item.profile_id %}<p class="muted">预设任务：<code>{{ item.profile_id }}</code></p>{% endif %}
      {% if item.blocked_reason %}<p class="warning-text">队列说明：{{ item.blocked_reason }}</p>{% endif %}
      {% if item.state == 'QUEUED' and actor.role in ['allocator', 'operator', 'admin'] %}
      <form method="post" action="/ui/action/cancel-request" class="inline-form">
        <input type="hidden" name="request_id" value="{{ item.id }}"><input type="hidden" name="csrf" value="{{ csrf }}">
        <input type="hidden" name="confirmed" value="yes"><button type="submit" class="danger">取消排队</button>
      </form>
      {% endif %}
    </article>
  {% else %}
    <p class="empty">尚无请求。可通过上方表单提交第一条申请。</p>
  {% endfor %}
  </div>
</section>

<section class="list-section" aria-labelledby="claims-title">
  <h2 id="claims-title">当前 GPU 归属</h2>
  <div class="stacked-cards">
  {% for lease in payload.leases if lease.state in ['HELD', 'ACTIVE', 'ORPHANED_BUSY', 'CONFLICT'] %}
    <article class="card lease-card state-{{ lease.state|lower }}">
      <div class="card-title"><div><strong>{{ lease.actor_id }}</strong><p class="muted">{{ lease.project_id }}</p></div><span class="badge">{{ lease.state }}</span></div>
      <p>GPU：{% for gpu_id in lease.gpu_ids %}<code>{{ gpu_id }}</code>{% if not loop.last %}、{% endif %}{% endfor %}</p>
      <p>安全截止：{{ lease.expires_at }}</p>
      <div class="inline-actions">
        <form method="post" action="/ui/action/renew-lease" class="inline-form">
          <input type="hidden" name="lease_id" value="{{ lease.id }}"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="confirmed" value="yes"><button type="submit">延长</button>
        </form>
        <form method="post" action="/ui/action/release-lease" class="inline-form">
          <input type="hidden" name="lease_id" value="{{ lease.id }}"><input type="hidden" name="reason" value="released-from-gui"><input type="hidden" name="csrf" value="{{ csrf }}"><label class="confirm"><input type="checkbox" name="confirmed" value="yes" required> 我确认释放只撤销协调归属，不会停止远端进程</label><button type="submit" class="danger">释放认领</button>
        </form>
      </div>
    </article>
  {% else %}<p class="empty">当前没有 GPU 被认领。</p>{% endfor %}
  </div>
</section>

{% elif page == 'leases' %}
<section class="list-section" aria-labelledby="lease-title">
  <h2 id="lease-title">可见租约</h2>
  <p class="muted">激活、续租、绑定工作负载和释放都是显式动作；释放不会 kill 已在运行的进程。</p>
  <div class="stacked-cards">
  {% for lease in payload.leases %}
    <article class="card lease-card state-{{ lease.state|lower }}">
      <div class="card-title"><div><strong>{{ lease.project_id }}</strong><p class="muted"><code>{{ lease.id }}</code></p></div><span class="badge">{{ lease.state }}</span></div>
      <p>GPU：{% for gpu_id in lease.gpu_ids %}<code>{{ gpu_id }}</code>{% if not loop.last %}、{% endif %}{% endfor %}</p>
      <p>安全截止：{{ lease.expires_at or '—' }}{% if lease.workloads %} · 已绑定 {{ lease.workloads|length }} 个工作负载{% endif %}</p>
      {% if actor.role in ['allocator', 'operator', 'admin'] and lease.state == 'HELD' %}
      <form method="post" action="/ui/action/activate-lease" class="inline-form">
        <input type="hidden" name="lease_id" value="{{ lease.id }}"><input type="hidden" name="csrf" value="{{ csrf }}">
        <label class="confirm"><input type="checkbox" name="confirmed" value="yes" required> 确认开始使用租约</label><button type="submit">激活租约</button>
      </form>
      {% endif %}
      {% if actor.role in ['allocator', 'operator', 'admin'] and lease.state in ['HELD', 'ACTIVE'] %}
      <div class="inline-actions">
        <form method="post" action="/ui/action/renew-lease" class="inline-form">
          <input type="hidden" name="lease_id" value="{{ lease.id }}"><input type="hidden" name="csrf" value="{{ csrf }}">
          <label class="confirm"><input type="checkbox" name="confirmed" value="yes" required> 确认续租</label><button type="submit">续租</button>
        </form>
        <form method="post" action="/ui/action/release-lease" class="inline-form">
          <input type="hidden" name="lease_id" value="{{ lease.id }}"><input type="hidden" name="csrf" value="{{ csrf }}">
          <label>释放原因<input name="reason" required value="finished"></label>
          <label class="confirm"><input type="checkbox" name="confirmed" value="yes" required> 我确认释放不会停止远端进程</label><button type="submit" class="danger">释放租约</button>
        </form>
      </div>
      <details>
        <summary>绑定已启动的工作负载</summary>
        <form method="post" action="/ui/action/bind-workload" class="stack compact-form">
          <input type="hidden" name="lease_id" value="{{ lease.id }}"><input type="hidden" name="csrf" value="{{ csrf }}">
          <label>Run ID<input name="run_id" required placeholder="训练或推理运行标识"></label>
          <label>进程 key（可留空；一行或逗号一个）<textarea name="process_keys" rows="3"></textarea></label>
          <label class="confirm"><input type="checkbox" name="confirmed" value="yes" required> 确认只登记观测归属，不会启动进程</label><button type="submit">绑定工作负载</button>
        </form>
      </details>
      {% endif %}
    </article>
  {% else %}
    <p class="empty">没有可见租约。先到“申请与队列”提交资源请求。</p>
  {% endfor %}
  </div>
</section>

{% elif page == 'reservations' %}
{% if actor.role in ['operator', 'admin'] %}
<section class="actions" aria-labelledby="reservation-title">
  <h2 id="reservation-title">安排未来使用时间</h2>
  <p>安排者：<strong>{{ actor.id }}</strong></p>
  <p class="muted">选择具体 GPU 和时间；与已有认领或安排冲突时会直接提示。</p>
  <form method="post" action="/ui/action/reservation" class="stack form-grid">
    <label>项目标识<input name="project_id" required placeholder="例如：storyboard"></label>
    <label>时区<select name="timezone"><option value="Asia/Shanghai" selected>Asia/Shanghai</option><option value="UTC">UTC</option></select></label>
    <label>开始时间<input name="start_at" type="datetime-local" required></label>
    <label>结束时间<input name="end_at" type="datetime-local" required></label>
    <label class="span-all">GPU（可多选）<select name="gpu_ids" multiple required size="8">{% for gpu in payload.gpus %}<option value="{{ gpu.id }}">{{ gpu.endpoint_id }} / GPU {{ gpu.gpu_index }} · {{ gpu.state }}</option>{% endfor %}</select></label>
    <label class="span-all">任务说明<input name="reason" required placeholder="例如：周一模型评测"></label>
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <input type="hidden" name="confirmed" value="yes"><button type="submit">保存安排</button>
  </form>
</section>
{% endif %}
<section class="list-section"><h2>现有预约</h2><div class="stacked-cards">
{% for reservation in payload.reservations %}
  <article class="card"><div class="card-title"><strong>{{ reservation.actor_id }}</strong><span class="badge">{{ reservation.state }}</span></div>
    <p>{{ reservation.start_at }} → {{ reservation.end_at }}</p><p>{{ reservation.reason }}</p>
    <p>{% for gpu_id in reservation.gpu_ids %}<code>{{ gpu_id }}</code>{% if not loop.last %}、{% endif %}{% endfor %}</p>
    {% if actor.role in ['operator', 'admin'] and reservation.state == 'ACTIVE' %}<form method="post" action="/ui/action/cancel-reservation" class="inline-form"><input type="hidden" name="reservation_id" value="{{ reservation.id }}"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="confirmed" value="yes"><button class="danger" type="submit">取消安排</button></form>{% endif %}
  </article>
{% else %}<p class="empty">尚无未来预约。</p>{% endfor %}
</div></section>

{% elif page == 'identities' %}
<section class="list-section"><h2>预设任务</h2><p class="muted">一次性认领可直接填写任意项目标识，不需要先创建项目。预设任务仅用于反复执行时固定 GPU 数量、用途和可选服务器范围。</p><div class="stacked-cards">
{% for profile in payload.workload_profiles %}<article class="card"><div class="card-title"><strong>{{ profile.display_name }}</strong><span class="badge">{{ '启用' if profile.enabled else '禁用' }}</span></div><p><code>{{ profile.id }}</code> · {{ profile.project_id }}</p><p>{{ profile.constraints.gpu_count }} GPU · 服务器：{{ profile.constraints.endpoint_ids|join('、') }}</p><p class="muted">用途：{{ profile.purpose }}</p></article>{% else %}<p class="empty">尚无预设任务；一次性认领不需要预设。</p>{% endfor %}
</div></section>

{% if actor.is_admin %}
<section class="actions" aria-labelledby="identity-admin-title">
  <h2 id="identity-admin-title">身份与预设任务</h2>
  <details>
    <summary>创建或更新预设任务</summary>
    <form method="post" action="/ui/action/workload-profile" class="stack form-grid">
      <label>配置 ID<input name="id" required placeholder="wrbench-eval-2gpu"></label><label>显示名称<input name="display_name" required placeholder="WRBench 双卡评测"></label>
      <label>归属标识<input name="project_id" required placeholder="例如：storyboard"></label><label>GPU 数量<input name="gpu_count" type="number" min="1" value="1" required></label>
      <label>最大租约窗口<select name="duration_hours"><option value="1">1 小时</option><option value="2" selected>2 小时</option><option value="4">4 小时</option><option value="8">8 小时</option><option value="24">24 小时</option></select></label><label>状态<select name="enabled"><option value="true">启用</option><option value="false">禁用</option></select></label>
      <label class="span-all">允许服务器（可多选，留空则由 Broker 在全部服务器中调度）<select name="endpoint_ids" multiple size="6">{% for endpoint in payload.endpoints %}<option value="{{ endpoint.id }}">{{ endpoint.id }} · {{ endpoint.host }}:{{ endpoint.port }}</option>{% endfor %}</select></label>
      <label class="span-all">固定用途<textarea name="purpose" rows="2" required placeholder="例如：WRBench 图像/视频生成基准评测"></textarea></label>
      <p class="field-help span-all">归属标识可任意填写，无需预先创建。配置 ID 一旦创建不能改归属标识；完成即释放租约。最大租约窗口是调度与未来预约共同遵守的硬边界，不是每次申请的预计时长。精确 GPU 固定选择仍应走一次性申请。</p>
      <input type="hidden" name="csrf" value="{{ csrf }}"><label class="confirm span-all"><input type="checkbox" name="confirmed" value="yes" required> 我确认这是可由 Agent 按配置重复认领的资源合同。</label><button type="submit">保存预设任务</button>
    </form>
  </details>
  <details>
    <summary>创建人类或 Agent 身份</summary>
    <form method="post" action="/ui/action/actor" class="stack form-grid">
      <label>身份 ID<input name="id" required placeholder="agent-name"></label><label>显示名称<input name="display_name" required placeholder="Agent Name"></label>
      <label>角色<select name="role"><option value="viewer">只读 viewer</option><option value="allocator" selected>申请 allocator</option><option value="operator">运维 operator</option><option value="admin">管理员 admin</option><option value="collector">采集 collector</option></select></label><label>Token 标签<input name="token_label" value="generated" required></label>
      <input type="hidden" name="csrf" value="{{ csrf }}"><label class="confirm span-all"><input type="checkbox" name="confirmed" value="yes" required> 我确认 token 只会显示一次，并会安全保存。</label><button type="submit">创建身份并显示 token</button>
    </form>
  </details>
</section>

<section class="list-section"><h2>已登记身份</h2><div class="stacked-cards">
{% for item in payload.actors %}<article class="card"><div class="card-title"><strong>{{ item.display_name }}</strong><span class="badge">{{ item.role }}</span></div><p><code>{{ item.id }}</code></p>
  {% for token in item.tokens %}<div class="token-row">{{ token.label }} · {{ '已撤销' if token.revoked_at else '有效' }}{% if not token.revoked_at %}<form method="post" action="/ui/action/revoke-token" class="inline-form"><input type="hidden" name="token_id" value="{{ token.id }}"><input type="hidden" name="csrf" value="{{ csrf }}"><label class="confirm"><input type="checkbox" name="confirmed" value="yes" required> 确认撤销</label><button type="submit" class="danger">撤销</button></form>{% endif %}</div>{% endfor %}
</article>{% else %}<p class="empty">尚无身份。</p>{% endfor %}
</div></section>
{% endif %}

{% elif page == 'maintenance' %}
{% if actor.role in ['operator', 'admin'] %}
<section class="actions"><h2>创建维护窗口</h2><p class="muted">维护窗口将 fail closed，阻止受影响资源的新分配。</p>
  <form method="post" action="/ui/action/maintenance" class="stack form-grid">
    <label>维护对象<select name="target" required><optgroup label="整个 endpoint">{% for endpoint in payload.endpoints %}<option value="endpoint|{{ endpoint.id }}">{{ endpoint.id }}</option>{% endfor %}</optgroup><optgroup label="单张 GPU">{% for gpu in payload.gpus %}<option value="gpu|{{ gpu.id }}">{{ gpu.endpoint_id }} / GPU {{ gpu.gpu_index }}</option>{% endfor %}</optgroup></select></label>
    <label>时区<select name="timezone"><option value="Asia/Shanghai" selected>Asia/Shanghai</option><option value="UTC">UTC</option></select></label><label>开始时间<input name="start_at" type="datetime-local" required></label><label>结束时间<input name="end_at" type="datetime-local" required></label>
    <label class="span-all">原因<textarea name="reason" rows="3" required></textarea></label><input type="hidden" name="csrf" value="{{ csrf }}"><label class="confirm span-all"><input type="checkbox" name="confirmed" value="yes" required> 我确认这是调度保护，不会停止任何远端进程。</label><button type="submit">创建维护窗口</button>
  </form>
</section>
{% endif %}
<section class="list-section"><h2>维护窗口</h2><div class="stacked-cards">{% for item in payload.maintenance %}<article class="card"><div class="card-title"><strong>{{ item.endpoint_id or item.gpu_id }}</strong><span class="badge">{{ item.state }}</span></div><p>{{ item.start_at }} → {{ item.end_at }}</p><p>{{ item.reason }}</p></article>{% else %}<p class="empty">尚无维护窗口。</p>{% endfor %}</div></section>

{% elif page == 'alerts' %}
<section class="list-section"><h2>告警</h2><div class="stacked-cards">
{% for alert in payload.alerts %}<article class="card state-{{ 'unhealthy' if alert.active else 'available' }}"><div class="card-title"><strong>{{ alert.type }}</strong><span class="badge">{{ alert.severity }}</span></div><p>{{ alert.message }}</p><p class="muted">{{ alert.resource_type }} · {{ alert.resource_id }} · 最近更新 {{ alert.last_seen_at }}</p>{% if alert.active and not alert.acknowledged_at and actor.role in ['operator', 'admin'] %}<form method="post" action="/ui/action/ack-alert" class="inline-form"><input type="hidden" name="alert_id" value="{{ alert.id }}"><input type="hidden" name="csrf" value="{{ csrf }}"><label>备注<input name="note" placeholder="已查看"></label><label class="confirm"><input type="checkbox" name="confirmed" value="yes" required> 确认已查看</label><button type="submit">确认告警</button></form>{% elif alert.acknowledged_at %}<p class="muted">已由 {{ alert.acknowledged_by }} 于 {{ alert.acknowledged_at }} 确认。</p>{% endif %}</article>{% else %}<p class="empty">没有可见告警。</p>{% endfor %}
</div></section>

{% elif page == 'audit' %}
<section class="quick-actions"><a class="button-link" href="/api/v1/events/export.csv">导出可见审计 CSV</a></section>
<section class="list-section"><h2>审计事件</h2><div class="stacked-cards">{% for event in payload.events %}<article class="card"><div class="card-title"><strong>{{ event.action }}</strong><span class="badge">{{ event.result }}</span></div><p><code>{{ event.resource_type }} / {{ event.resource_id }}</code></p><p class="muted">{{ event.created_at }} · {{ event.actor_id }}</p></article>{% else %}<p class="empty">还没有可见的审计事件。</p>{% endfor %}</div></section>

{% else %}
<section class="data-panel"><h2>Doctor</h2><pre tabindex="0">{{ payload.doctor | tojson(indent=2) }}</pre></section>
{% if actor.role in ['operator', 'admin'] %}
<section class="actions"><h2>安全运维操作</h2><form method="post" action="/ui/action/reconcile" class="inline-form"><input type="hidden" name="csrf" value="{{ csrf }}"><label class="confirm"><input type="checkbox" name="confirmed" value="yes" required> 确认运行安全对账</label><button type="submit">运行对账</button></form>
{% if actor.is_admin %}<form method="post" action="/ui/action/prune-telemetry" class="inline-form"><input type="hidden" name="csrf" value="{{ csrf }}"><label>保留天数<input name="retention_days" type="number" min="1" value="7" required></label><label class="confirm"><input type="checkbox" name="confirmed" value="yes" required> 确认清理旧 telemetry（审计和租约不会删除）</label><button type="submit" class="danger">清理 telemetry</button></form>{% endif %}</section>
{% endif %}
<section class="data-panel"><h2>扩展接口</h2><p>人类通过本界面完成日常操作；自动化与新增能力通过同一套 <a href="/docs">OpenAPI</a>、CLI 和 MCP 接口接入，不需要复制调度逻辑。</p></section>
{% endif %}
{% endblock %}

```

## Authentication and token pages

- Source: `src/gpu_broker/web/templates/login.html + token_created.html`
- Description: Unauthenticated and secret-issuance shells that extend the global layout.

```html
{% extends "base.html" %}
{% block content %}
<section class="login-panel" aria-labelledby="login-title">
  <p class="eyebrow">本机、协作式 GPU 资源控制面</p>
  <h1 id="login-title">登录 gpu-broker</h1>
  <p>输入只保存在本地秘密管理中的 API token。服务器只保存 token hash；租约不代表训练或服务启动授权。</p>
  <form method="post" action="/ui/login" class="stack">
    <label>API token
      <input id="api-token" name="token" type="password" autocomplete="current-password" required autofocus>
    </label>
    <button id="paste-desktop-token" class="quiet" type="button" hidden>从系统剪贴板粘贴 token</button>
    <p id="desktop-paste-status" class="muted" role="status" hidden></p>
    <button type="submit">登录</button>
  </form>
</section>
<script>
  (() => {
    const input = document.getElementById("api-token");
    const button = document.getElementById("paste-desktop-token");
    const status = document.getElementById("desktop-paste-status");
    const bridge = window.webkit?.messageHandlers?.gpuBrokerClipboard;
    if (!input || !button || !status || !bridge) return;

    button.hidden = false;
    const setStatus = (message) => {
      status.textContent = message;
      status.hidden = false;
    };
    window.gpuBrokerSetToken = (token) => {
      input.value = token;
      input.focus();
      setStatus("已填入 token；请点击“登录”。");
    };
    window.gpuBrokerClipboardError = (message) => setStatus(message);
    button.addEventListener("click", () => {
      setStatus("正在从系统剪贴板读取…");
      bridge.postMessage({ action: "paste-token" });
    });
  })();
</script>
{% endblock %}
{% extends "base.html" %}
{% block content %}
<section class="page-heading">
  <div>
    <p class="eyebrow">一次性凭证</p>
    <h1>已创建 {{ payload.actor.display_name }}</h1>
  </div>
</section>

<section class="secret-panel" aria-labelledby="token-title">
  <h2 id="token-title">立即复制并安全保存此 token</h2>
  <p>它只会在此响应中显示一次。请将其交给对应的人类或 Agent 的秘密管理工具；不要复制到项目配置、审计记录或聊天记录。</p>
  <label>API token
    <input id="issued-token" type="text" value="{{ payload.token }}" readonly autocomplete="off">
  </label>
  <div class="inline-actions">
    <button type="button" id="copy-issued-token">复制 token</button>
    <a class="button-link quiet-link" href="/ui/identities">我已安全保存</a>
  </div>
  <p id="copy-status" class="muted" role="status"></p>
</section>

<script>
(() => {
  const input = document.getElementById("issued-token");
  const button = document.getElementById("copy-issued-token");
  const status = document.getElementById("copy-status");
  if (!input || !button || !navigator.clipboard) return;
  button.addEventListener("click", async () => {
    await navigator.clipboard.writeText(input.value);
    status.textContent = "已复制。请确认它已保存到受权限保护的秘密管理工具。";
  });
})();
</script>
{% endblock %}

```

## Current macOS desktop shell

- Source: `desktop/GPU Broker.swift`
- Description: AppKit window that starts the loopback service and hosts the entire interface in WKWebView. This is the dependency that must be replaced before the web UI can be deleted.

```swift
import AppKit
import Darwin
import Foundation
import WebKit

private enum DesktopError: LocalizedError {
    case projectRootMissing
    case uvMissing
    case serverExecutableMissing
    case commandFailed(String)

    var errorDescription: String? {
        switch self {
        case .projectRootMissing:
            return "找不到 gpu-broker 项目目录。请将 GPU Broker.app 保留在项目的 dist/ 目录，或设置 GPU_BROKER_ROOT。"
        case .uvMissing:
            return "找不到 uv。请先安装 uv，或设置 GPU_BROKER_UV 指向它的绝对路径。"
        case .serverExecutableMissing:
            return "初始化完成，但找不到项目虚拟环境中的 gpu-broker 可执行文件。"
        case .commandFailed(let details):
            return details
        }
    }
}

final class DesktopAppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKScriptMessageHandler {
    private let port = 8787
    private var window: NSWindow?
    private var webView: WKWebView?
    private var serverProcess: Process?
    private var isStarting = false

    private lazy var projectRoot: URL? = {
        if let configured = ProcessInfo.processInfo.environment["GPU_BROKER_ROOT"], !configured.isEmpty {
            return URL(fileURLWithPath: configured, isDirectory: true)
        }
        let bundleParent = Bundle.main.bundleURL.deletingLastPathComponent()
        return findProjectRoot(startingAt: bundleParent)
    }()

    private var baseURL: URL {
        URL(string: "http://127.0.0.1:\(port)/")!
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        configureMainMenu()
        let contentRect = NSRect(x: 0, y: 0, width: 1440, height: 820)
        let createdWindow = NSWindow(
            contentRect: contentRect,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        createdWindow.title = "GPU Broker"
        createdWindow.titleVisibility = .hidden
        createdWindow.titlebarAppearsTransparent = true
        createdWindow.toolbarStyle = .unifiedCompact
        createdWindow.titlebarSeparatorStyle = .none
        createdWindow.backgroundColor = .windowBackgroundColor
        createdWindow.minSize = NSSize(width: 1024, height: 640)
        createdWindow.center()
        createdWindow.delegate = self

        let configuration = WKWebViewConfiguration()
        configuration.userContentController.add(self, name: "gpuBrokerClipboard")
        let view = WKWebView(frame: contentRect, configuration: configuration)
        view.autoresizingMask = [.width, .height]
        createdWindow.contentView = view
        window = createdWindow
        webView = view
        createdWindow.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        connectOrStartServer()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let process = serverProcess, process.isRunning {
            process.terminate()
        }
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard
            message.name == "gpuBrokerClipboard",
            message.webView === webView,
            message.webView?.url?.host == "127.0.0.1",
            message.webView?.url?.port == port,
            let body = message.body as? [String: Any],
            let action = body["action"] as? String,
            ["paste-ssh-command", "paste-token"].contains(action)
        else {
            return
        }

        guard let text = NSPasteboard.general.string(forType: .string), !text.isEmpty else {
            deliverClipboardError("系统剪贴板中没有可粘贴的文本。")
            return
        }
        let callback = action == "paste-ssh-command" ? "gpuBrokerSetSSHCommand" : "gpuBrokerSetToken"
        deliverClipboardText(text, to: callback)
    }

    private func configureMainMenu() {
        let mainMenu = NSMenu()

        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "退出 GPU Broker", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appMenuItem.submenu = appMenu

        let editMenuItem = NSMenuItem()
        mainMenu.addItem(editMenuItem)
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editMenuItem.submenu = editMenu

        NSApp.mainMenu = mainMenu
    }

    private func deliverClipboardText(_ text: String, to callback: String) {
        guard let argument = jsonArgument(text) else {
            deliverClipboardError("无法读取系统剪贴板文本。")
            return
        }
        webView?.evaluateJavaScript("window.\(callback)?.(\(argument));")
    }

    private func deliverClipboardError(_ message: String) {
        guard let argument = jsonArgument(message) else { return }
        webView?.evaluateJavaScript("window.gpuBrokerClipboardError?.(\(argument));")
    }

    private func jsonArgument(_ value: String) -> String? {
        guard
            let data = try? JSONSerialization.data(withJSONObject: [value]),
            let array = String(data: data, encoding: .utf8),
            array.count >= 2
        else {
            return nil
        }
        return String(array.dropFirst().dropLast())
    }

    private func findProjectRoot(startingAt url: URL) -> URL? {
        var candidate = url.standardizedFileURL
        let fileManager = FileManager.default
        while candidate.path != "/" {
            let projectFile = candidate.appendingPathComponent("pyproject.toml")
            let inventory = candidate.appendingPathComponent("configs/inventory.yaml")
            if fileManager.fileExists(atPath: projectFile.path) && fileManager.fileExists(atPath: inventory.path) {
                return candidate
            }
            candidate.deleteLastPathComponent()
        }
        return nil
    }

    private func uvExecutable() -> URL? {
        let environment = ProcessInfo.processInfo.environment
        let home = environment["HOME"] ?? NSHomeDirectory()
        let candidates = [
            environment["GPU_BROKER_UV"],
            "\(home)/.local/bin/uv",
            "/opt/homebrew/bin/uv",
            "/usr/local/bin/uv"
        ].compactMap { $0 }
        return candidates
            .map { URL(fileURLWithPath: $0) }
            .first(where: { FileManager.default.isExecutableFile(atPath: $0.path) })
    }

    private func processEnvironment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        if let root = projectRoot {
            environment["GPU_BROKER_PROJECT_ROOT"] = root.path
        }
        return environment
    }

    private func connectOrStartServer(attempt: Int = 0) {
        healthCheck { [weak self] ready in
            DispatchQueue.main.async {
                guard let self else { return }
                if ready {
                    self.webView?.load(URLRequest(url: self.baseURL))
                    return
                }
                if self.serverProcess == nil && !self.isStarting {
                    self.initializeAndStartServer()
                    return
                }
                if attempt < 80 {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
                        self.connectOrStartServer(attempt: attempt + 1)
                    }
                } else {
                    self.showFatalError("本机 GPU Broker 服务未能在规定时间内启动。请检查项目依赖和 state 目录。")
                }
            }
        }
    }

    private func healthCheck(completion: @escaping (Bool) -> Void) {
        DispatchQueue.global(qos: .utility).async { [port] in
            let descriptor = Darwin.socket(AF_INET, SOCK_STREAM, 0)
            guard descriptor >= 0 else {
                completion(false)
                return
            }
            defer { Darwin.close(descriptor) }

            var timeout = timeval(tv_sec: 0, tv_usec: 800_000)
            withUnsafePointer(to: &timeout) { pointer in
                _ = Darwin.setsockopt(
                    descriptor,
                    SOL_SOCKET,
                    SO_RCVTIMEO,
                    pointer,
                    socklen_t(MemoryLayout<timeval>.size)
                )
            }
            var address = sockaddr_in()
            address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
            address.sin_family = sa_family_t(AF_INET)
            address.sin_port = in_port_t(port).bigEndian
            guard inet_pton(AF_INET, "127.0.0.1", &address.sin_addr) == 1 else {
                completion(false)
                return
            }
            let connected = withUnsafePointer(to: &address) { pointer in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    Darwin.connect(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
            guard connected == 0 else {
                completion(false)
                return
            }

            let request = "GET /health/live HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n"
            let sent = request.utf8CString.withUnsafeBytes { bytes in
                Darwin.send(descriptor, bytes.baseAddress, bytes.count - 1, 0)
            }
            guard sent > 0 else {
                completion(false)
                return
            }
            var buffer = [UInt8](repeating: 0, count: 256)
            let received = buffer.withUnsafeMutableBytes { bytes in
                Darwin.recv(descriptor, bytes.baseAddress, bytes.count, 0)
            }
            guard received > 0 else {
                completion(false)
                return
            }
            let header = String(decoding: buffer.prefix(received), as: UTF8.self)
            completion(header.hasPrefix("HTTP/1.1 200") || header.hasPrefix("HTTP/1.0 200"))
        }
    }

    private func initializeAndStartServer() {
        guard let root = projectRoot else {
            showFatalError(DesktopError.projectRootMissing.localizedDescription)
            return
        }
        guard let uv = uvExecutable() else {
            showFatalError(DesktopError.uvMissing.localizedDescription)
            return
        }
        isStarting = true
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                _ = try self.runCommand(
                    executable: uv,
                    arguments: [
                        "run", "--no-editable", "--reinstall-package", "gpu-broker",
                        "gpu-broker", "init", "--db", "state/gpu-broker.sqlite3",
                        "--inventory", "configs/inventory.yaml"
                    ],
                    root: root
                )
                let serverExecutable = root.appendingPathComponent(".venv/bin/gpu-broker")
                guard FileManager.default.isExecutableFile(atPath: serverExecutable.path) else {
                    throw DesktopError.serverExecutableMissing
                }
                DispatchQueue.main.async {
                    do {
                        try self.startServer(executable: serverExecutable, root: root)
                        self.isStarting = false
                        self.connectOrStartServer()
                    } catch {
                        self.isStarting = false
                        self.showFatalError(error.localizedDescription)
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    self.isStarting = false
                    self.showFatalError(error.localizedDescription)
                }
            }
        }
    }

    private func runCommand(executable: URL, arguments: [String], root: URL) throws -> String {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.currentDirectoryURL = root
        process.environment = processEnvironment()
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        let details = String(data: data, encoding: .utf8) ?? ""
        guard process.terminationStatus == 0 else {
            throw DesktopError.commandFailed("初始化本机状态失败：\(details)")
        }
        return details
    }

    private func startServer(executable: URL, root: URL) throws {
        let process = Process()
        process.executableURL = executable
        process.arguments = [
            "serve", "--db", "state/gpu-broker.sqlite3",
            "--inventory", "configs/inventory.yaml", "--host", "127.0.0.1", "--port", "\(port)"
        ]
        process.currentDirectoryURL = root
        process.environment = processEnvironment()
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()
        serverProcess = process
    }

    private func showFatalError(_ message: String) {
        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = "无法启动 GPU Broker"
        alert.informativeText = message
        alert.addButton(withTitle: "退出")
        alert.runModal()
        NSApp.terminate(nil)
    }
}

let application = NSApplication.shared
application.setActivationPolicy(.regular)
let delegate = DesktopAppDelegate()
application.delegate = delegate
application.run()

```
