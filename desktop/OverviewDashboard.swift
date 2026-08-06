import AppKit
import SwiftUI

struct FleetOverview: View {
    let snapshot: BrokerSnapshot
    let openEndpoint: (EndpointRecord) -> Void
    let selectGPU: (GPURecord) -> Void
    let openServerPool: () -> Void

    private let maximumVisibleAttentionItems = 8

    private let attentionStates = Set([
        "BUSY_UNMANAGED", "UNKNOWN_RECOVERING", "UNKNOWN_STALE",
        "UNHEALTHY", "CONFLICT", "ORPHANED_BUSY", "DRAINING", "RETIRED",
        "DISABLED", "MAINTENANCE"
    ])

    private var attentionEndpoints: [EndpointRecord] {
        snapshot.operationalEndpoints.filter { ["ERROR", "STALE", "DRAINING", "DISABLED"].contains($0.monitorStatus) }
    }

    private var attentionGPUs: [GPURecord] {
        snapshot.operationalGPUs.filter { attentionStates.contains($0.state) }
    }

    private var visibleAttentionEndpoints: [EndpointRecord] {
        Array(attentionEndpoints.prefix(min(4, maximumVisibleAttentionItems)))
    }

    private var visibleAttentionGPUs: [GPURecord] {
        let remainingSlots = max(0, maximumVisibleAttentionItems - visibleAttentionEndpoints.count)
        return Array(attentionGPUs.prefix(remainingSlots))
    }

    private var hiddenAttentionCount: Int {
        max(
            0,
            attentionEndpoints.count + attentionGPUs.count
                - visibleAttentionEndpoints.count - visibleAttentionGPUs.count
        )
    }

    private var freshEndpointIDs: Set<String> {
        Set(snapshot.operationalEndpoints.filter { $0.monitorStatus == "ONLINE" }.map(\.id))
    }

    private var freshGPUCount: Int {
        snapshot.operationalGPUs.filter { freshEndpointIDs.contains($0.endpointID) }.count
    }

    private var allocatableGPUCount: Int {
        snapshot.operationalGPUs.filter {
            freshEndpointIDs.contains($0.endpointID) && $0.state == "AVAILABLE"
        }.count
    }

    private var runningTaskCount: Int {
        let linkedLeaseIDs = Set(snapshot.resourceClaims.flatMap(\.nativeLeaseIDs))
        let generic = snapshot.resourceClaims.filter {
            $0.runtimeState == "RUNNING" || $0.state == "RUNNING"
        }.count
        let legacy = snapshot.leases.filter {
            !linkedLeaseIDs.contains($0.id) && $0.runtimeState == "RUNNING"
        }.count
        return generic + legacy
    }

    private var assignedWaitingCount: Int {
        let linkedLeaseIDs = Set(snapshot.resourceClaims.flatMap(\.nativeLeaseIDs))
        let generic = snapshot.resourceClaims.filter {
            ["HELD", "ACTIVE"].contains($0.state) && $0.runtimeState != "RUNNING"
        }.count
        let legacy = snapshot.leases.filter {
            !linkedLeaseIDs.contains($0.id) && $0.runtimeState != "RUNNING"
        }.count
        return generic + legacy
    }

    private var queuedCoordinationCount: Int {
        let linkedRequestIDs = Set(snapshot.resourceClaims.flatMap(\.nativeRequestIDs))
        let generic = snapshot.resourceClaims.filter {
            ["BLOCKED", "QUEUED", "PENDING_APPROVAL", "REQUESTED"].contains($0.state)
        }.count
        let legacy = snapshot.requests.filter { !linkedRequestIDs.contains($0.id) }.count
        return generic + legacy
    }

    private var monitoringProviders: [ResourceProviderRecord] {
        snapshot.monitoringProviders.filter {
            !($0.providerType == "direct-gpu" && $0.total.gpuCount == 0 && $0.available.gpuCount == 0)
        }
    }

    private var hostCapacityProviders: [ResourceProviderRecord] {
        monitoringProviders.filter { $0.providerType == "host-capacity" }
    }

    private var schedulerProviders: [ResourceProviderRecord] {
        monitoringProviders.filter { $0.providerType == "scheduler" }
    }

    private var availableHostCPU: Int {
        Int(snapshot.operationalEndpoints.compactMap(\.availableCPUCores).reduce(0, +).rounded())
    }

    private var availableHostMemoryGiB: Int {
        snapshot.operationalEndpoints
            .filter { $0.monitorStatus == "ONLINE" }
            .compactMap(\.memoryAvailableMiB)
            .reduce(0, +) / 1024
    }

    private var totalHostCPU: Int {
        snapshot.operationalEndpoints.compactMap(\.cpuCount).reduce(0, +)
    }

    private var totalHostMemoryGiB: Int {
        snapshot.operationalEndpoints.compactMap(\.memoryTotalMiB).reduce(0, +) / 1024
    }

    private var onlineEndpointCount: Int {
        snapshot.operationalEndpoints.filter { $0.monitorStatus == "ONLINE" }.count
    }

    private var hasCoordinationSignals: Bool {
        assignedWaitingCount > 0 || queuedCoordinationCount > 0
            || !attentionEndpoints.isEmpty || !attentionGPUs.isEmpty
    }

    private var hasPlanningHistory: Bool {
        !snapshot.resourcePlanEvaluations.isEmpty
            || !snapshot.resourceRunActuals.isEmpty
            || !snapshot.resourceClaims.isEmpty
    }

