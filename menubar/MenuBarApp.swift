import AppKit
import Combine
import SwiftUI

@MainActor
final class MenuBarDelegate: NSObject, NSApplicationDelegate {
    private let options = LaunchOptions(arguments: CommandLine.arguments)
    private var model: ObservationModel?
    private var item: NSStatusItem?
    private var languageSubscription: AnyCancellable?
    private let popover = NSPopover()

    func applicationDidFinishLaunching(_ notification: Notification) {
        let model = ObservationModel(options: options)
        self.model = model
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        self.item = item
        languageSubscription = model.$language.sink { [weak item] language in
            let name = AppLocalization(language)["app.name"]
            let icon = NSImage(systemSymbolName: "externaldrive.fill", accessibilityDescription: name)
            icon?.isTemplate = true
            item?.button?.image = icon
            item?.button?.toolTip = name
        }
        item.button?.target = self
        item.button?.action = #selector(togglePanel)
        popover.behavior = .transient
        let host = NSHostingController(rootView: Panel(model: model))
        host.sizingOptions = [.preferredContentSize]
        popover.contentViewController = host
        model.startOnLaunch()
        if options.showPanel {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.togglePanel() }
        }
    }

    @objc private func togglePanel() {
        guard let button = item?.button else { return }
        if popover.isShown {
            popover.performClose(nil)
        } else {
            model?.refreshLoginItemStatus()
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        model?.terminate()
    }
}

@main
struct MenuBarApp {
    @MainActor
    static func main() {
        let app = NSApplication.shared
        let delegate = MenuBarDelegate()
        app.setActivationPolicy(.accessory)
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
