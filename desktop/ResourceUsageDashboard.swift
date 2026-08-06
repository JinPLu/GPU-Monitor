import SwiftUI

private enum ResourceUsageScope: String, CaseIterable, Identifiable {
    case project
    case agent
    case task

    var id: String { rawValue }

    var label: String {
        switch self {
        case .project: return "项目"
        case .agent: return "Agent"
        case .task: return "任务"
        }
    }

    var icon: String {
        switch self {
        case .project: return "folder.fill"
        case .agent: return "person.crop.circle.fill"
        case .task: return "checklist.checked"
        }
    }
}

private struct ResourceUsageBucket {
    let key: String
    let title: String
    var projectIDs: Set<String> = []
    var actorIDs: Set<String> = []
    var taskReferences: Set<String> = []
    var claims: [ResourceClaimRecord] = []
    var leases: [LeaseRecord] = []
    var requests: [AllocationRequestRecord] = []
    var actuals: [ResourceRunActualRecord] = []
}

private struct ResourceUsageGroup: Identifiable {
    let id: String
    let scope: ResourceUsageScope
    let title: String
    let projectIDs: [String]
    let actorIDs: [String]
    let taskReferences: [String]
    let claims: [ResourceClaimRecord]
    let leases: [LeaseRecord]
    let requests: [AllocationRequestRecord]
    let actuals: [ResourceRunActualRecord]

    private let assignedClaimStates = Set(["HELD", "ACTIVE"])
    private let pendingClaimStates = Set(["BLOCKED", "QUEUED", "PENDING_APPROVAL", "REQUESTED"])

    var assignedClaims: [ResourceClaimRecord] {
        claims.filter {
            assignedClaimStates.contains($0.state) && $0.runtimeState != "RUNNING"
        }
    }

    var runningClaims: [ResourceClaimRecord] {
        claims.filter { $0.runtimeState == "RUNNING" || $0.state == "RUNNING" }
    }

    var pendingClaims: [ResourceClaimRecord] {
        claims.filter { pendingClaimStates.contains($0.state) }
    }

    var visibleLegacyRequests: [AllocationRequestRecord] {
        requests
    }

    var assignedLegacyLeases: [LeaseRecord] {
        leases.filter { $0.runtimeState != "RUNNING" }
    }

    var runningLegacyLeases: [LeaseRecord] {
        leases.filter { $0.runtimeState == "RUNNING" }
    }

    var assignedQuantities: ResourceQuantityRecord {
        combinedQuantities(
            assignedClaims.map(\.quantities) + [
                ResourceQuantityRecord(
                    gpuCount: assignedLegacyLeases.reduce(0) { $0 + $1.gpuIDs.count }
                )
            ]
        )
    }

    var runningQuantities: ResourceQuantityRecord {
        combinedQuantities(
            runningClaims.map(\.quantities) + [
                ResourceQuantityRecord(
                    gpuCount: runningLegacyLeases.reduce(0) { $0 + $1.gpuIDs.count }
                )
            ]
        )
    }

    var requestedQuantities: ResourceQuantityRecord {
        combinedQuantities(
            pendingClaims.map(\.quantities) + [
                ResourceQuantityRecord(
                    gpuCount: visibleLegacyRequests.reduce(0) { $0 + $1.gpuCount }
                )
            ]
        )
    }

    var subtitle: String {
        switch scope {
        case .project:
            return "\(actorIDs.count) 个 Agent · \(taskReferences.count) 个任务"
        case .agent:
            return "\(projectIDs.count) 个项目 · \(taskReferences.count) 个任务"
        case .task:
            let project = projectIDs.first ?? "未标注项目"
            return "\(project) · \(actorIDs.count) 个 Agent"
        }
    }

    var activityCount: Int {
        claims.count + leases.count + visibleLegacyRequests.count + actuals.count
    }

    var hasPendingWork: Bool {
        !pendingClaims.isEmpty || !visibleLegacyRequests.isEmpty
    }
}

private struct ResourceUsageProjection {
    let projectCount: Int
    let agentCount: Int
    let taskCount: Int
    let assignedQuantities: ResourceQuantityRecord
    let runningQuantities: ResourceQuantityRecord
    let requestedQuantities: ResourceQuantityRecord
    let groupsByScope: [ResourceUsageScope: [ResourceUsageGroup]]