    private var hasActiveUsage: Bool {
        !snapshot.leases.isEmpty || !snapshot.requests.isEmpty || !snapshot.reservations.isEmpty
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                overviewHeader
                if snapshot.operationalEndpoints.isEmpty {
                    emptyFleetState
                } else {
                    summaryGrid
                    serverPool
                }
                if !attentionEndpoints.isEmpty || !attentionGPUs.isEmpty {
                    attentionSection
                }
                if hasCoordinationSignals {
                    coordinationSignals
                }
                if !monitoringProviders.isEmpty {
                    resourceProjectionSection
                }
                if hasPlanningHistory {
                    planningAuditSection
                }
                if hasActiveUsage {
                    leaseSection
                }

                if !snapshot.operationalEndpoints.isEmpty || hasActiveUsage {
                    Label(snapshot.admissionBoundary, systemImage: "hand.raised.fill")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.horizontal, 24)
            .padding(.bottom, 16)
            .frame(maxWidth: .infinity, alignment: .topLeading)
        }
        .accessibilityLabel("资源总览")
    }

    private var overviewHeader: some View {
        HStack(alignment: .bottom, spacing: 12) {
            overviewHeading
            Spacer(minLength: 12)
            overviewCapacityBadge
        }
        .padding(.top, 4)
    }

    private var overviewHeading: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("实时概览")
                .font(.system(size: 22, weight: .bold))
            Text("CPU、内存与 GPU 的实时容量和可用状态")
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
                .lineLimit(1)
        }
    }

    private var overviewCapacityBadge: some View {
        Text("\(snapshot.operationalEndpoints.count) 台已登记 · \(onlineEndpointCount) 台在线")
            .font(.system(size: 11, weight: .semibold))
            .foregroundStyle(DesignTokens.ink)
            .lineLimit(1)
            .padding(.horizontal, 11)
            .padding(.vertical, 7)
            .background(DesignTokens.surface, in: Capsule())
            .overlay(Capsule().stroke(DesignTokens.surfaceStroke, lineWidth: 1))
    }

    private var summaryGrid: some View {
        LazyVGrid(columns: summaryColumns, spacing: 12) {
            OverviewSummaryCard(
                title: "服务器",
                value: "\(onlineEndpointCount) / \(snapshot.operationalEndpoints.count)",
                detail: "在线 / 已登记",
                icon: "server.rack",
                color: DesignTokens.network
            )
            OverviewSummaryCard(
                title: "CPU",
                value: "\(availableHostCPU) / \(totalHostCPU)",
                detail: "可用核 / 总核",
                icon: "cpu",
                color: DesignTokens.cpu
            )
            OverviewSummaryCard(
                title: "内存",
                value: "\(availableHostMemoryGiB) / \(totalHostMemoryGiB) GB",
                detail: "可用 / 总容量",
                icon: "memorychip",
                color: DesignTokens.memory
            )
            OverviewSummaryCard(
                title: "GPU",
                value: "\(allocatableGPUCount) / \(snapshot.operationalGPUs.count)",
                detail: "可分配 / 已登记",
                icon: "square.stack.3d.up.fill",
                color: DesignTokens.gpu
            )
            OverviewSummaryCard(
                title: "运行中的任务",
                value: "\(runningTaskCount)",
                detail: "\(assignedWaitingCount) 个已分配任务等待运行",
                icon: "key.fill",
                color: DesignTokens.interaction
            )
            OverviewSummaryCard(
                title: "需要关注",
                value: "\(attentionEndpoints.count + attentionGPUs.count)",
                detail: queuedCoordinationCount > 0
                    ? "另有 \(queuedCoordinationCount) 个等待中的资源申请"
                    : "没有等待中的资源申请",
                icon: "exclamationmark.triangle.fill",
                color: attentionEndpoints.isEmpty && attentionGPUs.isEmpty ? DesignTokens.success : DesignTokens.danger
            )
        }
    }

    private var coordinationSignals: some View {
        LazyVGrid(columns: columns, spacing: 8) {
            CoordinationSignal(
                title: "已分配未运行",
                value: "\(assignedWaitingCount)",
                detail: "资源已分配，但尚未检测到任务",
                icon: "pause.circle",
                color: assignedWaitingCount > 0 ? DesignTokens.warning : DesignTokens.mutedInk
            )
            CoordinationSignal(
                title: "排队请求",
                value: "\(queuedCoordinationCount)",
                detail: "等待分配的资源申请",
                icon: "hourglass",
                color: queuedCoordinationCount > 0 ? DesignTokens.warning : DesignTokens.mutedInk
            )
            CoordinationSignal(
                title: "方案比较",
                value: "\(snapshot.resourcePlanEvaluations.count)",
                detail: "只推荐收益明确的资源方案",
                icon: "chart.line.uptrend.xyaxis",
                color: snapshot.resourcePlanEvaluations.isEmpty ? DesignTokens.mutedInk : DesignTokens.interaction
            )
            CoordinationSignal(
                title: "任务记录",
                value: "\(snapshot.resourceRunActuals.count)",
                detail: "预计时间与实际时间",
                icon: "checklist.checked",
                color: snapshot.resourceRunActuals.isEmpty ? DesignTokens.mutedInk : DesignTokens.success
            )
            CoordinationSignal(
                title: "需要处理",
                value: "\(attentionEndpoints.count + attentionGPUs.count)",
                detail: "连接、数据或资源状态异常",
                icon: "exclamationmark.triangle.fill",
                color: (attentionEndpoints.isEmpty && attentionGPUs.isEmpty) ? DesignTokens.mutedInk : DesignTokens.danger
            )
        }
    }

    private var resourceProjectionSection: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text(monitoringProviders.allSatisfy { $0.providerType == "scheduler" }
                    ? "外部计算平台"
                    : "可用资源")
                    .font(.system(size: 13, weight: .semibold))
                Text("\(monitoringProviders.count)")
                    .font(.system(size: 10, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.mutedInk)
                Spacer()
                Text(monitoringProviders.allSatisfy { $0.providerType == "scheduler" }
                    ? "资源状态由外部调度系统确认"
                    : "按服务器和资源来源查看")
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }

            LazyVGrid(columns: [GridItem(.adaptive(minimum: 260, maximum: 420), spacing: 8)], spacing: 8) {
                ForEach(monitoringProviders) { provider in
                    ResourceProjectionCard(
                        provider: provider,
                        unitCount: snapshot.allocatableUnits.filter { $0.providerID == provider.id }.count
                    )
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("可用资源")
    }

    private var planningAuditSection: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text("资源方案")
                    .font(.system(size: 13, weight: .semibold))
                Text("\(snapshot.resourcePlanEvaluations.count)")
                    .font(.system(size: 10, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.mutedInk)
                Spacer()
                Text("查看资源选择和预计收益")
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }

            if snapshot.resourcePlanEvaluations.isEmpty && snapshot.resourceRunActuals.isEmpty && snapshot.resourceClaims.isEmpty {
                Label("暂无资源方案", systemImage: "clock")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .frame(maxWidth: .infinity, minHeight: 46, alignment: .leading)
                    .padding(.horizontal, 13)
                    .overviewSurface(radius: 9)
            } else {
                LazyVStack(spacing: 6) {
                    ForEach(snapshot.resourceClaims.prefix(4)) { claim in
                        ResourceClaimOverviewRow(claim: claim)
                    }
                    ForEach(snapshot.resourcePlanEvaluations.prefix(4)) { evaluation in
                        ResourcePlanOverviewRow(evaluation: evaluation)
                    }
                    ForEach(snapshot.resourceRunActuals.prefix(4)) { actual in
                        ResourceActualOverviewRow(actual: actual)
                    }
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("资源方案")
    }

    private var attentionSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("需要处理")
                    .font(.system(size: 12, weight: .semibold))
                Spacer()
                if !attentionEndpoints.isEmpty || !attentionGPUs.isEmpty {
                    Text("\(attentionEndpoints.count + attentionGPUs.count) 项")
                        .font(.system(size: 10, weight: .semibold, design: .rounded))
                        .foregroundStyle(DesignTokens.mutedInk)
                }
            }

            if attentionEndpoints.isEmpty && attentionGPUs.isEmpty {
                Label("所有服务器状态正常", systemImage: "checkmark.circle.fill")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(DesignTokens.success)
                    .frame(maxWidth: .infinity, minHeight: 48, alignment: .leading)
                    .padding(.horizontal, 13)
                    .overviewSurface(radius: 10)
            } else {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 260, maximum: 420), spacing: 8)], spacing: 8) {
                    ForEach(visibleAttentionEndpoints) { endpoint in
                        Button { openEndpoint(endpoint) } label: {
                            AttentionCard(
                                title: endpoint.sshCommand,
                                detail: endpoint.monitorDetail ?? endpoint.monitorLabel,
                                icon: endpointIcon(endpoint.monitorStatus)
                            )
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("服务器需要处理")
                        .accessibilityValue("\(endpoint.displayName)，\(endpoint.monitorLabel)")
                    }
                    ForEach(visibleAttentionGPUs) { gpu in
                        Button { selectGPU(gpu) } label: {
                            AttentionCard(
                                title: "GPU \(gpu.index) · \(gpu.name)",
                                detail: overviewGPUStateLabel(gpu.state),
                                icon: overviewGPUStateIcon(gpu.state)
                            )
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("GPU 需要处理")
                        .accessibilityValue("GPU \(gpu.index)，\(overviewGPUStateLabel(gpu.state))")
                    }
                }
                if hiddenAttentionCount > 0 {
                    Button(action: openServerPool) {
                        HStack(spacing: 8) {
                            Label("另有 \(hiddenAttentionCount) 项需要处理", systemImage: "ellipsis.circle.fill")
                                .font(.system(size: 10, weight: .semibold))
                            Spacer(minLength: 8)
                                Text("查看全部服务器")
                                .font(.system(size: 10, weight: .semibold))
                            Image(systemName: "arrow.right")
                                .font(.system(size: 9, weight: .semibold))
                        }
                        .foregroundStyle(DesignTokens.interaction)
                        .padding(.horizontal, 12)
                        .frame(maxWidth: .infinity, minHeight: 40, alignment: .leading)
                        .overviewSurface(radius: 9)
                    }
                    .buttonStyle(.plain)
                    .help("打开服务器页面查看全部问题")
                    .accessibilityLabel("另有 \(hiddenAttentionCount) 项需要处理，打开服务器页面查看")
                }
            }
        }
    }

    private var serverPool: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text("服务器")
                    .font(.system(size: 13, weight: .semibold))
                Text("\(snapshot.operationalEndpoints.count)")
                    .font(.system(size: 10, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.mutedInk)
                Spacer()
                Text(snapshotAgeLabel)
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }

            LazyVGrid(columns: serverColumns, alignment: .leading, spacing: 12) {
                ForEach(snapshot.operationalEndpoints) { endpoint in
                    OverviewServerCard(
                        endpoint: endpoint,
                        gpus: snapshot.gpus(for: endpoint),
                        open: { openEndpoint(endpoint) },
                        selectGPU: selectGPU
                    )
                }
            }
        }
    }

    private var emptyFleetState: some View {
        VStack(spacing: 10) {
            Image(systemName: "server.rack")
                .font(.system(size: 25, weight: .semibold))
                .foregroundStyle(DesignTokens.interaction)
                .frame(width: 52, height: 52)
                .background(DesignTokens.selection, in: Circle())
            Text("还没有服务器")
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(DesignTokens.ink)
            Text("添加服务器后，这里会显示 CPU、内存、GPU 和正在使用的任务。")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
                .multilineTextAlignment(.center)
            Button("前往服务器", action: openServerPool)
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
        }
        .frame(maxWidth: .infinity, minHeight: 190)
        .overviewSurface(radius: 14)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("还没有服务器。前往服务器页面添加服务器。")
    }

    private var leaseSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("正在使用")
                    .font(.system(size: 13, weight: .semibold))
                Text("\(snapshot.leases.count)")
                    .font(.system(size: 10, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.mutedInk)
                Spacer()
                Text("\(snapshot.requests.count) 个排队 · \(snapshot.reservations.count) 个预约")
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }

            LazyVStack(spacing: 6) {
                if snapshot.leases.isEmpty {
                    Text("当前没有正在使用的资源")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .frame(maxWidth: .infinity, minHeight: 42, alignment: .leading)
                        .padding(.horizontal, 13)
                        .overviewSurface(radius: 9)
                } else {
                    ForEach(snapshot.leases) { lease in
                        OverviewLeaseRow(
                            lease: lease,
                            gpuLabel: gpuLabel(for: lease)
                        )
                    }
                }
                ForEach(snapshot.reservations) { reservation in
                    ReservationOverviewRow(reservation: reservation)
                }
            }
        }
    }

    private var columns: [GridItem] {
        [GridItem(.adaptive(minimum: 180, maximum: 320), spacing: 10)]
    }

    private var summaryColumns: [GridItem] {
        Array(repeating: GridItem(.flexible(minimum: 0), spacing: 12), count: 3)
    }

    private var serverColumns: [GridItem] {
        Array(repeating: GridItem(.flexible(minimum: 360), spacing: 12), count: 2)
    }

    private func gpuLabel(for lease: LeaseRecord) -> String {
        let indexed = lease.gpuIDs.compactMap { gpuID in
            snapshot.gpus.first(where: { $0.id == gpuID }).map { "\($0.index)" }
        }
        if indexed.count == lease.gpuIDs.count, !indexed.isEmpty {
            return indexed.joined(separator: " · ")
        }
        return lease.gpuIDs.map { String($0.suffix(6)) }.joined(separator: " · ")
    }

    private var snapshotAgeLabel: String {
        guard let age = snapshot.dataAgeSeconds else { return "等待数据" }
        if age < 5 { return "刚刚更新" }
        return "\(Int(age.rounded())) 秒前更新"
    }
}

