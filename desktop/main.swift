import AppKit

let application = NSApplication.shared
application.setActivationPolicy(.regular)
let delegate = DesktopAppDelegate()
application.delegate = delegate
application.run()
