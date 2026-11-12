import AppKit
import Combine
import Foundation
import OSLog

struct Forecast: Decodable, Identifiable {
    var id: Int { minutes }
    let minutes: Int
    let seconds: Int
}

struct LidSession: Decodable, Identifiable {
    var id: Double { start }
    let start: Double
    let end: Double
    let before: Int?
    let after: Int?
    let onBattery: Bool
    let u3BeforeSleep: Bool?
    var sameDevice: Bool?

    var drop: Int? {
        guard let before, let after else { return nil }
        return before - after
    }
}

struct LidComparison: Decodable {
    let percentPoints: Double
    let hours: Double
}

struct ObservationSnapshot: Decodable {
    var state: String
    var message: String?
    var device: String?
    var mount: String?
    var sampleTime: Double?
    var quietSeconds: Double?
    var inU3: Bool?
    var watts: Double?
    var idleWatts: Double?
    var u3Watts: Double?
    var sleepSavingsWatts7d: Double?
    var batteryFullWh: Double?
    var energyWh24h: Double?
    var powerCoverageSeconds: Int?
    var quiet24hSeconds: Int?
    var u3Seconds: Int?
    var u3Seconds7d: Int?
    var windowCount: Int?
    var forecasts: [Forecast]?
    var recommendation: Int?
    var lids: [LidSession]?
    var lidComparison: LidComparison?

    var estimatedWattHours: Double? {
        guard let idleWatts, let u3Watts, let u3Seconds, u3Seconds > 0 else { return nil }
        return (idleWatts - u3Watts) * Double(u3Seconds) / 3600
    }

    func savingsWh(forSleepSeconds seconds: Int?) -> Double? {
        guard let seconds else { return nil }
        if seconds == 0 { return 0 }
        return sleepSavingsWatts7d.map { $0 * Double(seconds) / 3600 }
    }

    static func preview() -> ObservationSnapshot {
        let now = Date().timeIntervalSince1970
        let nights = [4, 3, 4, 2, 3, 2].enumerated().map { index, drop in
            let end = now - Double(5 - index) * 86400
            return LidSession(start: end - 8 * 3600, end: end, before: 82, after: 82 - drop,
                              onBattery: true, u3BeforeSleep: index >= 3, sameDevice: true)
        }
        let windows = [190, 360, 700, 720, 2900, 10200, 14400]
        let forecasts = (1...120).map { minutes in
            let durations = windows.map { max(0, $0 - minutes * 60 - 5) }
            return Forecast(minutes: minutes, seconds: durations.reduce(0, +))
        }
        return ObservationSnapshot(
            state: "observing",
            sampleTime: now, quietSeconds: 166, inU3: false, watts: 1.057, idleWatts: 1.04,
            u3Watts: 0.085, sleepSavingsWatts7d: 1.04 - 0.085, batteryFullWh: 50,
            energyWh24h: 5.12, powerCoverageSeconds: 28800,
            quiet24hSeconds: 18240, u3Seconds: 11340, u3Seconds7d: 18240, windowCount: windows.count,
            forecasts: forecasts, recommendation: 10, lids: nights,
            lidComparison: LidComparison(percentPoints: 2, hours: 8)
        )
    }
}

struct LaunchOptions {
    let arguments: [String]

    func value(after option: String) -> String? {
        guard let index = arguments.firstIndex(of: option), index + 1 < arguments.count else { return nil }
        return arguments[index + 1]
    }

    var preview: Bool { arguments.contains("--preview") }
    var showPanel: Bool { arguments.contains("--show") || preview }
    var showStrategy: Bool { arguments.contains("--strategy") }
    var mount: String? { value(after: "--mount") }
    var testDirectory: URL? { value(after: "--test-directory").map { URL(fileURLWithPath: $0, isDirectory: true) } }
    var labPython: String? {
        value(after: "--lab-python") ?? Bundle.main.object(forInfoDictionaryKey: "LabPythonExecutable") as? String
    }
    private var applicationSupport: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
    }
    var dataDirectory: URL {
        if let path = value(after: "--data-directory") {
            return URL(fileURLWithPath: path, isDirectory: true)
        }
        return applicationSupport.appendingPathComponent("Disk Nap/Observation", isDirectory: true)
    }

    func loadDefaults() -> UserDefaults {
        let defaults = preview ? UserDefaults(suiteName: "DiskNap.Preview")! : .standard
        let previousDomain = preview ? "MacSSDSleep.Preview" : "com.github.grasscaograss.mac-ssd-sleep.menubar"
        if let previous = defaults.persistentDomain(forName: previousDomain) {
            for key in ["language", "idleMinutes", "batteryOnly"] where defaults.object(forKey: key) == nil {
                if let value = previous[key] { defaults.set(value, forKey: key) }
            }
        }
        return defaults
    }

    func migrateSavedHistory() throws {
        guard !preview, value(after: "--data-directory") == nil else { return }
        let legacy = applicationSupport.appendingPathComponent("Mac SSD Sleep/Observation", isDirectory: true)
        try Self.copySavedHistory(from: legacy, to: dataDirectory)
    }

    static func copySavedHistory(from legacy: URL, to current: URL) throws {
        let files = FileManager.default
        guard files.fileExists(atPath: legacy.path) else { return }
        let records = try files.contentsOfDirectory(at: legacy, includingPropertiesForKeys: [.isRegularFileKey])
        try files.createDirectory(at: current, withIntermediateDirectories: true)
        for record in records where record.pathExtension == "json" {
            guard try record.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile == true else { continue }
            let target = current.appendingPathComponent(record.lastPathComponent)
            if !files.fileExists(atPath: target.path) { try files.copyItem(at: record, to: target) }
        }
    }
}