private struct OverviewSummaryCard: View {
    let title: String
    let value: String
    let detail: String
    let icon: String
    let color: Color

    var body: some View {
        HStack(spacing: 13) {
            Image(systemName: icon)
                .font(.system(size: 18, weight: .semibold))
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(color)
                .frame(width: 42, height: 42)
                .background(color.opacity(0.14), in: RoundedRectangle(cornerRadius: 11, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(DesignTokens.mutedInk)
                Text(value)
                    .font(.system(size: 19, weight: .bold, design: .rounded))
                    .lineLimit(1)
                    .minimumScaleFactor(0.82)
                Text(detail)
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)
        }
        .padding(13)
        .frame(minHeight: 78)
        .overviewSurface(radius: 14)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(title)
        .accessibilityValue("\(value)，\(detail)")
    }
}

private struct AttentionCard: View {
    let title: String
    let detail: String
    let icon: String

    var body: some View {
        HStack(spacing: 9) {
            Image(systemName: icon)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(DesignTokens.danger)
                .frame(width: 28, height: 28)
                .background(DesignTokens.danger.opacity(0.10), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.system(size: 10, weight: .semibold, design: .monospaced))
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(detail)
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
            }
            Spacer(minLength: 6)
            Image(systemName: "chevron.right")
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(DesignTokens.mutedInk)
        }
        .padding(.horizontal, 11)
        .frame(maxWidth: .infinity, minHeight: 50, alignment: .leading)
        .overviewSurface(radius: 9)
    }
}

