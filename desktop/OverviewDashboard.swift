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
        snapshot.endpoints.filter { ["ERROR", "STALE", "DRAINING", "RETIRED", "DISABLED"].contains($0.monitorStatus) }
    }

    private var attentionGPUs: [GPURecord] {
        snapshot.gpus.filter { attentionStates.contains($0.state) }
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
        Set(snapshot.endpoints.filter { $0.monitorStatus == "ONLINE" }.map(\.id))
    }

    private var freshGPUCount: Int {
        snapshot.gpus.filter { freshEndpointIDs.contains($0.endpointID) }.count
    }

    private var allocatableGPUCount: Int {
        snapshot.gpus.filter {
            freshEndpointIDs.contains($0.endpointID) && $0.state == "AVAILABLE"
        }.count
    }

    private var idleLeaseGPUCount: Int {
        snapshot.gpus.filter { ["HELD", "LEASED_IDLE"].contains($0.state) }.count
    }

    private var queuedRequestCount: Int {
        snapshot.requests.filter { $0.state == "QUEUED" }.count
    }

    private var monitoringProviders: [ResourceProviderRecord] {
        snapshot.monitoringProviders
    }

    private var hostCapacityProviders: [ResourceProviderRecord] {
        monitoringProviders.filter { $0.providerType == "host-capacity" }
    }

    private var schedulerProviders: [ResourceProviderRecord] {
        monitoringProviders.filter { $0.providerType == "scheduler" }
    }

    private var availableHostCPU: Int {
        Int(hostCapacityProviders.reduce(0) { $0 + $1.available.cpuCores }.rounded())
    }

    private var availableHostMemoryGiB: Int {
        hostCapacityProviders.reduce(0) { $0 + $1.available.memoryMiB } / 1024
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                overviewHeader
                summaryGrid
                coordinationSignals
                resourceProjectionSection
                planningAuditSection
                attentionSection
                serverPool
                leaseSection

                Label(snapshot.admissionBoundary, systemImage: "hand.raised.fill")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.horizontal, 24)
            .padding(.bottom, 16)
            .frame(maxWidth: .infinity, alignment: .topLeading)
        }
        .accessibilityLabel("资源总览")
    }

    private var overviewHeader: some View {
        VStack(alignment: .leading, spacing: 7) {
            overviewHeading
            overviewCapacityBadge
        }
        .font(.system(size: 11, weight: .semibold, design: .rounded))
        .foregroundStyle(DesignTokens.mutedInk)
    }

    private var overviewHeading: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("实时概览")
                .font(.system(size: 16, weight: .semibold))
            Text("查看已登记容量、最新在线状态与协调中的租约")
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
                .lineLimit(1)
        }
    }

    private var overviewCapacityBadge: some View {
        Text("\(snapshot.summary.totalServers) 台已登记 · \(snapshot.summary.onlineServers) 台在线")
            .lineLimit(1)
            .padding(.horizontal, 9)
            .padding(.vertical, 5)
            .background(.regularMaterial, in: Capsule())
    }

    private var summaryGrid: some View {
        LazyVGrid(columns: columns, spacing: 10) {
            OverviewSummaryCard(title: "已登记 GPU", value: snapshot.summary.totalGPUs, icon: "square.grid.3x3.fill")
            OverviewSummaryCard(title: "在线 / 最新 GPU", value: freshGPUCount, icon: "waveform.path.ecg")
            OverviewSummaryCard(title: "当前可分配", value: allocatableGPUCount, icon: "checkmark.circle.fill")
            OverviewSummaryCard(title: "可用 CPU", value: availableHostCPU, icon: "cpu")
            OverviewSummaryCard(title: "可用内存 GB", value: availableHostMemoryGiB, icon: "memorychip")
            OverviewSummaryCard(title: "调度目标", value: schedulerProviders.count, icon: "point.3.connected.trianglepath.dotted")
        }
    }

    private var coordinationSignals: some View {
        LazyVGrid(columns: columns, spacing: 8) {
            CoordinationSignal(
                title: "空闲租约",
                value: "\(idleLeaseGPUCount) GPU",
                detail: "已归属、未运行",
                icon: "pause.circle",
                color: idleLeaseGPUCount > 0 ? DesignTokens.warning : DesignTokens.mutedInk
            )
            CoordinationSignal(
                title: "排队请求",
                value: "\(queuedRequestCount + snapshot.resourceClaims.filter { $0.state == "QUEUED" }.count)",
                detail: "GPU 与通用资源请求",
                icon: "hourglass",
                color: (queuedRequestCount > 0 || snapshot.resourceClaims.contains { $0.state == "QUEUED" }) ? DesignTokens.warning : DesignTokens.mutedInk
            )
            CoordinationSignal(
                title: "边际收益决策",
                value: "\(snapshot.resourcePlanEvaluations.count)",
                detail: "扩容需 ≥10% 且 ≥2 分钟",
                icon: "chart.line.uptrend.xyaxis",
                color: snapshot.resourcePlanEvaluations.isEmpty ? DesignTokens.mutedInk : DesignTokens.interaction
            )
            CoordinationSignal(
                title: "运行实绩",
                value: "\(snapshot.resourceRunActuals.count)",
                detail: "预测 vs 实际",
                icon: "checklist.checked",
                color: snapshot.resourceRunActuals.isEmpty ? DesignTokens.mutedInk : DesignTokens.success
            )
            CoordinationSignal(
                title: "状态例外",
                value: "\(attentionEndpoints.count + attentionGPUs.count)",
                detail: "需要人工确认",
                icon: "exclamationmark.triangle.fill",
                color: (attentionEndpoints.isEmpty && attentionGPUs.isEmpty) ? DesignTokens.mutedInk : DesignTokens.danger
            )
        }
    }

    private var resourceProjectionSection: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text("资源调度投影")
                    .font(.system(size: 13, weight: .semibold))
                Text("\(monitoringProviders.count)")
                    .font(.system(size: 10, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.mutedInk)
                Spacer()
                Text("GPU、主机容量、外部调度分开显示")
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
        .accessibilityLabel("资源调度投影")
    }

    private var planningAuditSection: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text("Agent 调度决策")
                    .font(.system(size: 13, weight: .semibold))
                Text("\(snapshot.resourcePlanEvaluations.count)")
                    .font(.system(size: 10, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.mutedInk)
                Spacer()
                Text("候选方案、选择/拒绝与预测-实际")
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }

            if snapshot.resourcePlanEvaluations.isEmpty && snapshot.resourceRunActuals.isEmpty && snapshot.resourceClaims.isEmpty {
                Label("等待 Agent 提交通用资源计划；旧快照保持兼容", systemImage: "clock")
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
        .accessibilityLabel("Agent 调度决策")
    }

    private var attentionSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("状态例外")
                    .font(.system(size: 12, weight: .semibold))
                Spacer()
                if !attentionEndpoints.isEmpty || !attentionGPUs.isEmpty {
                    Text("\(attentionEndpoints.count + attentionGPUs.count) 项")
                        .font(.system(size: 10, weight: .semibold, design: .rounded))
                        .foregroundStyle(DesignTokens.mutedInk)
                }
            }

            if attentionEndpoints.isEmpty && attentionGPUs.isEmpty {
                Label("所有端点均未报告过期或连接异常", systemImage: "checkmark.circle.fill")
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
                        .accessibilityLabel("端点状态例外")
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
                        .accessibilityLabel("GPU 状态例外")
                        .accessibilityValue("GPU \(gpu.index)，\(overviewGPUStateLabel(gpu.state))")
                    }
                }
                if hiddenAttentionCount > 0 {
                    Button(action: openServerPool) {
                        HStack(spacing: 8) {
                            Label("另有 \(hiddenAttentionCount) 项状态例外", systemImage: "ellipsis.circle.fill")
                                .font(.system(size: 10, weight: .semibold))
                            Spacer(minLength: 8)
                            Text("在服务器池查看")
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
                    .help("打开服务器池查看全部状态例外")
                    .accessibilityLabel("另有 \(hiddenAttentionCount) 项状态例外，在服务器池查看全部")
                }
            }
        }
    }

    private var serverPool: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text("服务器池")
                    .font(.system(size: 13, weight: .semibold))
                Text("\(snapshot.endpoints.count)")
                    .font(.system(size: 10, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.mutedInk)
                Spacer()
                Text(snapshotAgeLabel)
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }

            LazyVGrid(
                columns: [
                    GridItem(.flexible(minimum: 0), spacing: 10),
                    GridItem(.flexible(minimum: 0), spacing: 10)
                ],
                alignment: .leading,
                spacing: 10
            ) {
                ForEach(snapshot.endpoints) { endpoint in
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

    private var leaseSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("当前租约")
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
                    Text("暂无活动租约")
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
        guard let age = snapshot.dataAgeSeconds else { return "等待快照" }
        if age < 5 { return "快照刚更新" }
        return "快照 \(Int(age.rounded())) 秒前"
    }
}

