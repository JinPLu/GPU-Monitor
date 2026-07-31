import AppKit
import SwiftUI

struct FleetOverview: View {
    let snapshot: BrokerSnapshot
    let isRefreshing: Bool
    let refresh: () -> Void
    let openEndpoint: (EndpointRecord) -> Void
    let selectGPU: (GPURecord) -> Void

    private let attentionStates = Set([
        "BUSY_UNMANAGED", "UNKNOWN_RECOVERING", "UNKNOWN_STALE",
        "UNHEALTHY", "CONFLICT", "ORPHANED_BUSY"
    ])

    private var attentionEndpoints: [EndpointRecord] {
        snapshot.endpoints.filter { ["ERROR", "STALE"].contains($0.monitorStatus) }
    }

    private var attentionGPUs: [GPURecord] {
        snapshot.gpus.filter { attentionStates.contains($0.state) }
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

    var body: some View {
        GeometryReader { proxy in
            let compact = proxy.size.width < 1000
            let condensed = proxy.size.width < 1000
            let serverColumns = overviewColumns(for: proxy.size.width)

            VStack(alignment: .leading, spacing: 10) {
                overviewHeader
                summaryGrid(compact: compact)
                coordinationSignals
                attentionSection
                actionBar
                serverPool(columns: serverColumns)
                leaseSection(condensed: condensed)

                Label(snapshot.admissionBoundary, systemImage: "hand.raised.fill")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
            }
            .padding(.leading, compact ? 16 : 24)
            .padding(.trailing, compact ? 40 : 24)
            .padding(.bottom, 16)
            .frame(width: proxy.size.width, height: proxy.size.height, alignment: .top)
        }
    }

    private var overviewHeader: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text("实时概览")
                    .font(.system(size: 16, weight: .semibold))
                Text("查看已登记容量、最新在线状态与协调中的租约")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
            }
            Spacer(minLength: 12)
            Text("\(snapshot.summary.totalServers) 台已登记 · \(snapshot.summary.onlineServers) 台在线")
                .lineLimit(1)
                .padding(.horizontal, 9)
                .padding(.vertical, 5)
                .background(.ultraThinMaterial, in: Capsule())
        }
        .font(.system(size: 11, weight: .semibold, design: .rounded))
        .foregroundStyle(DesignTokens.mutedInk)
    }

    @ViewBuilder
    private func summaryGrid(compact: Bool) -> some View {
        let columns = Array(repeating: GridItem(.flexible(), spacing: 10), count: compact ? 1 : 3)
        LazyVGrid(columns: columns, spacing: 10) {
            OverviewSummaryCard(title: "已登记 GPU", value: snapshot.summary.totalGPUs, icon: "square.grid.3x3.fill")
            OverviewSummaryCard(title: "在线 / 最新 GPU", value: freshGPUCount, icon: "waveform.path.ecg")
            OverviewSummaryCard(title: "当前可分配", value: allocatableGPUCount, icon: "checkmark.circle.fill")
        }
    }

    private var coordinationSignals: some View {
        HStack(spacing: 8) {
            CoordinationSignal(
                title: "空闲租约",
                value: "\(idleLeaseGPUCount) GPU",
                detail: "已归属、未运行",
                icon: "pause.circle",
                color: idleLeaseGPUCount > 0 ? DesignTokens.warning : DesignTokens.mutedInk
            )
            CoordinationSignal(
                title: "排队请求",
                value: "\(queuedRequestCount)",
                detail: "等待分配",
                icon: "hourglass",
                color: queuedRequestCount > 0 ? DesignTokens.warning : DesignTokens.mutedInk
            )
            CoordinationSignal(
                title: "状态例外",
                value: "\(attentionEndpoints.count)",
                detail: "过期或连接异常",
                icon: "exclamationmark.triangle.fill",
                color: attentionEndpoints.isEmpty ? DesignTokens.mutedInk : DesignTokens.danger
            )
        }
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
                ScrollView(.horizontal, showsIndicators: true) {
                    HStack(spacing: 8) {
                        ForEach(attentionEndpoints) { endpoint in
                            Button { openEndpoint(endpoint) } label: {
                                AttentionCard(
                                    title: endpoint.sshCommand,
                                    detail: endpoint.monitorDetail ?? endpoint.monitorLabel,
                                    icon: "server.rack"
                                )
                            }
                            .buttonStyle(.plain)
                        }
                        ForEach(attentionGPUs) { gpu in
                            Button { selectGPU(gpu) } label: {
                                AttentionCard(
                                    title: "GPU \(gpu.index) · \(gpu.name)",
                                    detail: overviewGPUStateLabel(gpu.state),
                                    icon: "square.3.layers.3d"
                                )
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(.bottom, 3)
                }
                .frame(height: 58)
            }
        }
    }

    private var actionBar: some View {
        HStack(spacing: 8) {
            Button(action: refresh) {
                HStack(spacing: 5) {
                    Image(systemName: "arrow.clockwise")
                        .rotationEffect(.degrees(isRefreshing ? 360 : 0))
                    Text("刷新")
                }
                .animation(
                    isRefreshing ? .linear(duration: 0.7).repeatForever(autoreverses: false) : .easeOut(duration: 0.15),
                    value: isRefreshing
                )
            }
            .buttonStyle(SecondaryActionButtonStyle())
            .disabled(isRefreshing)
            .help("刷新资源状态")

            Spacer()
            Label(snapshotAgeLabel, systemImage: "clock.arrow.circlepath")
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
        }
    }

    private func serverPool(columns: [GridItem]) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text("服务器池")
                    .font(.system(size: 13, weight: .semibold))
                Text("\(snapshot.endpoints.count)")
                    .font(.system(size: 10, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.mutedInk)
                Spacer()
                Text("上下滚动查看更多服务器")
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }

            ScrollView(.vertical, showsIndicators: true) {
                LazyVGrid(columns: columns, alignment: .leading, spacing: 10) {
                    ForEach(snapshot.endpoints) { endpoint in
                        OverviewServerCard(
                            endpoint: endpoint,
                            gpus: snapshot.gpus(for: endpoint),
                            open: { openEndpoint(endpoint) },
                            selectGPU: selectGPU
                        )
                    }
                }
                .padding(.trailing, 3)
                .padding(.bottom, 4)
            }
            .frame(maxHeight: .infinity)
        }
        .frame(maxHeight: .infinity)
    }

    private func leaseSection(condensed: Bool) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("当前租约")
                    .font(.system(size: 13, weight: .semibold))
                Text("\(snapshot.leases.count)")
                    .font(.system(size: 10, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.mutedInk)
                Spacer()
                Text("上下滚动")
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }

            ScrollView(.vertical, showsIndicators: true) {
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
                                gpuLabel: gpuLabel(for: lease),
                                condensed: condensed
                            )
                        }
                    }
                }
                .padding(.trailing, 3)
            }
            .frame(height: 52)
        }
    }

    private func overviewColumns(for width: CGFloat) -> [GridItem] {
        let count = width >= 1060 ? 3 : (width >= 1000 ? 2 : 1)
        return Array(repeating: GridItem(.flexible(), spacing: 10), count: count)
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
        .frame(width: 292, height: 50)
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
                    ForEach(sortedGPUs) { gpu in
                        Button { selectGPU(gpu) } label: {
                            OverviewGPUTile(gpu: gpu)
                        }
                        .buttonStyle(.plain)
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

    private var statusColor: Color {
        switch endpoint.monitorStatus {
        case "ONLINE": return DesignTokens.success
        case "PENDING", "STALE": return DesignTokens.warning
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
    let condensed: Bool

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
            if !condensed {
                LeaseFact(label: "操作者", value: lease.actorID, width: 230)
                LeaseFact(label: "GPU ID", value: gpuLabel, width: 112)
                    .help(lease.gpuIDs.joined(separator: "\n"))
            }
            LeaseFact(label: "GPU", value: "\(lease.gpuIDs.count) 块", width: 54)
            LeaseFact(label: "到期", value: overviewTimestamp(lease.expiresAt), width: 74)
            Text(lease.stateLabel)
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(lease.state == "CONFLICT" ? DesignTokens.danger : DesignTokens.interaction)
        }
        .padding(.horizontal, 12)
        .frame(minHeight: 44)
        .overviewSurface(radius: 9)
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
        background(DesignTokens.surface.opacity(0.78), in: RoundedRectangle(cornerRadius: radius, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .stroke(DesignTokens.surfaceStroke, lineWidth: 1)
            )
    }
}

private func percent(_ value: Double?) -> String {
    guard let value else { return "—" }
    return "\(Int((value * 100).rounded()))%"
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
    default: return "需处理"
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