private struct CoordinationSignal: View {
    let title: String
    let value: String
    let detail: String
    let icon: String
    let color: Color

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: icon)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(color)
                .frame(width: 24, height: 24)
                .background(color.opacity(0.10), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
            VStack(alignment: .leading, spacing: 1) {
                Text("\(title) · \(value)")
                    .font(.system(size: 10, weight: .semibold, design: .rounded))
                Text(detail)
                    .font(.system(size: 8, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 10)
        .frame(maxWidth: .infinity, minHeight: 42, alignment: .leading)
        .overviewSurface(radius: 9)
    }
}

private struct ResourceProjectionCard: View {
    let provider: ResourceProviderRecord
    let unitCount: Int

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 8) {
                Image(systemName: providerIcon(provider.providerType))
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(providerColor)
                    .frame(width: 28, height: 28)
                    .background(providerColor.opacity(0.10), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                VStack(alignment: .leading, spacing: 2) {
                    Text(provider.displayName)
                        .font(.system(size: 10, weight: .semibold, design: .monospaced))
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Text("\(provider.providerLabel) · \(provider.stateLabel)")
                        .font(.system(size: 9, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                }
                Spacer(minLength: 4)
                Text(provider.enabled ? provider.stateLabel : "已停用")
                    .font(.system(size: 9, weight: .semibold, design: .rounded))
                    .foregroundStyle(provider.enabled ? providerColor : DesignTokens.mutedInk)
            }

            if !isWaitingForExternalSystem {
                LazyVGrid(columns: [GridItem(.flexible(), spacing: 8), GridItem(.flexible(), spacing: 8)], spacing: 7) {
                    ResourceProjectionFact(title: "总容量", value: provider.total.compactLabel)
                    ResourceProjectionFact(title: "已分配", value: provider.committed.compactLabel)
                    ResourceProjectionFact(title: "当前可用", value: availableLabel)
                    ResourceProjectionFact(title: "资源项", value: "\(unitCount)")
                }
            }

            if let boundary = provider.trustBoundary {
                Label(boundary, systemImage: "hand.raised.fill")
                    .font(.system(size: 8, weight: .medium))
                    .foregroundStyle(provider.providerType == "scheduler" ? DesignTokens.warning : DesignTokens.danger)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(11)
        .overviewSurface(radius: 10)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(provider.providerLabel) \(provider.displayName)")
        .accessibilityValue("\(provider.stateLabel)，当前可用 \(availableLabel)，已分配 \(provider.committed.compactLabel)")
    }

    private var providerColor: Color {
        if isWaitingForExternalSystem {
            return DesignTokens.warning
        }
        switch provider.state {
        case "ONLINE", "READY", "AVAILABLE": return DesignTokens.success
        case "PENDING", "QUEUED", "SUBMITTED", "DRAINING": return DesignTokens.warning
        case "ALLOCATED", "LEASED", "RUNNING": return DesignTokens.interaction
        default: return DesignTokens.danger
        }
    }

    private var availableLabel: String {
        if isWaitingForExternalSystem {
            return "等待外部系统确认"
        }
        return provider.available.compactLabel
    }

    private var isWaitingForExternalSystem: Bool {
        provider.providerType == "scheduler"
            && ["PENDING", "QUEUED", "SUBMITTED"].contains(provider.state)
    }
}

private struct ResourceProjectionFact: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(title)
                .font(.system(size: 8, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
            Text(value)
                .font(.system(size: 9, weight: .semibold, design: .rounded))
                .lineLimit(1)
                .truncationMode(.middle)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct ResourceClaimOverviewRow: View {
    let claim: ResourceClaimRecord

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "person.crop.circle.badge.clock")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(DesignTokens.interaction)
                .frame(width: 28, height: 28)
                .background(DesignTokens.interaction.opacity(0.10), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text(claim.projectID)
                    .font(.system(size: 10, weight: .semibold))
                Text("\(claim.actorID) · \(claim.taskReference.isEmpty ? (claim.purpose ?? "未命名任务") : claim.taskReference)")
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
            }
            Spacer(minLength: 8)
            LeaseFact(label: "请求", value: claim.quantities.compactLabel, width: 150)
            Text(claim.stateLabel)
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(claim.state == "REJECTED" ? DesignTokens.danger : DesignTokens.interaction)
        }
        .padding(.horizontal, 12)
        .frame(minHeight: 44)
        .overviewSurface(radius: 9)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("资源申请 \(claim.projectID)")
        .accessibilityValue("\(claim.stateLabel)，\(claim.quantities.compactLabel)")
    }
}

private struct ResourcePlanOverviewRow: View {
    let evaluation: ResourcePlanEvaluationRecord

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 10) {
                Image(systemName: "chart.line.uptrend.xyaxis")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(DesignTokens.interaction)
                    .frame(width: 28, height: 28)
                    .background(DesignTokens.interaction.opacity(0.10), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                VStack(alignment: .leading, spacing: 2) {
                    Text(evaluation.projectID)
                        .font(.system(size: 10, weight: .semibold))
                    Text("\(evaluation.actorID) · \(evaluation.taskReference.isEmpty ? evaluation.id : evaluation.taskReference)")
                        .font(.system(size: 9, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                }
                Spacer(minLength: 8)
                Text(thresholdLabel)
                    .font(.system(size: 9, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.mutedInk)
            }

            ForEach(evaluation.candidates.prefix(3)) { candidate in
                ResourceCandidateRow(candidate: candidate)
            }
        }
        .padding(12)
        .overviewSurface(radius: 9)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("资源计划 \(evaluation.projectID)")
    }

    private var thresholdLabel: String {
        let seconds = evaluation.minimumSavedSeconds ?? 120
        let ratio = Int(((evaluation.minimumSavedRatio ?? 0.10) * 100).rounded())
        return "门槛 ≥\(ratio)% 且 ≥\(seconds / 60) 分钟"
    }
}

private struct ResourceCandidateRow: View {
    let candidate: ResourcePlanCandidateRecord

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: candidate.selected ? "checkmark.circle.fill" : "xmark.circle")
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(candidate.selected ? DesignTokens.success : DesignTokens.mutedInk)
            Text(candidate.candidateKey)
                .font(.system(size: 9, weight: .semibold, design: .monospaced))
                .lineLimit(1)
                .truncationMode(.middle)
            Spacer(minLength: 8)
            Text(candidate.quantities.compactLabel)
                .font(.system(size: 9, weight: .medium, design: .rounded))
                .foregroundStyle(DesignTokens.mutedInk)
                .lineLimit(1)
            Text("\(durationLabel(candidate.predictedRuntimeSeconds)) · 省 \(durationLabel(candidate.predictedSavedSeconds))")
                .font(.system(size: 9, weight: .semibold, design: .rounded))
                .foregroundStyle(candidate.selected ? DesignTokens.success : DesignTokens.mutedInk)
                .lineLimit(1)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("候选方案 \(candidate.candidateKey)")
        .accessibilityValue("\(candidate.decisionLabel)，预测 \(durationLabel(candidate.predictedRuntimeSeconds))，节省 \(durationLabel(candidate.predictedSavedSeconds))")
    }
}

private struct ResourceActualOverviewRow: View {
    let actual: ResourceRunActualRecord

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "checklist.checked")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(DesignTokens.success)
                .frame(width: 28, height: 28)
                .background(DesignTokens.success.opacity(0.10), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text(actual.projectID)
                    .font(.system(size: 10, weight: .semibold))
                Text("\(actual.actorID) · \(actual.taskReference.isEmpty ? actual.id : actual.taskReference)")
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
            }
            Spacer(minLength: 8)
            LeaseFact(label: "资源", value: actual.quantities.compactLabel, width: 150)
            LeaseFact(label: "预测/实际", value: actualDurationLabel, width: 96)
        }
        .padding(.horizontal, 12)
        .frame(minHeight: 44)
        .overviewSurface(radius: 9)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("运行实绩 \(actual.projectID)")
        .accessibilityValue("预测和实际耗时 \(actualDurationLabel)")
    }

    private var actualDurationLabel: String {
        "\(durationLabel(actual.predictedDurationSeconds)) / \(durationLabel(actual.actualDurationSeconds))"
    }
}

