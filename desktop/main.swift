import AppKit

@main
struct ServerPilotDesktopMain {
    @MainActor
    static func main() {
        let application = NSApplication.shared
        application.setActivationPolicy(.regular)
        let delegate = DesktopAppDelegate()
        application.delegate = delegate
        application.run()
    }
}
