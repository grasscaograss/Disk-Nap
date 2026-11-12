import Foundation

enum AppLanguage: String, CaseIterable, Identifiable {
    case system
    case english = "en"
    case simplifiedChinese = "zh-Hans"

    var id: Self { self }

    func label(in localization: AppLocalization) -> String {
        switch self {
        case .system: return localization["language.system"]
        case .english: return "English"
        case .simplifiedChinese: return "简体中文"
        }
    }
}

struct AppLocalization {
    private let bundle: Bundle
    let locale: Locale

    init(_ language: AppLanguage) {
        let identifier: String
        if language == .system {
            identifier = Bundle.main.preferredLocalizations.first ?? "en"
            locale = .current
        } else {
            identifier = language.rawValue
            locale = Locale(identifier: identifier)
        }
        bundle = Bundle.main.url(forResource: identifier, withExtension: "lproj")
            .flatMap(Bundle.init(url:)) ?? .main
    }

    subscript(_ key: String) -> String {
        bundle.localizedString(forKey: key, value: nil, table: nil)
    }

    func format(_ key: String, _ arguments: CVarArg...) -> String {
        String(format: self[key], locale: locale, arguments: arguments)
    }

    func duration(_ seconds: Int?, compact: Bool = false) -> String {
        guard let seconds else { return "—" }
        let units: Set<Duration.UnitsFormatStyle.Unit>
        switch seconds {
        case 3600...: units = [.hours, .minutes]
        case 60...: units = [.minutes, .seconds]
        default: units = [.seconds]
        }
        return Duration.seconds(seconds).formatted(
            .units(allowed: units, width: compact ? .narrow : .abbreviated).locale(locale)
        )
    }

    func minutes(_ count: Int, abbreviated: Bool = false) -> String {
        Duration.seconds(count * 60).formatted(
            .units(allowed: [.minutes], width: abbreviated ? .abbreviated : .wide).locale(locale)
        )
    }

    func percentagePoints(_ count: Int) -> String {
        format(count == 1 ? "lid.point.one" : "lid.point.other", count)
    }
}

enum PanelIssue: String {
    case observationResources = "error.observation.resources"
    case observationStart = "error.observation.start"
    case observationFailed = "error.observation.failed"
    case observationStopped = "error.observation.stopped"
    case labStart = "error.lab.start"
    case labPolicy = "error.lab.policy"
    case labStopped = "error.lab.stopped"
}
