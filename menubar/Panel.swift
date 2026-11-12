import AppKit
import SwiftUI

private let accent = Color(red: 0.07, green: 0.49, blue: 0.43)

private enum PowerReading: String {
    case power = "power.latest"
    case energy = "power.energy"
    case savings = "power.savings"

    var next: Self {
        switch self {
        case .power: return .energy
        case .energy: return .savings
        case .savings: return .power
        }
    }
}

private struct Card<Accessory: View, Content: View>: View {
    let title: String
    let icon: String
    @ViewBuilder let accessory: Accessory
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: icon)
                    .frame(width: 16)
                    .font(.system(size: 13, weight: .semibold))
                    .accessibilityHidden(true)
                Text(title).font(.system(size: 13, weight: .semibold))
                Spacer(minLength: 8)
                accessory
            }
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(.primary.opacity(0.07)))
    }
}

private extension Card where Accessory == EmptyView {
    init(title: String, icon: String, @ViewBuilder content: () -> Content) {
        self.init(title: title, icon: icon, accessory: { EmptyView() }, content: content)
    }
}

private struct Metric: View {
    let title: String
    let value: String
    var emphasized = false
    var canCycle = false

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 4) {
                Text(title)
                if canCycle {
                    Image(systemName: "arrow.left.arrow.right").font(.system(size: 8))
                        .accessibilityHidden(true)
                }
            }
            .font(.system(size: 11))
            .foregroundStyle(.secondary)
            Text(value)
                .font(.system(size: emphasized ? 20 : 15, weight: .semibold))
                .monospacedDigit()
                .foregroundStyle(emphasized ? accent : Color.primary)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
                .accessibilityIdentifier(title)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct DetailLine: View {
    let label: String
    let value: String

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label).foregroundStyle(.secondary)
            Spacer(minLength: 8)
            Text(value).monospacedDigit().multilineTextAlignment(.trailing)
        }
        .font(.system(size: 12))
    }
}

struct Panel: View {
    private let inset: CGFloat = 18
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ObservedObject var model: ObservationModel
    @State private var strategy: Bool
    @State private var powerReading: PowerReading = .power
    @State private var savingsAsBatteryPercent = false
    @State private var displayedSuggestion: Int?
    @State private var suggestionFading = false

    init(model: ObservationModel) {
        self.model = model
        _strategy = State(initialValue: model.options.showStrategy)
    }

    private var snapshot: ObservationSnapshot? { model.snapshot }
    private var forecast: Forecast? {
        guard let snapshot, (snapshot.windowCount ?? 0) > 0 else { return nil }
        return snapshot.forecasts?.first { $0.minutes == model.threshold }
    }
    private var strategySleepSeconds: Int? {
        model.automatic ? snapshot?.u3Seconds7d : forecast?.seconds
    }
    private var suggestedThreshold: Int? {
        guard let minutes = snapshot?.recommendation, minutes != model.threshold else { return nil }
        return minutes
    }
    private var l10n: AppLocalization { AppLocalization(model.language) }