private struct OverviewSummaryCard: View {
    let title: String
    let value: Int
    let icon: String
    var isAttention = false

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: icon)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(isAttention ? DesignTokens.danger : DesignTokens.interaction)
                .frame(width: 30, height: 30)
                .background(
                    (isAttention ? DesignTokens.danger : DesignTokens.interaction).opacity(0.11),
                    in: RoundedRectangle(cornerRadius: 8, style: .continuous)
                )
            VStack(alignment: .leading, spacing: 1) {
                Text("\(value)")
                    .font(.system(size: 19, weight: .semibold, design: .rounded))
                Text(title)
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .frame(minHeight: 56)
        .overviewSurface(radius: 11)
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
                Text(provider.enabled ? "可观察" : "停用")
                    .font(.system(size: 9, weight: .semibold, design: .rounded))
                    .foregroundStyle(provider.enabled ? providerColor : DesignTokens.mutedInk)
            }

            LazyVGrid(columns: [GridItem(.flexible(), spacing: 8), GridItem(.flexible(), spacing: 8)], spacing: 7) {
                ResourceProjectionFact(title: "总容量", value: provider.total.compactLabel)
                ResourceProjectionFact(title: "已承诺", value: provider.committed.compactLabel)
                ResourceProjectionFact(title: "当前可认领", value: availableLabel)
                ResourceProjectionFact(title: "单元", value: "\(unitCount)")
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
        .accessibilityValue("\(provider.stateLabel)，可认领 \(availableLabel)，已承诺 \(provider.committed.compactLabel)")
    }

    private var providerColor: Color {
        if provider.providerType == "scheduler", ["PENDING", "QUEUED", "SUBMITTED"].contains(provider.state) {
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
        if provider.providerType == "scheduler", ["PENDING", "QUEUED", "SUBMITTED"].contains(provider.state) {
            return "等待调度确认"
        }
        return provider.available.compactLabel
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
        .accessibilityLabel("资源认领 \(claim.projectID)")
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
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Circle()
                    .fill(statusColor)
                    .frame(width: 7, height: 7)
                Button(action: open) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(endpoint.sshCommand)
                            .font(.system(size: 10, weight: .semibold, design: .monospaced))
                            .lineLimit(1)
                            .truncationMode(.middle)
                        Text(endpoint.monitorLabel)
                            .font(.system(size: 9, weight: .medium))
                            .foregroundStyle(DesignTokens.mutedInk)
                    }
                }
                .buttonStyle(.plain)

                Spacer(minLength: 4)
                Text(capacityLabel)
                    .font(.system(size: 9, weight: .semibold, design: .rounded))
                    .foregroundStyle(endpoint.monitorStatus == "ONLINE" ? DesignTokens.mutedInk : DesignTokens.warning)

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

            LazyVGrid(columns: metricColumns, spacing: 8) {
                OverviewMetric(
                    title: "CPU 负载",
                    value: endpoint.cpuLoadFraction,
                    detail: cpuDetail
                )
                OverviewMetric(
                    title: "内存",
                    value: endpoint.memoryFraction,
                    detail: memoryDetail
                )
                OverviewMetric(
                    title: "GPU 利用率",
                    value: averageUtilization,
                    detail: utilizationDetail
                )
                OverviewMetric(
                    title: "显存",
                    value: averageVRAM,
                    detail: vramDetail
                )
            }

            Divider().opacity(0.34)

            if sortedGPUs.isEmpty {
                Text("等待 GPU 状态")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .frame(maxWidth: .infinity, minHeight: 70)
            } else {
                LazyVGrid(columns: gpuColumns, spacing: 7) {
                    ForEach(visibleGPUs) { gpu in
                        Button { selectGPU(gpu) } label: {
                            OverviewGPUTile(gpu: gpu)
                        }
                        .buttonStyle(.plain)
                    }
                    if hiddenGPUCount > 0 {
                        Text("+\(hiddenGPUCount)")
                            .font(.system(size: 10, weight: .semibold, design: .rounded))
                            .foregroundStyle(DesignTokens.mutedInk)
                            .frame(maxWidth: .infinity, minHeight: 62)
                            .background(DesignTokens.ink.opacity(0.05), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                    }
                }
            }
        }
        .padding(12)
        .overviewSurface(radius: 12)
    }

    private var metricColumns: [GridItem] {
        [GridItem(.flexible(), spacing: 9), GridItem(.flexible(), spacing: 9)]
    }

    private var gpuColumns: [GridItem] {
        Array(repeating: GridItem(.flexible(), spacing: 6), count: 4)
    }

    private var visibleGPUs: [GPURecord] {
        Array(sortedGPUs.prefix(12))
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

    private var utilizationDetail: String {
        guard endpoint.monitorStatus == "ONLINE" else { return "状态未在线，不显示旧遥测" }
        guard !gpus.isEmpty else { return "等待 GPU 状态" }
        return "\(gpus.count) 块 GPU 平均"
    }

    private var vramDetail: String {
        guard endpoint.monitorStatus == "ONLINE" else { return "状态未在线，不显示旧遥测" }
        guard totalVRAMMiB > 0 else { return "等待 GPU 状态" }
        return "\(gibibytes(usedVRAMMiB)) / \(gibibytes(totalVRAMMiB)) GB"
    }

    private var capacityLabel: String {
        guard endpoint.monitorStatus == "ONLINE" else { return "状态未在线" }
        guard !gpus.isEmpty else { return "等待 GPU 状态" }
        return "\(allocatableCount)/\(gpus.count) 可分配"
    }
}