private struct OverviewServerCard: View {
    let endpoint: EndpointRecord
    let gpus: [GPURecord]
    let open: () -> Void
    let selectGPU: (GPURecord) -> Void

    private var sortedGPUs: [GPURecord] { gpus.sorted { $0.index < $1.index } }
    private var allocatableCount: Int {
        guard endpoint.monitorStatus == "ONLINE" else { return 0 }
        return gpus.filter { $0.state == "AVAILABLE" }.count
    }
    private var averageUtilization: Double? {
        guard endpoint.monitorStatus == "ONLINE" else { return nil }
        let values = gpus.compactMap { $0.utilization.map { Double($0) / 100 } }
        return values.isEmpty ? nil : values.reduce(0, +) / Double(values.count)
    }
    private var averageVRAM: Double? {
        guard endpoint.monitorStatus == "ONLINE" else { return nil }
        guard !gpus.isEmpty else { return nil }
        return gpus.map(\.memoryFraction).reduce(0, +) / Double(gpus.count)
    }
    private var totalVRAMMiB: Int { gpus.reduce(0) { $0 + $1.totalVRAMMiB } }
    private var usedVRAMMiB: Int { gpus.reduce(0) { $0 + ($1.memoryUsedMiB ?? 0) } }
    private var usedMemoryMiB: Int? {
        guard let total = endpoint.memoryTotalMiB, let available = endpoint.memoryAvailableMiB else { return nil }
        return max(0, total - available)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 13) {
            HStack(spacing: 11) {
                Image(systemName: "server.rack")
                    .font(.system(size: 15, weight: .semibold))
                    .symbolRenderingMode(.hierarchical)
                    .foregroundStyle(statusColor)
                    .frame(width: 38, height: 38)
                    .background(statusColor.opacity(0.14), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                Button(action: open) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(endpoint.sshCommand)
                            .font(.system(size: 12, weight: .semibold, design: .monospaced))
                            .lineLimit(1)
                            .truncationMode(.middle)
                        Text(endpoint.monitorLabel)
                            .font(.system(size: 11, weight: .medium))
                            .foregroundStyle(statusColor)
                    }
                }
                .buttonStyle(.plain)

                Spacer(minLength: 4)
                Text(capacityLabel)
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(gpus.isEmpty ? DesignTokens.cpu : DesignTokens.gpu)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 5)
                    .background(
                        (gpus.isEmpty ? DesignTokens.cpu : DesignTokens.gpu).opacity(0.12),
                        in: Capsule()
                    )