    static let empty = ResourceUsageProjection(
        projectCount: 0,
        agentCount: 0,
        taskCount: 0,
        assignedQuantities: ResourceQuantityRecord(),
        runningQuantities: ResourceQuantityRecord(),
        requestedQuantities: ResourceQuantityRecord(),
        groupsByScope: [:]
    )

    init(snapshot: BrokerSnapshot, scope: ResourceUsageScope) {
        let identities = resourceUsageIdentities(snapshot: snapshot)
        projectCount = Set(identities.map(\.projectID)).count
        agentCount = Set(identities.map(\.actorID)).count
        taskCount = Set(identities.map { "\($0.projectID)\u{1F}\($0.taskReference)" }).count

        let linkedLeaseIDs = Set(snapshot.resourceClaims.flatMap(\.nativeLeaseIDs))
        let linkedRequestIDs = Set(snapshot.resourceClaims.flatMap(\.nativeRequestIDs))
        let legacyLeases = snapshot.leases.filter { !linkedLeaseIDs.contains($0.id) }
        let legacyRequests = snapshot.requests.filter { !linkedRequestIDs.contains($0.id) }
        let assignedClaims = snapshot.resourceClaims.filter {
            ["HELD", "ACTIVE"].contains($0.state) && $0.runtimeState != "RUNNING"
        }
        let runningClaims = snapshot.resourceClaims.filter {
            $0.runtimeState == "RUNNING" || $0.state == "RUNNING"
        }
        let pendingClaims = snapshot.resourceClaims.filter {
            ["BLOCKED", "QUEUED", "PENDING_APPROVAL", "REQUESTED"].contains($0.state)
        }
        assignedQuantities = combinedQuantities(
            assignedClaims.map(\.quantities) + [
                ResourceQuantityRecord(
                    gpuCount: legacyLeases
                        .filter { $0.runtimeState != "RUNNING" }
                        .reduce(0) { $0 + $1.gpuIDs.count }
                )
            ]
        )
        runningQuantities = combinedQuantities(
            runningClaims.map(\.quantities) + [
                ResourceQuantityRecord(
                    gpuCount: legacyLeases
                        .filter { $0.runtimeState == "RUNNING" }
                        .reduce(0) { $0 + $1.gpuIDs.count }
                )
            ]
        )
        requestedQuantities = combinedQuantities(
            pendingClaims.map(\.quantities) + [
                ResourceQuantityRecord(
                    gpuCount: legacyRequests.reduce(0) { $0 + $1.gpuCount }
                )
            ]
        )

        groupsByScope = [scope: makeResourceUsageGroups(snapshot: snapshot, scope: scope)]
    }

    private init(
        projectCount: Int,
        agentCount: Int,
        taskCount: Int,
        assignedQuantities: ResourceQuantityRecord,
        runningQuantities: ResourceQuantityRecord,
        requestedQuantities: ResourceQuantityRecord,
        groupsByScope: [ResourceUsageScope: [ResourceUsageGroup]]
    ) {
        self.projectCount = projectCount
        self.agentCount = agentCount
        self.taskCount = taskCount
        self.assignedQuantities = assignedQuantities
        self.runningQuantities = runningQuantities
        self.requestedQuantities = requestedQuantities
        self.groupsByScope = groupsByScope
    }

    func groups(for scope: ResourceUsageScope) -> [ResourceUsageGroup] {
        groupsByScope[scope] ?? []
    }
}

struct ResourceUsageDashboard: View {
    @ObservedObject var store: BrokerStore
    @State private var scope: ResourceUsageScope = .project
    @State private var selectedGroupID = ""
    @State private var inlineMessage: String?

    init(store: BrokerStore) {
        self.store = store
#if DEBUG
        let requestedScope = ProcessInfo.processInfo.environment["GPU_BROKER_DESKTOP_USAGE_SCOPE"]
        let initialScope: ResourceUsageScope
        switch requestedScope {
        case "agent": initialScope = .agent
        case "task": initialScope = .task
        default: initialScope = .project
        }
        _scope = State(initialValue: initialScope)
#endif
    }

    private var snapshot: BrokerSnapshot { store.snapshot }

    private var projection: ResourceUsageProjection {
        ResourceUsageProjection(snapshot: snapshot, scope: scope)
    }

    private var groups: [ResourceUsageGroup] {
        projection.groups(for: scope)
    }