private struct OverviewMetric: View {
    let title: String
    let value: Double?
    let detail: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 4) {
                Text(title)
                Spacer(minLength: 4)
                Text(percent(value))
                    .fontDesign(.rounded)
            }
            .font(.system(size: 9, weight: .semibold))

            GeometryReader { proxy in
                ZStack(alignment: .leading) {
                    Capsule().fill(DesignTokens.ink.opacity(0.09))
                    Capsule()
                        .fill(DesignTokens.interaction)
                        .frame(width: proxy.size.width * CGFloat(value ?? 0))
                }
            }
            .frame(height: 4)

            Text(detail)
                .font(.system(size: 8, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
                .lineLimit(1)
        }
    }
}

private struct OverviewGPUTile: View {
    let gpu: GPURecord

    var body: some View {
        VStack(spacing: 4) {
            ZStack {
                Circle()
                    .stroke(DesignTokens.ink.opacity(0.10), lineWidth: 4)
                Circle()
                    .trim(from: 0, to: gpu.memoryFraction)
                    .stroke(DesignTokens.interaction, style: StrokeStyle(lineWidth: 4, lineCap: .round))
                    .rotationEffect(.degrees(-90))
                Text("\(Int((gpu.memoryFraction * 100).rounded()))%")
                    .font(.system(size: 8, weight: .semibold, design: .rounded))
            }
            .frame(width: 36, height: 36)

            Text("GPU \(gpu.index)")
                .font(.system(size: 8, weight: .semibold, design: .rounded))
            Text(gpu.memoryLabel)
                .font(.system(size: 7, weight: .medium, design: .rounded))
                .foregroundStyle(DesignTokens.mutedInk)
                .lineLimit(1)
                .minimumScaleFactor(0.72)
        }
        .frame(maxWidth: .infinity, minHeight: 62)
        .contentShape(Rectangle())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("GPU \(gpu.index)，显存占用 \(Int((gpu.memoryFraction * 100).rounded()))%，\(overviewGPUStateLabel(gpu.state))")
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
        .accessibilityLabel("租约 \(lease.projectID)")
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
        return background(.regularMaterial, in: shape)
            .background(DesignTokens.surface.opacity(0.32), in: shape)
            .overlay(
                shape.stroke(DesignTokens.surfaceStroke, lineWidth: 0.8)
            )
            .shadow(color: DesignTokens.surfaceShadow, radius: 8, x: 0, y: 2)
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
    case "HELD", "LEASED_IDLE": return "已认领"
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