    private func updateDisplayedSuggestion(_ suggestion: Int?) async {
        if let suggestion {
            displayedSuggestion = suggestion
            suggestionFading = false
        } else if displayedSuggestion != nil {
            if !reduceMotion {
                withAnimation(.easeOut(duration: 0.22)) { suggestionFading = true }
                do {
                    try await Task.sleep(for: .milliseconds(220))
                } catch {
                    return
                }
            }
            displayedSuggestion = nil
            suggestionFading = false
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Picker(l10n["tabs.title"], selection: $strategy) {
                Text(l10n["tabs.overview"]).tag(false)
                Text(l10n["tabs.strategy"]).tag(true)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .accessibilityIdentifier("panelTabs")
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, inset)
            .padding(.bottom, 12)

            VStack(spacing: 10) {
                if let issue = model.loginItemIssue ?? model.observationIssue ?? model.labIssue {
                    Label(l10n[issue.rawValue], systemImage: "exclamationmark.circle")
                        .font(.system(size: 12))
                        .foregroundStyle(.orange)
                        .frame(maxWidth: .infinity, alignment: .leading)
                } else if model.observing && snapshot?.state == "waiting" {
                    Label(l10n["device.waiting"], systemImage: "externaldrive.badge.questionmark")
                        .font(.system(size: 12))
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                if model.needsLoginApproval {
                    Button(l10n["login.approval"], systemImage: "gearshape") { model.openLoginSettings() }
                        .buttonStyle(.borderless)
                        .font(.system(size: 12))
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                if strategy {
                    strategyCard
                } else {
                    powerCard
                    idleCard
                    lidCard
                }
            }
            .padding(.horizontal, inset)
            .padding(.bottom, 12)
            Divider()
            footer
        }
        .frame(width: 412)
        .fixedSize(horizontal: false, vertical: true)
        .background(Color(nsColor: .windowBackgroundColor))
        .tint(accent)
        .environment(\.locale, l10n.locale)
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: "externaldrive.fill")
                .font(.system(size: 18, weight: .medium))
                .foregroundStyle(accent)
                .frame(width: 40, height: 40)
                .background(accent.opacity(0.12), in: RoundedRectangle(cornerRadius: 11))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(l10n["app.name"]).font(.system(size: 17, weight: .semibold))
                Text(model.options.preview ? l10n["device.preview"] : snapshot?.device ?? "USB SSD")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 8)
            HStack(spacing: 6) {
                let mode = l10n[model.automatic ? "mode.automatic" : "mode.observation"]
                Text(mode).font(.system(size: 11, weight: .medium))
                Toggle(mode, isOn: Binding(get: { model.automatic }, set: { model.setAutomatic($0) }))
                    .labelsHidden()
                    .toggleStyle(.switch)
                    .controlSize(.small)
                    .disabled(!model.labReady || model.settingPolicy)
                    .accessibilityIdentifier("automaticSleepToggle")
                    .help(l10n["strategy.testScope"])
            }
        }
        .padding(.horizontal, inset)
        .padding(.vertical, 14)
    }

    private var powerCard: some View {
        Card(title: l10n["power.title"], icon: "bolt") {
            HStack(alignment: .top, spacing: 12) {
                Button {
                    powerReading = powerReading.next
                } label: {
                    VStack(alignment: .leading, spacing: 5) {
                        HStack(spacing: 4) {
                            Text(l10n[powerReading.rawValue])
                            Image(systemName: "arrow.left.arrow.right").font(.system(size: 8))
                                .accessibilityHidden(true)
                        }
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .minimumScaleFactor(0.8)
                        Text(powerValue)
                            .font(.system(size: 20, weight: .semibold))
                            .foregroundStyle(accent)
                            .monospacedDigit()
                            .lineLimit(1)
                            .minimumScaleFactor(0.7)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(l10n["power.cycle"])
                .accessibilityValue(l10n.format("power.reading", l10n[powerReading.rawValue], powerValue))
                .accessibilityIdentifier("cyclePowerReading")
                .help(l10n["power.cycle.help"])
                Metric(title: l10n["power.idle"], value: power(snapshot?.idleWatts))
                Metric(title: l10n["power.sleep"], value: power(snapshot?.u3Watts))
            }
            Text(powerDetail).font(.system(size: 11)).foregroundStyle(.secondary)
        }
    }

    private var powerValue: String {
        switch powerReading {
        case .power: return power(snapshot?.watts)
        case .energy: return power(snapshot?.energyWh24h, unit: "Wh")
        case .savings: return power(snapshot?.estimatedWattHours, unit: "Wh")
        }
    }

    private func power(_ value: Double?, unit: String = "W") -> String {
        guard let value else { return "—" }
        return "\(value.formatted(.number.precision(.fractionLength(3)).locale(l10n.locale))) \(unit)"
    }

    private var powerDetail: String {
        switch powerReading {
        case .power:
            if let time = snapshot?.sampleTime, snapshot?.watts != nil {
                let timeLabel = Date(timeIntervalSince1970: time).formatted(.dateTime.hour().minute().second().locale(l10n.locale))
                return l10n.format("power.sample", timeLabel)
            }
            return l10n["power.waiting"]
        case .energy:
            return l10n.format("power.coverage", l10n.duration(snapshot?.powerCoverageSeconds))
        case .savings:
            return l10n[snapshot?.estimatedWattHours != nil ? "power.savings.method" : "power.savings.waiting"]
        }
    }

    private var idleCard: some View {
        Card(title: l10n["idle.title"], icon: "moon.zzz") {
            HStack(alignment: .top, spacing: 12) {
                Metric(title: l10n["idle.current"], value: l10n.duration(snapshot?.quietSeconds.map { Int($0) }, compact: true), emphasized: true)
                Metric(title: l10n["idle.total"], value: l10n.duration(snapshot?.quiet24hSeconds, compact: true))
                Metric(title: l10n["idle.sleep"], value: l10n.duration(snapshot?.u3Seconds, compact: true))
            }
        }
    }

    private var lidCard: some View {
        Card(title: l10n["lid.title"], icon: "laptopcomputer.and.arrow.down") {
            if let records = snapshot?.lids, let latest = records.last {
                HStack(alignment: .top, spacing: 12) {
                    Metric(title: l10n["lid.battery"],
                           value: "\(batteryPercent(latest.before)) → \(batteryPercent(latest.after))",
                           emphasized: true)
                    Metric(title: l10n["lid.duration"], value: l10n.duration(Int(latest.end - latest.start)))
                }
                DetailLine(label: l10n["lid.ssd"], value: latest.u3BeforeSleep.map {
                    l10n[$0 ? "lid.ssd.confirmed" : "lid.ssd.awake"]
                } ?? l10n["lid.noData"])
                if let drop = latest.drop, latest.onBattery, drop >= 0 {
                    DetailLine(label: l10n["lid.drop"], value: l10n.percentagePoints(drop))
                } else {
                    DetailLine(label: l10n["lid.comparison"], value: l10n["lid.comparison.unavailable"])
                }
                if latest.sameDevice == false {
                    DetailLine(label: l10n["lid.comparison"], value: l10n["lid.deviceAbsent"])
                }
                Divider()
                DisclosureGroup(l10n.format(records.count == 1 ? "lid.history.one" : "lid.history.other", records.count)) {
                    VStack(spacing: 6) {
                        ForEach(Array(records.reversed())) { session in
                            DetailLine(label: Date(timeIntervalSince1970: session.end)
                                .formatted(.dateTime.year().month(.abbreviated).day().hour().minute().locale(l10n.locale)),
                                       value: session.onBattery ? session.drop.map(l10n.percentagePoints) ?? "—" : l10n["lid.pluggedIn"])
                        }
                    }
                    .padding(.top, 6)
                }
                .font(.system(size: 11, weight: .medium))
                if let comparison = snapshot?.lidComparison {
                    let key = comparison.percentPoints >= 0 ? "lid.comparison.savings" : "lid.comparison.extra"
                    Text(l10n.format(key, comparison.hours, abs(comparison.percentPoints)))
                        .font(.system(size: 12, weight: .medium))
                } else {
                    Text(l10n["lid.comparison.waiting"]).font(.system(size: 11)).foregroundStyle(.secondary)
                }
            } else {
                Text(l10n["lid.waiting"]).font(.system(size: 13, weight: .medium))
                Text(l10n["lid.waiting.detail"])
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
        }
    }

    private func batteryPercent(_ value: Int?) -> String {
        guard let value else { return "—" }
        return (Double(value) / 100).formatted(.percent.precision(.fractionLength(0)).locale(l10n.locale))
    }

    private var savingsMetric: some View {
        let energy = snapshot?.savingsWh(forSleepSeconds: strategySleepSeconds)
        let savingsTitle = model.automatic ? "strategy.savings.actual" : "strategy.savings"
        let title = l10n[savingsAsBatteryPercent ? "strategy.savings.battery" : savingsTitle]
        let value: String
        let hint: String
        if savingsAsBatteryPercent {
            if let energy, let capacity = snapshot?.batteryFullWh, capacity > 0 {
                value = (energy / capacity).formatted(.percent.precision(.fractionLength(2)).locale(l10n.locale))
                hint = l10n["strategy.savings.cycle"]
            } else {
                value = "—"
                hint = l10n[energy == nil ? "strategy.savings.waiting" : "strategy.savings.batteryUnavailable"]
            }
        } else {
            value = power(energy, unit: "Wh")
            hint = l10n[energy == nil ? "strategy.savings.waiting" : "strategy.savings.cycle"]
        }
        return Button {
            savingsAsBatteryPercent.toggle()
        } label: {
            Metric(title: title, value: value, emphasized: true, canCycle: true)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(title)
        .accessibilityValue(value)
        .accessibilityIdentifier("cycleStrategySavings")
        .help(hint)
    }

    private var strategyCard: some View {
        VStack(spacing: 10) {
            Card(title: l10n["strategy.threshold"], icon: "timer", accessory: {
                HStack(spacing: 8) {
                    Text(l10n.minutes(model.threshold)).font(.system(size: 16, weight: .semibold)).monospacedDigit()
                        .accessibilityIdentifier("selectedThreshold")
                    Stepper(l10n["strategy.custom"], value: Binding(get: { model.threshold }, set: model.setThreshold),
                            in: 1...120)
                        .labelsHidden()
                        .controlSize(.small)
                }
            }) {
                HStack(spacing: 6) {
                    ForEach([2, 5, 10, 20, 30], id: \.self) { minutes in
                        Button {
                            model.setThreshold(minutes)
                        } label: {
                            Text(l10n.minutes(minutes, abbreviated: true))
                                .font(.system(size: 11, weight: model.threshold == minutes ? .semibold : .regular))
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 6)
                                .background(accent.opacity(model.threshold == minutes ? 0.17 : 0.05),
                                            in: RoundedRectangle(cornerRadius: 6))
                        }
                        .buttonStyle(.plain)
                        .accessibilityAddTraits(model.threshold == minutes ? .isSelected : [])
                    }
                }
                Divider()
                HStack {
                    Text(l10n["strategy.batteryOnly"]).font(.system(size: 12))
                        .accessibilityHidden(true)
                    Spacer(minLength: 8)
                    Toggle(l10n["strategy.batteryOnly"],
                           isOn: Binding(get: { model.batteryOnly }, set: model.setBatteryOnly))
                        .labelsHidden()
                        .toggleStyle(.switch)
                        .controlSize(.small)
                        .accessibilityIdentifier("batteryOnlyToggle")
                }
                if model.automatic {
                    Text(l10n["strategy.testScope"])
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                }
            }
            .disabled(model.settingPolicy)
            Card(title: l10n[model.automatic ? "strategy.actual" : "strategy.estimate"], icon: "chart.bar.xaxis") {
                HStack(alignment: .top, spacing: 12) {
                    Metric(title: l10n[model.automatic ? "strategy.sleep.actual" : "strategy.sleep"],
                           value: l10n.duration(strategySleepSeconds), emphasized: true)
                    savingsMetric
                }
            }
            if let minutes = displayedSuggestion {
                Card(title: l10n["strategy.recommendation"], icon: "lightbulb") {
                    Text(l10n.minutes(minutes))
                        .font(.system(size: 17, weight: .semibold))
                    Button(l10n["strategy.apply"]) {
                        model.setThreshold(minutes)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.settingPolicy)
                    .accessibilityIdentifier("applyRecommendation")
                }
                .opacity(suggestionFading ? 0 : 1)
                .allowsHitTesting(suggestedThreshold != nil)
                .accessibilityHidden(suggestedThreshold == nil)
            }
        }
        .task(id: suggestedThreshold) { await updateDisplayedSuggestion(suggestedThreshold) }
    }

    private var footer: some View {
        HStack(spacing: 12) {
            if let directory = model.labDirectory ?? model.options.testDirectory {
                Button(l10n["footer.testFolder"], systemImage: "folder") { NSWorkspace.shared.open(directory) }
                    .buttonStyle(.borderless)
            } else if let mount = snapshot?.mount, !model.options.preview {
                Text(mount).lineLimit(1).truncationMode(.middle).foregroundStyle(.secondary)
            }
            Spacer()
            Menu {
                Toggle(l10n["login.start"], isOn: Binding(get: { model.launchAtLogin }, set: model.setLaunchAtLogin))
                    .accessibilityIdentifier("launchAtLoginToggle")
                Menu(l10n["language.title"]) {
                    Picker(l10n["language.title"], selection: $model.language) {
                        ForEach(AppLanguage.allCases) { language in
                            Text(language.label(in: l10n)).tag(language)
                        }
                    }
                    .labelsHidden()
                    .pickerStyle(.inline)
                }
                .accessibilityIdentifier("languageMenu")
            } label: {
                Label(l10n["footer.settings"], systemImage: "gearshape")
                    .labelStyle(.iconOnly)
                    .font(.system(size: 11))
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .controlSize(.small)
            .fixedSize()
            .accessibilityIdentifier("settingsMenu")
            .help(l10n["footer.settings"])
            Button(l10n["footer.quit"]) { NSApplication.shared.terminate(nil) }.buttonStyle(.borderless)
        }
        .font(.system(size: 11))
        .padding(.horizontal, inset)
        .frame(height: 36)
    }
}