private struct LabEvent: Decodable {
    let event: String
    var path: String?
    var automatic: Bool?
    var idleSeconds: Int?
    var batteryOnly: Bool?
    var error: String?

    enum CodingKeys: String, CodingKey {
        case event, path, automatic, error
        case idleSeconds = "idle_seconds"
        case batteryOnly = "battery_only"
    }
}

@MainActor
final class ObservationModel: ObservableObject {
    @Published private(set) var snapshot: ObservationSnapshot?
    @Published private(set) var observing = false
    @Published private(set) var observationIssue: PanelIssue?
    @Published private(set) var labIssue: PanelIssue?
    @Published private(set) var labReady = false
    @Published private(set) var automatic = false
    @Published private(set) var settingPolicy = false
    @Published private(set) var labDirectory: URL?
    @Published var language: AppLanguage {
        didSet { defaults.set(language.rawValue, forKey: "language") }
    }
    @Published private(set) var threshold: Int {
        didSet { defaults.set(threshold, forKey: "idleMinutes") }
    }
    @Published private(set) var batteryOnly: Bool {
        didSet { defaults.set(batteryOnly, forKey: "batteryOnly") }
    }

    let options: LaunchOptions
    private let defaults: UserDefaults
    private let logger = Logger(subsystem: Bundle.main.bundleIdentifier ?? "DiskNap", category: "Backend")
    private var worker: Process?
    private var pipe: Pipe?
    private var buffer = Data()
    private var lab: Process?
    private var labInput: Pipe?
    private var labOutput: Pipe?
    private var labBuffer = Data()

    init(options: LaunchOptions) {
        self.options = options
        defaults = options.loadDefaults()
        language = defaults.string(forKey: "language").flatMap(AppLanguage.init(rawValue:)) ?? .system
        let stored = defaults.integer(forKey: "idleMinutes")
        threshold = (1...120).contains(stored) ? stored : 5
        batteryOnly = defaults.bool(forKey: "batteryOnly")
    }

    func startOnLaunch() {
        do {
            try options.migrateSavedHistory()
        } catch {
            logger.error("Could not copy saved observation history: \(error.localizedDescription, privacy: .private)")
        }
        start()
        if options.preview {
            labReady = true
        } else if let python = options.labPython {
            startLab(python: python)
        }
    }

    func setAutomatic(_ enabled: Bool) {
        guard labReady, !settingPolicy else { return }
        sendPolicy(enabled, threshold: threshold, batteryOnly: batteryOnly)
    }

    func setThreshold(_ minutes: Int) {
        guard !settingPolicy, minutes != threshold, (1...120).contains(minutes) else { return }
        if automatic {
            sendPolicy(true, threshold: minutes, batteryOnly: batteryOnly)
        } else {
            threshold = minutes
        }
    }

    func setBatteryOnly(_ enabled: Bool) {
        guard !settingPolicy, enabled != batteryOnly else { return }
        if automatic {
            sendPolicy(true, threshold: threshold, batteryOnly: enabled)
        } else {
            batteryOnly = enabled
        }
    }