                Menu {
                    Button("复制 SSH 命令", systemImage: "doc.on.doc") {
                        copyToPasteboard(endpoint.sshCommand)
                    }
                    Button("查看详情", systemImage: "info.circle", action: open)
                } label: {
                    Image(systemName: "ellipsis")
                        .font(.system(size: 12, weight: .semibold))
                        .frame(width: 26, height: 26)
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .help("查看或复制服务器信息")
            }

            HStack(alignment: .top, spacing: 9) {
                HomeResourceMetric(
                    icon: "cpu",
                    title: "CPU",
                    value: percent(endpoint.cpuLoadFraction),
                    detail: cpuDetail,
                    progress: endpoint.cpuLoadFraction,
                    color: DesignTokens.cpu
                )
                HomeResourceMetric(
                    icon: "memorychip",
                    title: "内存",
                    value: percent(endpoint.memoryFraction),
                    detail: memoryDetail,
                    progress: endpoint.memoryFraction,
                    color: DesignTokens.memory
                )
                HomeResourceMetric(
                    icon: "square.stack.3d.up.fill",
                    title: "GPU",
                    value: gpus.isEmpty ? "0 块" : "\(allocatableCount) / \(gpus.count)",
                    detail: gpuCapacityDetail,
                    progress: averageUtilization,
                    color: DesignTokens.gpu
                )
            }

            Divider().opacity(0.34)