    private var selectedGroup: ResourceUsageGroup? {
        groups.first { $0.id == selectedGroupID } ?? groups.first
    }

    private var projectCount: Int {
        projection.projectCount
    }

    private var agentCount: Int {
        projection.agentCount
    }

    private var taskCount: Int {
        projection.taskCount
    }

    private var assignedQuantities: ResourceQuantityRecord {
        projection.assignedQuantities
    }

    private var runningQuantities: ResourceQuantityRecord {
        projection.runningQuantities
    }

    private var requestedQuantities: ResourceQuantityRecord {
        projection.requestedQuantities
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider().opacity(0.45)
            HStack(spacing: 0) {
                groupNavigator
                Divider().opacity(0.45)
                Group {
                    if let selectedGroup {
                        ResourceUsageGroupDetail(
                            store: store,
                            group: selectedGroup,
                            inlineMessage: inlineMessage,
                            release: release
                        )
                        .id(selectedGroup.id)
                    } else {
                        ContentUnavailableView(
                            "还没有项目或 Agent 申请资源",
                            systemImage: "person.2.slash",
                            description: Text("点击“申请 GPU”开始分配资源。")
                        )
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .spatialContentSurface()
            }
        }
        .background(DesignTokens.surface)
        .onAppear { ensureSelectedGroup() }
        .onChange(of: scope) { _, _ in ensureSelectedGroup(reset: true) }
        .onChange(of: store.snapshot.snapshotRevision) { _, _ in ensureSelectedGroup() }
        .accessibilityLabel("项目与 Agent")
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("项目与 Agent")
                        .font(.system(size: 22, weight: .semibold))
                        .foregroundStyle(DesignTokens.ink)
                    Text("按项目、Agent 或任务查看资源分配、运行和排队状态")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                }
                Spacer(minLength: 20)
                Label("记录资源归属，不控制远端任务", systemImage: "hand.raised.fill")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }

            HStack(spacing: 10) {
                ResourceUsageCountMetric(value: "\(projectCount)", label: "项目", icon: "folder.fill")
                    .frame(width: 86)
                ResourceUsageCountMetric(value: "\(agentCount)", label: "Agent", icon: "person.crop.circle.fill")
                    .frame(width: 86)
                ResourceUsageCountMetric(value: "\(taskCount)", label: "任务", icon: "checklist.checked")
                    .frame(width: 86)
                ResourceUsageSummaryMetric(value: assignedQuantities.compactLabel, label: "已分配", icon: "checkmark.circle.fill")
                    .frame(maxWidth: .infinity)
                ResourceUsageSummaryMetric(value: runningQuantities.compactLabel, label: "运行中", icon: "play.circle.fill")
                    .frame(maxWidth: .infinity)
                ResourceUsageSummaryMetric(value: requestedQuantities.compactLabel, label: "申请中", icon: "hourglass")
                    .frame(maxWidth: .infinity)
            }
        }
        .padding(.horizontal, 24)
        .padding(.top, 14)
        .padding(.bottom, 16)
        .background(DesignTokens.surface)
    }

    private var groupNavigator: some View {
        VStack(alignment: .leading, spacing: 0) {
            Picker("查看方式", selection: $scope) {
                ForEach(ResourceUsageScope.allCases) { item in
                    Text(item.label).tag(item)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding(14)
            .accessibilityLabel("按项目、Agent 或任务查看")

            HStack {
                Text("\(scope.label)列表")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(DesignTokens.ink)
                Spacer()
                Text("\(groups.count)")
                    .font(.system(size: 10, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            .padding(.horizontal, 16)
            .padding(.bottom, 9)

            ScrollView {
                LazyVStack(spacing: 4) {
                    ForEach(groups) { group in
                        ResourceUsageGroupRow(
                            group: group,
                            selected: group.id == selectedGroup?.id
                        ) {
                            selectedGroupID = group.id
                            inlineMessage = nil
                        }
                    }
                }
                .padding(.horizontal, 9)
                .padding(.bottom, 12)
            }
        }
        .frame(width: 292)
        .background(DesignTokens.surface)
    }

    private func ensureSelectedGroup(reset: Bool = false) {
        if reset || !groups.contains(where: { $0.id == selectedGroupID }) {
            selectedGroupID = groups.first?.id ?? ""
            inlineMessage = nil
        }
    }

    private func release(_ lease: LeaseRecord) {
        guard confirmLeaseRelease(lease) else { return }
        inlineMessage = nil
        store.releaseLease(lease) { success, error in
            if success {
                inlineMessage = "资源已归还。"
            } else {
                inlineMessage = error ?? "没有归还成功，请稍后再试。"
            }
        }
    }
}

private struct ResourceUsageCountMetric: View {
    let value: String
    let label: String
    let icon: String

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Image(systemName: icon)
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(DesignTokens.mutedInk)
                Text(value)
                    .font(.system(size: 15, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.ink)
            }
            Text(label)
                .font(.system(size: 9, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
                .lineLimit(1)
        }
        .padding(.horizontal, 11)
        .frame(maxWidth: .infinity, minHeight: 50, alignment: .leading)
        .background(DesignTokens.glassSmoke, in: RoundedRectangle(cornerRadius: 11, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 11, style: .continuous).stroke(DesignTokens.surfaceStroke, lineWidth: 1))
        .accessibilityElement(children: .combine)
        .accessibilityLabel(label)
        .accessibilityValue(value)
    }
}

private struct ResourceUsageSummaryMetric: View {
    let value: String
    let label: String
    let icon: String

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: icon)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(DesignTokens.mutedInk)
                .frame(width: 30, height: 30)
                .background(DesignTokens.mutedInk.opacity(0.09), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text(value)
                    .font(.system(size: value.count > 14 ? 11 : 15, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.ink)
                    .lineLimit(1)
                    .minimumScaleFactor(0.75)
                Text(label)
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 11)
        .frame(minHeight: 50)
        .background(DesignTokens.glassSmoke, in: RoundedRectangle(cornerRadius: 11, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 11, style: .continuous).stroke(DesignTokens.surfaceStroke, lineWidth: 1))
        .accessibilityElement(children: .combine)
        .accessibilityLabel(label)
        .accessibilityValue(value)
    }
}

private struct ResourceUsageGroupRow: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var hovering = false
    let group: ResourceUsageGroup
    let selected: Bool
    let select: () -> Void

    var body: some View {
        Button(action: select) {
            HStack(spacing: 11) {
                Image(systemName: group.scope.icon)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(selected ? DesignTokens.interaction : DesignTokens.mutedInk)
                    .frame(width: 30, height: 30)
                    .background(
                        (selected ? DesignTokens.interaction : DesignTokens.mutedInk).opacity(0.10),
                        in: RoundedRectangle(cornerRadius: 8, style: .continuous)
                    )
                VStack(alignment: .leading, spacing: 3) {
                    Text(group.title)
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(DesignTokens.ink)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Text(group.subtitle)
                        .font(.system(size: 9, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                }
                Spacer(minLength: 8)
                VStack(alignment: .trailing, spacing: 3) {
                    Text("\(group.activityCount)")
                        .font(.system(size: 11, weight: .semibold, design: .rounded))
                        .foregroundStyle(DesignTokens.ink)
                    Text(group.hasPendingWork ? "有申请" : "条记录")
                        .font(.system(size: 8, weight: .medium))
                        .foregroundStyle(group.hasPendingWork ? DesignTokens.warning : DesignTokens.mutedInk)
                        .lineLimit(1)
                }
            }
            .padding(.horizontal, 10)
            .frame(height: 58)
            .background(
                selected ? DesignTokens.interaction.opacity(0.11) : DesignTokens.ink.opacity(hovering ? 0.04 : 0),
                in: RoundedRectangle(cornerRadius: 9, style: .continuous)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.14), value: hovering)
        .accessibilityLabel("\(group.scope.label) \(group.title)")
        .accessibilityValue("已分配 \(group.assignedQuantities.compactLabel)，运行中 \(group.runningQuantities.compactLabel)，申请中 \(group.requestedQuantities.compactLabel)")
    }
}

private struct ResourceUsageGroupDetail: View {
    @ObservedObject var store: BrokerStore
    let group: ResourceUsageGroup
    let inlineMessage: String?
    let release: (LeaseRecord) -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                HStack(alignment: .top, spacing: 14) {
                    Image(systemName: group.scope.icon)
                        .font(.system(size: 19, weight: .semibold))
                        .foregroundStyle(DesignTokens.interaction)
                        .frame(width: 44, height: 44)
                        .background(DesignTokens.interaction.opacity(0.10), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    VStack(alignment: .leading, spacing: 4) {
                        Text(group.title)
                            .font(.system(size: 24, weight: .semibold))
                            .foregroundStyle(DesignTokens.ink)
                            .lineLimit(2)
                        Text(group.subtitle)
                            .font(.system(size: 11, weight: .medium))
                            .foregroundStyle(DesignTokens.mutedInk)
                    }
                    Spacer(minLength: 0)
                    Text("\(group.activityCount) 条记录")
                        .font(.system(size: 10, weight: .semibold, design: .rounded))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .background(DesignTokens.surface, in: Capsule())
                        .overlay(Capsule().stroke(DesignTokens.surfaceStroke, lineWidth: 1))
                }

                if let inlineMessage {
                    Label(inlineMessage, systemImage: inlineMessage == "资源已归还。" ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(inlineMessage == "资源已归还。" ? DesignTokens.success : DesignTokens.danger)
                        .padding(11)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(DesignTokens.surface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).stroke(DesignTokens.surfaceStroke, lineWidth: 1))
                }

                LazyVGrid(columns: [GridItem(.adaptive(minimum: 180, maximum: 320), spacing: 12)], spacing: 12) {
                    ResourceQuantityPanel(
                        title: "已分配",
                        subtitle: "资源已归属，尚未检测到任务进程",
                        quantities: group.assignedQuantities,
                        icon: "checkmark.circle.fill"
                    )
                    ResourceQuantityPanel(
                        title: "运行中",
                        subtitle: "已检测到任务进程；数值为分配量",
                        quantities: group.runningQuantities,
                        icon: "play.circle.fill"
                    )
                    ResourceQuantityPanel(
                        title: "申请中",
                        subtitle: "等待分配的资源",
                        quantities: group.requestedQuantities,
                        icon: "hourglass"
                    )
                }

                if !group.assignedClaims.isEmpty || !group.runningClaims.isEmpty || !group.leases.isEmpty {
                    ResourceUsageSectionTitle(
                        title: "当前资源",
                        detail: "\(group.assignedClaims.count + group.runningClaims.count + group.leases.count) 条"
                    )
                    VStack(spacing: 6) {
                        ForEach(group.assignedClaims) { claim in
                            ResourceClaimDetailRow(claim: claim)
                        }
                        ForEach(group.runningClaims) { claim in
                            ResourceClaimDetailRow(claim: claim)
                        }
                        ForEach(group.leases) { lease in
                            ResourceLeaseDetailRow(
                                store: store,
                                lease: lease,
                                release: { release(lease) }
                            )
                        }
                    }
                }

                if !group.pendingClaims.isEmpty || !group.visibleLegacyRequests.isEmpty {
                    ResourceUsageSectionTitle(
                        title: "等待中的申请",
                        detail: "\(group.pendingClaims.count + group.visibleLegacyRequests.count) 条"
                    )
                    VStack(spacing: 6) {
                        ForEach(group.pendingClaims) { claim in
                            ResourceClaimDetailRow(claim: claim)
                        }
                        ForEach(group.visibleLegacyRequests) { request in
                            ResourceRequestDetailRow(request: request)
                        }
                    }
                }

                if !group.actuals.isEmpty {
                    ResourceUsageSectionTitle(title: "最近任务记录", detail: "\(group.actuals.count) 条")
                    VStack(spacing: 6) {
                        ForEach(group.actuals) { actual in
                            ResourceActualDetailRow(actual: actual)
                        }
                    }
                }

                if group.activityCount == 0 {
                    ContentUnavailableView("暂无记录", systemImage: "tray")
                }

                Label("资源归属与远端任务生命周期彼此独立", systemImage: "hand.raised.fill")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            .padding(24)
            .padding(.bottom, 60)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

private struct ResourceQuantityPanel: View {
    let title: String
    let subtitle: String
    let quantities: ResourceQuantityRecord
    let icon: String

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Image(systemName: icon)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(DesignTokens.mutedInk)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(DesignTokens.ink)
                    Text(subtitle)
                        .font(.system(size: 9, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(2)
                }
            }
            HStack(spacing: 8) {
                ResourceQuantityItem(label: "CPU", value: usageCPUText(quantities.cpuCores), icon: "cpu")
                ResourceQuantityItem(label: "内存", value: usageMemoryText(quantities.memoryMiB), icon: "memorychip")
                ResourceQuantityItem(label: "GPU", value: "\(quantities.gpuCount)", icon: "square.stack.3d.up.fill")
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(DesignTokens.surface, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).stroke(DesignTokens.surfaceStroke, lineWidth: 1))
        .accessibilityElement(children: .combine)
        .accessibilityLabel(title)
        .accessibilityValue(quantities.compactLabel)
    }
}

private struct ResourceQuantityItem: View {
    let label: String
    let value: String
    let icon: String

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 4) {
                Image(systemName: icon)
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(DesignTokens.mutedInk)
                Text(label)
                    .font(.system(size: 8, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
            }
            Text(value)
                .font(.system(size: 12, weight: .semibold, design: .rounded))
                .foregroundStyle(DesignTokens.ink)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
        .padding(.horizontal, 9)
        .frame(maxWidth: .infinity, minHeight: 45, alignment: .leading)
        .background(DesignTokens.glassSmoke, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

private struct ResourceUsageSectionTitle: View {
    let title: String
    let detail: String

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(title)
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(DesignTokens.ink)
            Spacer()
            Text(detail)
                .font(.system(size: 9, weight: .semibold, design: .rounded))
                .foregroundStyle(DesignTokens.mutedInk)
        }
    }
}

private struct ResourceClaimDetailRow: View {
    let claim: ResourceClaimRecord

    var body: some View {
        ResourceUsageRecordShell {
            HStack(spacing: 10) {
                ResourceRecordIcon(systemName: "key.fill")
                VStack(alignment: .leading, spacing: 2) {
                    Text(claim.taskReference.isEmpty ? (claim.purpose ?? "未命名任务") : claim.taskReference)
                        .font(.system(size: 11, weight: .semibold))
                        .lineLimit(1)
                    Text("\(claim.projectID) · \(claim.actorID)")
                        .font(.system(size: 9, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                }
                Spacer(minLength: 10)
                Text(claim.quantities.compactLabel)
                    .font(.system(size: 10, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.ink)
                    .lineLimit(1)
                Text(claim.stateLabel)
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(claim.state == "REJECTED" ? DesignTokens.danger : DesignTokens.interaction)
                    .frame(width: 58, alignment: .trailing)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("资源申请 \(claim.taskReference)")
        .accessibilityValue("\(claim.stateLabel)，\(claim.quantities.compactLabel)")
    }
}

private struct ResourceLeaseDetailRow: View {
    @ObservedObject var store: BrokerStore
    let lease: LeaseRecord
    let release: () -> Void

    var body: some View {
        ResourceUsageRecordShell {
            HStack(spacing: 10) {
                ResourceRecordIcon(systemName: "square.stack.3d.up.fill")
                VStack(alignment: .leading, spacing: 2) {
                    Text(lease.taskReference ?? lease.purpose ?? "未命名任务")
                        .font(.system(size: 11, weight: .semibold))
                        .lineLimit(1)
                    Text("\(lease.projectID) · \(lease.actorID) · 到期 \(usageTimestamp(lease.expiresAt))")
                        .font(.system(size: 9, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                }
                Spacer(minLength: 10)
                Text("\(lease.gpuIDs.count) GPU")
                    .font(.system(size: 10, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.ink)
                Text(lease.stateLabel)
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(DesignTokens.success)
                Button(action: release) {
                    Label(store.releasingLeaseIDs.contains(lease.id) ? "归还中" : "归还", systemImage: "arrow.uturn.backward")
                        .font(.system(size: 9, weight: .semibold))
                }
                .buttonStyle(SecondaryActionButtonStyle())
                .disabled(!store.allowsMutations || store.releasingLeaseIDs.contains(lease.id))
                .help(store.allowsMutations ? "归还资源；不会停止远端任务" : store.mutationUnavailableReason)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("GPU 使用记录 \(lease.taskReference ?? lease.id)")
    }
}

private struct ResourceRequestDetailRow: View {
    let request: AllocationRequestRecord

    var body: some View {
        ResourceUsageRecordShell {
            HStack(spacing: 10) {
                ResourceRecordIcon(systemName: "hourglass")
                VStack(alignment: .leading, spacing: 2) {
                    Text(request.taskReference)
                        .font(.system(size: 11, weight: .semibold))
                        .lineLimit(1)
                    Text("\(request.projectID) · \(request.actorID) · \(usageTimestamp(request.createdAt))")
                        .font(.system(size: 9, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                }
                Spacer(minLength: 10)
                Text("\(request.gpuCount) GPU")
                    .font(.system(size: 10, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.ink)
                Text(request.stateLabel)
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(DesignTokens.warning)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("等待中的 GPU 申请 \(request.taskReference)")
        .accessibilityValue("\(request.gpuCount) GPU，\(request.stateLabel)")
    }
}

private struct ResourceActualDetailRow: View {
    let actual: ResourceRunActualRecord

    var body: some View {
        ResourceUsageRecordShell {
            HStack(spacing: 10) {
                ResourceRecordIcon(systemName: "checklist.checked")
                VStack(alignment: .leading, spacing: 2) {
                    Text(actual.taskReference.isEmpty ? actual.id : actual.taskReference)
                        .font(.system(size: 11, weight: .semibold))
                        .lineLimit(1)
                    Text("\(actual.projectID) · \(actual.actorID)")
                        .font(.system(size: 9, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                }
                Spacer(minLength: 10)
                Text(actual.quantities.compactLabel)
                    .font(.system(size: 10, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.ink)
                    .lineLimit(1)
                Text("实际 \(usageDuration(actual.actualDurationSeconds))")
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("任务记录 \(actual.taskReference)")
        .accessibilityValue("\(actual.quantities.compactLabel)，实际耗时 \(usageDuration(actual.actualDurationSeconds))")
    }
}

private struct ResourceUsageRecordShell<Content: View>: View {
    @ViewBuilder let content: Content

    var body: some View {
        content
            .padding(.horizontal, 12)
            .frame(minHeight: 50)
            .background(DesignTokens.surface, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous).stroke(DesignTokens.surfaceStroke, lineWidth: 1))
    }
}

private struct ResourceRecordIcon: View {
    let systemName: String

    var body: some View {
        Image(systemName: systemName)
            .font(.system(size: 11, weight: .semibold))
            .foregroundStyle(DesignTokens.mutedInk)
            .frame(width: 28, height: 28)
            .background(DesignTokens.mutedInk.opacity(0.09), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
    }
}

private struct ResourceUsageIdentity {
    let projectID: String
    let actorID: String
    let taskReference: String
}

private func resourceUsageIdentities(snapshot: BrokerSnapshot) -> [ResourceUsageIdentity] {
    var identities: [ResourceUsageIdentity] = []
    let linkedLeaseIDs = Set(snapshot.resourceClaims.flatMap(\.nativeLeaseIDs))
    let linkedRequestIDs = Set(snapshot.resourceClaims.flatMap(\.nativeRequestIDs))
    identities.append(contentsOf: snapshot.resourceClaims.map {
        ResourceUsageIdentity(
            projectID: $0.projectID,
            actorID: $0.actorID,
            taskReference: normalizedTask($0.taskReference, purpose: $0.purpose)
        )
    })
    identities.append(contentsOf: snapshot.leases.filter { !linkedLeaseIDs.contains($0.id) }.map {
        ResourceUsageIdentity(
            projectID: $0.projectID,
            actorID: $0.actorID,
            taskReference: normalizedTask($0.taskReference, purpose: $0.purpose)
        )
    })
    identities.append(contentsOf: snapshot.requests.filter { !linkedRequestIDs.contains($0.id) }.map {
        ResourceUsageIdentity(projectID: $0.projectID, actorID: $0.actorID, taskReference: normalizedTask($0.taskReference))
    })
    identities.append(contentsOf: snapshot.resourceRunActuals.map {
        ResourceUsageIdentity(projectID: $0.projectID, actorID: $0.actorID, taskReference: normalizedTask($0.taskReference))
    })
    return identities
}

private func makeResourceUsageGroups(snapshot: BrokerSnapshot, scope: ResourceUsageScope) -> [ResourceUsageGroup] {
    var buckets: [String: ResourceUsageBucket] = [:]
    let linkedLeaseIDs = Set(snapshot.resourceClaims.flatMap(\.nativeLeaseIDs))
    let linkedRequestIDs = Set(snapshot.resourceClaims.flatMap(\.nativeRequestIDs))

    func add(
        projectID: String,
        actorID: String,
        taskReference: String,
        update: (inout ResourceUsageBucket) -> Void
    ) {
        let key: String
        let title: String
        switch scope {
        case .project:
            key = projectID
            title = projectID
        case .agent:
            key = actorID
            title = actorID
        case .task:
            key = "\(projectID)\u{1F}\(taskReference)"
            title = taskReference
        }
        var bucket = buckets[key] ?? ResourceUsageBucket(key: key, title: title)
        bucket.projectIDs.insert(projectID)
        bucket.actorIDs.insert(actorID)
        bucket.taskReferences.insert(taskReference)
        update(&bucket)
        buckets[key] = bucket
    }

    for claim in snapshot.resourceClaims {
        add(
            projectID: claim.projectID,
            actorID: claim.actorID,
            taskReference: normalizedTask(claim.taskReference, purpose: claim.purpose)
        ) { $0.claims.append(claim) }
    }
    for lease in snapshot.leases where !linkedLeaseIDs.contains(lease.id) {
        add(
            projectID: lease.projectID,
            actorID: lease.actorID,
            taskReference: normalizedTask(lease.taskReference, purpose: lease.purpose)
        ) { $0.leases.append(lease) }
    }
    for request in snapshot.requests where !linkedRequestIDs.contains(request.id) {
        add(
            projectID: request.projectID,
            actorID: request.actorID,
            taskReference: normalizedTask(request.taskReference)
        ) { $0.requests.append(request) }
    }
    for actual in snapshot.resourceRunActuals {
        add(
            projectID: actual.projectID,
            actorID: actual.actorID,
            taskReference: normalizedTask(actual.taskReference)
        ) { $0.actuals.append(actual) }
    }

    return buckets.values.map { bucket in
        ResourceUsageGroup(
            id: "\(scope.rawValue):\(bucket.key)",
            scope: scope,
            title: bucket.title,
            projectIDs: bucket.projectIDs.sorted(),
            actorIDs: bucket.actorIDs.sorted(),
            taskReferences: bucket.taskReferences.sorted(),
            claims: bucket.claims.sorted { ($0.createdAt ?? "") > ($1.createdAt ?? "") },
            leases: bucket.leases.sorted { ($0.issuedAt ?? "") > ($1.issuedAt ?? "") },
            requests: bucket.requests.sorted { ($0.createdAt ?? "") > ($1.createdAt ?? "") },
            actuals: bucket.actuals.sorted { ($0.createdAt ?? "") > ($1.createdAt ?? "") }
        )
    }
    .sorted {
        if $0.hasPendingWork != $1.hasPendingWork { return $0.hasPendingWork }
        if $0.activityCount != $1.activityCount { return $0.activityCount > $1.activityCount }
        return $0.title.localizedStandardCompare($1.title) == .orderedAscending
    }
}

private func combinedQuantities(_ values: [ResourceQuantityRecord]) -> ResourceQuantityRecord {
    ResourceQuantityRecord(
        cpuCores: values.reduce(0) { $0 + $1.cpuCores },
        memoryMiB: values.reduce(0) { $0 + $1.memoryMiB },
        gpuCount: values.reduce(0) { $0 + $1.gpuCount },
        nodeCount: values.reduce(0) { $0 + $1.nodeCount },
        schedulerUnits: values.reduce(0) { $0 + $1.schedulerUnits }
    )
}

private func normalizedTask(_ taskReference: String?, purpose: String? = nil) -> String {
    let task = taskReference?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    if !task.isEmpty { return task }
    let purposeText = purpose?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    return purposeText.isEmpty ? "未标注任务" : purposeText
}

private func usageCPUText(_ cores: Double) -> String {
    let rounded = (cores * 10).rounded() / 10
    return rounded == Double(Int(rounded)) ? "\(Int(rounded)) 核" : String(format: "%.1f 核", rounded)
}

private func usageMemoryText(_ mebibytes: Int) -> String {
    let gibibytes = Double(mebibytes) / 1024
    if gibibytes == Double(Int(gibibytes)) {
        return "\(Int(gibibytes)) GB"
    }
    return String(format: "%.1f GB", gibibytes)
}

private func usageTimestamp(_ value: String?) -> String {
    guard let value, !value.isEmpty else { return "未提供" }
    return value.replacingOccurrences(of: "T", with: " ").replacingOccurrences(of: "Z", with: "")
}

private func usageDuration(_ seconds: Int?) -> String {
    guard let seconds else { return "未记录" }
    if seconds < 60 { return "\(seconds) 秒" }
    if seconds < 3600 { return "\(seconds / 60) 分钟" }
    return String(format: "%.1f 小时", Double(seconds) / 3600)
}