    private func start() {
        guard worker == nil else { return }
        observationIssue = nil
        if options.preview {
            snapshot = .preview()
            observing = true
            return
        }
        guard let resources = Bundle.main.resourceURL else {
            observationIssue = .observationResources
            return
        }
        let child = Process()
        let output = Pipe()
        child.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        child.currentDirectoryURL = resources
        var args = ["-B", "-u", "-m", "disk_nap.observation",
                    "--data-directory", options.dataDirectory.path]
        if let mount = options.mount { args += ["--mount", mount] }
        child.arguments = args
        child.standardOutput = output
        child.terminationHandler = { [weak self] completed in
            DispatchQueue.main.async { self?.finished(completed) }
        }
        buffer = Data()
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if !data.isEmpty {
                DispatchQueue.main.async { self?.consume(data) }
            }
        }
        do {
            try child.run()
            worker = child
            pipe = output
            observing = true
        } catch {
            output.fileHandleForReading.readabilityHandler = nil
            observationIssue = .observationStart
            logger.error("Could not start observation: \(error.localizedDescription, privacy: .private)")
        }
    }

    private func consume(_ data: Data) {
        buffer.append(data)
        while let newline = buffer.firstIndex(of: 10) {
            let line = Data(buffer.prefix(upTo: newline))
            buffer.removeSubrange(...newline)
            guard let next = try? JSONDecoder().decode(ObservationSnapshot.self, from: line) else { continue }
            if next.state == "error" {
                observationIssue = .observationFailed
                if let message = next.message { logger.error("Observation: \(message, privacy: .private)") }
            } else {
                snapshot = next
            }
        }
    }

    private func finished(_ child: Process) {
        guard worker === child else { return }
        if child.terminationStatus != 0 && observationIssue == nil {
            observationIssue = .observationStopped
        }
        pipe?.fileHandleForReading.readabilityHandler = nil
        pipe = nil
        worker = nil
        observing = false
    }

    private func startLab(python: String) {
        guard let resources = Bundle.main.resourceURL else { return }
        let root = options.dataDirectory.appendingPathComponent("Scratch-\(UUID().uuidString)", isDirectory: true)
        let child = Process()
        let input = Pipe()
        let output = Pipe()
        do {
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            child.executableURL = URL(fileURLWithPath: python)
            child.currentDirectoryURL = resources
            child.arguments = ["-B", "-u", "-m", "disk_nap.lab", root.path,
                               "--idle-seconds", String(threshold * 60), "--controlled"]
            child.standardInput = input
            child.standardOutput = output
            child.terminationHandler = { [weak self] finished in
                DispatchQueue.main.async { self?.labFinished(finished) }
            }
            output.fileHandleForReading.readabilityHandler = { [weak self] handle in
                let data = handle.availableData
                if !data.isEmpty {
                    DispatchQueue.main.async { self?.consumeLab(data) }
                }
            }
            try child.run()
            lab = child
            labInput = input
            labOutput = output
        } catch {
            output.fileHandleForReading.readabilityHandler = nil
            labIssue = .labStart
            logger.error("Could not start the test disk: \(error.localizedDescription, privacy: .private)")
        }
    }

    private func sendPolicy(_ enabled: Bool, threshold minutes: Int, batteryOnly restricted: Bool) {
        if options.preview {
            automatic = enabled
            threshold = minutes
            batteryOnly = restricted
            return
        }
        guard let input = labInput, lab?.isRunning == true else { return }
        do {
            let data = try JSONSerialization.data(withJSONObject: [
                "automatic": enabled, "idle_seconds": minutes * 60, "battery_only": restricted,
            ])
            try input.fileHandleForWriting.write(contentsOf: data + Data([10]))
            settingPolicy = true
            labIssue = nil
        } catch {
            labIssue = .labPolicy
            logger.error("Could not update the sleep settings: \(error.localizedDescription, privacy: .private)")
        }
    }

    private func consumeLab(_ data: Data) {
        labBuffer.append(data)
        while let newline = labBuffer.firstIndex(of: 10) {
            let line = Data(labBuffer.prefix(upTo: newline))
            labBuffer.removeSubrange(...newline)
            guard let event = try? JSONDecoder().decode(LabEvent.self, from: line) else { continue }
            switch event.event {
            case "lab-ready":
                labDirectory = event.path.map { URL(fileURLWithPath: $0, isDirectory: true) }
                labReady = labDirectory != nil
            case "policy-changed":
                if let enabled = event.automatic, let seconds = event.idleSeconds, let restricted = event.batteryOnly {
                    automatic = enabled
                    threshold = seconds / 60
                    batteryOnly = restricted
                } else {
                    labIssue = .labPolicy
                }
                settingPolicy = false
            case "policy-error":
                labIssue = .labPolicy
                if let message = event.error { logger.error("Sleep settings: \(message, privacy: .private)") }
                settingPolicy = false
            default: break
            }
        }
    }

    private func labFinished(_ child: Process) {
        guard lab === child else { return }
        labOutput?.fileHandleForReading.readabilityHandler = nil
        labOutput = nil
        labInput = nil
        lab = nil
        labReady = false
        labDirectory = nil
        settingPolicy = false
        automatic = false
        if child.terminationStatus != 0 { labIssue = .labStopped }
    }

    func terminate() {
        try? labInput?.fileHandleForWriting.close()
        pipe?.fileHandleForReading.readabilityHandler = nil
        if let worker, worker.isRunning { worker.terminate() }
    }
}