            if sortedGPUs.isEmpty {
                Label("CPU 计算节点 · 当前未检测到 GPU", systemImage: "cpu")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .frame(maxWidth: .infinity, minHeight: 42, alignment: .leading)
            } else {
                LazyVGrid(columns: gpuColumns, spacing: 7) {
                    ForEach(visibleGPUs) { gpu in
                        Button { selectGPU(gpu) } label: {
                            HomeGPUChip(gpu: gpu)
                        }
                        .buttonStyle(.plain)
                    }
                    if hiddenGPUCount > 0 {
                        Label("另有 \(hiddenGPUCount) 块", systemImage: "ellipsis.circle")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(DesignTokens.mutedInk)
                            .frame(maxWidth: .infinity, minHeight: 44)
                            .background(DesignTokens.ink.opacity(0.05), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                    }
                }
            }
        }
        .padding(14)
        .overviewSurface(radius: 15)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("服务器 \(endpoint.displayName)")
        .accessibilityValue("\(endpoint.monitorLabel)，\(capacityLabel)，\(cpuDetail)，\(memoryDetail)")
    }

    private var gpuColumns: [GridItem] {
        [GridItem(.adaptive(minimum: 132, maximum: 210), spacing: 7)]
    }

    private var visibleGPUs: [GPURecord] {
        Array(sortedGPUs.prefix(6))
    }

    private var hiddenGPUCount: Int {
        max(0, sortedGPUs.count - visibleGPUs.count)
    }

    private var statusColor: Color {
        switch endpoint.monitorStatus {
        case "ONLINE": return DesignTokens.success
        case "PENDING", "STALE", "DRAINING": return DesignTokens.warning
        default: return DesignTokens.danger
        }
    }

    private var cpuDetail: String {
        guard endpoint.monitorStatus == "ONLINE" else { return "状态未在线，不显示旧遥测" }
        guard let load = endpoint.load1m, let count = endpoint.cpuCount else { return "等待主机状态" }
        return String(format: "1m %.1f · %d 核", load, count)
    }

    private var memoryDetail: String {
        guard endpoint.monitorStatus == "ONLINE" else { return "状态未在线，不显示旧遥测" }
        guard let used = usedMemoryMiB, let total = endpoint.memoryTotalMiB else { return "等待主机状态" }
        return "\(gibibytes(used)) / \(gibibytes(total)) GB"
    }

    private var gpuCapacityDetail: String {
        guard endpoint.monitorStatus == "ONLINE" else { return "状态未在线，不显示旧遥测" }
        guard !gpus.isEmpty else { return "仅 CPU · 无 GPU" }
        return "\(gibibytes(totalVRAMMiB)) GB 显存"
    }

    private var capacityLabel: String {
        guard endpoint.monitorStatus == "ONLINE" else { return "状态未在线" }
        guard !gpus.isEmpty else { return "CPU 节点" }
        return "\(allocatableCount)/\(gpus.count) 可分配"
    }
}

private struct HomeResourceMetric: View {
    let icon: String
    let title: String
    let value: String
    let detail: String
    let progress: Double?
    let color: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 7) {
                Image(systemName: icon)
                    .font(.system(size: 12, weight: .semibold))
                    .symbolRenderingMode(.hierarchical)
                    .foregroundStyle(color)
                    .frame(width: 26, height: 26)
                    .background(color.opacity(0.13), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                VStack(alignment: .leading, spacing: 1) {
                    Text(title)
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(DesignTokens.mutedInk)
                    Text(value)
                        .font(.system(size: 14, weight: .bold, design: .rounded))
                        .lineLimit(1)
                }
            }

            GeometryReader { proxy in
                ZStack(alignment: .leading) {
                    Capsule().fill(DesignTokens.ink.opacity(0.08))
                    Capsule()
                        .fill(color)
                        .frame(width: proxy.size.width * CGFloat(progress ?? 0))
                }
            }
            .frame(height: 5)

            Text(detail)
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
                .lineLimit(1)
        }
        .padding(10)
        .frame(maxWidth: .infinity, minHeight: 92, alignment: .topLeading)
        .background(DesignTokens.glassSmoke, in: RoundedRectangle(cornerRadius: 11, style: .continuous))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(title)
        .accessibilityValue("\(value)，\(detail)")
    }
}

private struct HomeGPUChip: View {
    let gpu: GPURecord

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: overviewGPUStateIcon(gpu.state))
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(DesignTokens.gpu)
                .frame(width: 28, height: 28)
                .background(DesignTokens.gpu.opacity(0.12), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text("GPU \(gpu.index)")
                    .font(.system(size: 11, weight: .semibold, design: .rounded))
                Text(overviewGPUStateLabel(gpu.state))
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
            }
            Spacer(minLength: 2)
            Text(gpu.vramLabel)
                .font(.system(size: 9, weight: .semibold, design: .rounded))
                .foregroundStyle(DesignTokens.mutedInk)
        }
        .padding(.horizontal, 9)
        .frame(maxWidth: .infinity, minHeight: 44)
        .background(DesignTokens.glassSmoke, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
        .contentShape(Rectangle())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("GPU \(gpu.index)，\(overviewGPUStateLabel(gpu.state))，显存 \(gpu.vramLabel)")
    }
}

private struct OverviewLeaseRow: View {
    let lease: LeaseRecord
    let gpuLabel: String

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "key.fill")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(DesignTokens.interaction)
                .frame(width: 28, height: 28)
                .background(DesignTokens.interaction.opacity(0.10), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text(lease.projectID)
                    .font(.system(size: 10, weight: .semibold))
                Text(lease.taskReference ?? lease.purpose ?? "未提供任务说明")
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
            }
            Spacer(minLength: 8)
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 12) {
                    LeaseFact(label: "操作者", value: lease.actorID, width: 170)
                    LeaseFact(label: "GPU ID", value: gpuLabel, width: 112)
                        .help(lease.gpuIDs.joined(separator: "\n"))
                    LeaseFact(label: "GPU", value: "\(lease.gpuIDs.count) 块", width: 54)
                    LeaseFact(label: "到期", value: overviewTimestamp(lease.expiresAt), width: 74)
                }
                HStack(spacing: 12) {
                    LeaseFact(label: "GPU", value: "\(lease.gpuIDs.count) 块", width: 54)
                    LeaseFact(label: "到期", value: overviewTimestamp(lease.expiresAt), width: 74)
                }
            }
            Text(lease.stateLabel)
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(lease.state == "CONFLICT" ? DesignTokens.danger : DesignTokens.interaction)
        }
        .padding(.horizontal, 12)
        .frame(minHeight: 44)
        .overviewSurface(radius: 9)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("资源使用 \(lease.projectID)")
        .accessibilityValue("\(lease.stateLabel)，\(lease.gpuIDs.count) 块 GPU")
    }
}

private struct ReservationOverviewRow: View {
    let reservation: ReservationRecord

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "calendar.badge.clock")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(DesignTokens.warning)
                .frame(width: 28, height: 28)
                .background(DesignTokens.warning.opacity(0.10), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text(reservation.projectID ?? "未标注项目")
                    .font(.system(size: 10, weight: .semibold))
                Text(reservation.purpose ?? "预约资源")
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
            }
            Spacer(minLength: 8)
            LeaseFact(label: "GPU", value: "\(reservation.gpuIDs.count) 块", width: 54)
            LeaseFact(label: "结束", value: overviewTimestamp(reservation.endsAt), width: 74)
            Text(reservation.stateLabel)
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(DesignTokens.warning)
        }
        .padding(.horizontal, 12)
        .frame(minHeight: 44)
        .overviewSurface(radius: 9)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("预约 \(reservation.projectID ?? reservation.id)")
        .accessibilityValue("\(reservation.stateLabel)，\(reservation.gpuIDs.count) 块 GPU")
    }
}

private struct LeaseFact: View {
    let label: String
    let value: String
    let width: CGFloat

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(label)
                .font(.system(size: 8, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
            Text(value)
                .font(.system(size: 9, weight: .semibold, design: .rounded))
                .lineLimit(1)
        }
        .frame(width: width, alignment: .leading)
    }
}

private extension View {
    func overviewSurface(radius: CGFloat) -> some View {
        let shape = RoundedRectangle(cornerRadius: radius, style: .continuous)
        return background(DesignTokens.surface, in: shape)
            .overlay(
                shape.stroke(DesignTokens.surfaceStroke, lineWidth: 0.8)
            )
    }
}

private func percent(_ value: Double?) -> String {
    guard let value else { return "—" }
    return "\(Int((value * 100).rounded()))%"
}

private func providerIcon(_ providerType: String) -> String {
    switch providerType {
    case "direct-gpu": return "square.grid.3x3.fill"
    case "host-capacity": return "cpu"
    case "scheduler": return "point.3.connected.trianglepath.dotted"
    default: return "shippingbox"
    }
}

private func durationLabel(_ seconds: Int?) -> String {
    guard let seconds else { return "—" }
    if seconds < 60 { return "\(seconds)s" }
    if seconds < 3600 { return "\(seconds / 60)m" }
    let hours = seconds / 3600
    let minutes = (seconds % 3600) / 60
    return minutes == 0 ? "\(hours)h" : "\(hours)h \(minutes)m"
}

private func gibibytes(_ mebibytes: Int) -> Int {
    Int((Double(mebibytes) / 1024).rounded())
}

private func overviewGPUStateLabel(_ state: String) -> String {
    switch state {
    case "AVAILABLE": return "可用"
    case "HELD", "LEASED_IDLE": return "已分配"
    case "RUNNING_MANAGED": return "运行中"
    case "BUSY_UNMANAGED": return "非托管占用"
    case "ORPHANED_BUSY": return "释放后仍占用"
    case "RESERVED": return "已预约"
    case "UNKNOWN_RECOVERING": return "等待状态"
    case "UNKNOWN_STALE": return "状态过期"
    case "UNHEALTHY": return "状态异常"
    case "CONFLICT": return "需要处理"
    case "DISABLED": return "已停用"
    case "MAINTENANCE": return "维护中"
    case "DRAINING": return "排空中"
    case "RETIRED": return "已退役"
    default: return "需处理"
    }
}

private func overviewGPUStateIcon(_ state: String) -> String {
    switch state {
    case "AVAILABLE": return "checkmark.circle.fill"
    case "HELD", "LEASED_IDLE": return "key.fill"
    case "RUNNING_MANAGED": return "play.circle.fill"
    case "BUSY_UNMANAGED", "ORPHANED_BUSY": return "person.crop.circle.badge.exclamationmark"
    case "RESERVED": return "calendar.badge.clock"
    case "MAINTENANCE": return "wrench.and.screwdriver.fill"
    case "UNKNOWN_RECOVERING", "UNKNOWN_STALE": return "clock.badge.exclamationmark"
    case "UNHEALTHY": return "cross.case.fill"
    case "CONFLICT": return "exclamationmark.triangle.fill"
    case "DISABLED": return "pause.circle.fill"
    case "DRAINING": return "arrow.down.forward.and.arrow.up.backward"
    case "RETIRED": return "archivebox.fill"
    default: return "questionmark.diamond.fill"
    }
}

private func endpointIcon(_ state: String) -> String {
    switch state {
    case "ONLINE": return "server.rack"
    case "PENDING": return "hourglass"
    case "STALE": return "clock.badge.exclamationmark"
    case "ERROR": return "exclamationmark.triangle.fill"
    case "DISABLED": return "pause.circle.fill"
    case "DRAINING": return "arrow.down.forward.and.arrow.up.backward"
    case "RETIRED": return "archivebox.fill"
    default: return "questionmark.diamond.fill"
    }
}

private func overviewTimestamp(_ value: String?) -> String {
    guard let value else { return "未知" }
    let parser = ISO8601DateFormatter()
    parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let date = parser.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    guard let date else { return "未知" }
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "zh_CN")
    formatter.dateFormat = "M/d HH:mm"
    return formatter.string(from: date)
}

private func copyToPasteboard(_ value: String) {
    NSPasteboard.general.clearContents()
    NSPasteboard.general.setString(value, forType: .string)
}
