#!/usr/bin/env swift
import Cocoa
import ApplicationServices

struct ProbeError: Error {
    let message: String
}

let args = Array(CommandLine.arguments.dropFirst())

func value(after name: String) -> String? {
    guard let index = args.firstIndex(of: name), args.indices.contains(index + 1) else {
        return nil
    }
    return args[index + 1]
}

func hasFlag(_ name: String) -> Bool {
    args.contains(name)
}

func printJSON(_ object: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

func fail(_ message: String, code: Int32 = 1) -> Never {
    printJSON(["ok": false, "error": message])
    exit(code)
}

func requireAXTrusted(prompt: Bool) {
    let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: prompt] as CFDictionary
    if !AXIsProcessTrustedWithOptions(options) {
        fail("accessibility permission is not granted for this process", code: 13)
    }
}

func matchingApps(processName: String?, bundleID: String?) -> [NSRunningApplication] {
    let apps = NSWorkspace.shared.runningApplications
    return apps.filter { app in
        if let bundleID, app.bundleIdentifier == bundleID {
            return true
        }
        if let processName {
            if app.localizedName == processName {
                return true
            }
            if app.executableURL?.deletingPathExtension().lastPathComponent == processName {
                return true
            }
            if app.executableURL?.lastPathComponent == processName {
                return true
            }
        }
        return false
    }
}

func axValue<T>(_ element: AXUIElement, _ attribute: String, as type: T.Type = T.self) -> T? {
    var raw: CFTypeRef?
    let result = AXUIElementCopyAttributeValue(element, attribute as CFString, &raw)
    if result != .success {
        return nil
    }
    return raw as? T
}

func axPoint(_ element: AXUIElement, _ attribute: String) -> CGPoint? {
    var raw: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, attribute as CFString, &raw) == .success,
          let value = raw else {
        return nil
    }
    var point = CGPoint.zero
    if AXValueGetType(value as! AXValue) == .cgPoint,
       AXValueGetValue(value as! AXValue, .cgPoint, &point) {
        return point
    }
    return nil
}

func axSize(_ element: AXUIElement, _ attribute: String) -> CGSize? {
    var raw: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, attribute as CFString, &raw) == .success,
          let value = raw else {
        return nil
    }
    var size = CGSize.zero
    if AXValueGetType(value as! AXValue) == .cgSize,
       AXValueGetValue(value as! AXValue, .cgSize, &size) {
        return size
    }
    return nil
}

func setAXPoint(_ element: AXUIElement, _ attribute: String, _ point: CGPoint) -> Bool {
    var value = point
    guard let axValue = AXValueCreate(.cgPoint, &value) else {
        return false
    }
    return AXUIElementSetAttributeValue(element, attribute as CFString, axValue) == .success
}

func setAXSize(_ element: AXUIElement, _ attribute: String, _ size: CGSize) -> Bool {
    var value = size
    guard let axValue = AXValueCreate(.cgSize, &value) else {
        return false
    }
    return AXUIElementSetAttributeValue(element, attribute as CFString, axValue) == .success
}

func windows(for app: NSRunningApplication) -> [AXUIElement] {
    let appElement = AXUIElementCreateApplication(app.processIdentifier)
    return axValue(appElement, kAXWindowsAttribute as String, as: [AXUIElement].self) ?? []
}

func frontWindow(for app: NSRunningApplication) -> AXUIElement? {
    let appElement = AXUIElementCreateApplication(app.processIdentifier)
    if let front: AXUIElement = axValue(appElement, kAXFocusedWindowAttribute as String) {
        return front
    }
    return windows(for: app).first
}

func windowTitles(_ windows: [AXUIElement]) -> [String] {
    windows.compactMap { axValue($0, kAXTitleAttribute as String, as: String.self) }
}

func targetWindow(for app: NSRunningApplication, titleContains: String?, allowWindowFallback: Bool) -> AXUIElement? {
    let appWindows = windows(for: app)
    if let titleContains, !titleContains.isEmpty {
        for window in appWindows {
            let title: String? = axValue(window, kAXTitleAttribute as String)
            if title?.localizedCaseInsensitiveContains(titleContains) == true {
                return window
            }
        }
        if !allowWindowFallback {
            return nil
        }
    }
    return frontWindow(for: app) ?? appWindows.first
}

func appReady(processName: String?, bundleID: String?, titleContains: String?, allowWindowFallback: Bool) -> (NSRunningApplication, String, [String])? {
    for app in matchingApps(processName: processName, bundleID: bundleID) {
        let appWindows = windows(for: app)
        if appWindows.isEmpty {
            continue
        }
        let titles = windowTitles(appWindows)
        if let titleContains, !titleContains.isEmpty {
            if titles.contains(where: { $0.localizedCaseInsensitiveContains(titleContains) }) {
                return (app, "ax_window_title", titles)
            }
            if !allowWindowFallback {
                continue
            }
        }
        return (app, "ax_window", titles)
    }
    return nil
}

func postKey(_ virtualKey: CGKeyCode, command: Bool = false) {
    let source = CGEventSource(stateID: .hidSystemState)
    let down = CGEvent(keyboardEventSource: source, virtualKey: virtualKey, keyDown: true)!
    let up = CGEvent(keyboardEventSource: source, virtualKey: virtualKey, keyDown: false)!
    if command {
        down.flags = .maskCommand
        up.flags = .maskCommand
    }
    down.post(tap: .cghidEventTap)
    usleep(20_000)
    up.post(tap: .cghidEventTap)
    usleep(30_000)
}

func click(at point: CGPoint) {
    let source = CGEventSource(stateID: .hidSystemState)
    let down = CGEvent(mouseEventSource: source, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left)!
    let up = CGEvent(mouseEventSource: source, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left)!
    down.post(tap: .cghidEventTap)
    usleep(20_000)
    up.post(tap: .cghidEventTap)
}

func center(of window: AXUIElement) -> CGPoint? {
    guard let position = axPoint(window, kAXPositionAttribute as String),
          let size = axSize(window, kAXSizeAttribute as String) else {
        return nil
    }
    return CGPoint(x: position.x + size.width / 2.0, y: position.y + size.height / 2.0)
}

func copyPasteboardString() -> String? {
    NSPasteboard.general.string(forType: .string)
}

func setPasteboardString(_ value: String) {
    NSPasteboard.general.clearContents()
    NSPasteboard.general.setString(value, forType: .string)
}

func restorePasteboardString(_ value: String?) {
    guard let value else {
        return
    }
    setPasteboardString(value)
}

func usage() -> Never {
    fail("usage: mac_gui_probe wait-ready|set-window|type-save --process-name NAME [--bundle-id ID] [--title-contains TEXT] [--timeout SECONDS] [--text TEXT] [--window-x N --window-y N --window-width N --window-height N] [--activation-delay SECONDS] [--prompt-permissions]")
}

guard let command = args.first else {
    usage()
}

let processName = value(after: "--process-name")
let bundleID = value(after: "--bundle-id")
let titleContains = value(after: "--title-contains")
let timeout = Double(value(after: "--timeout") ?? "30") ?? 30
let activationDelay = Double(value(after: "--activation-delay") ?? "0.5") ?? 0.5
let promptPermissions = hasFlag("--prompt-permissions")
let allowWindowFallback = hasFlag("--allow-window-fallback")
let windowX = Double(value(after: "--window-x") ?? "")
let windowY = Double(value(after: "--window-y") ?? "")
let windowWidth = Double(value(after: "--window-width") ?? "")
let windowHeight = Double(value(after: "--window-height") ?? "")

if processName == nil && bundleID == nil {
    fail("either --process-name or --bundle-id is required")
}

requireAXTrusted(prompt: promptPermissions)

let started = CFAbsoluteTimeGetCurrent()
let deadline = started + timeout

switch command {
case "wait-ready":
    while CFAbsoluteTimeGetCurrent() < deadline {
        if let ready = appReady(processName: processName, bundleID: bundleID, titleContains: titleContains, allowWindowFallback: allowWindowFallback) {
            printJSON([
                "ok": true,
                "elapsed_seconds": CFAbsoluteTimeGetCurrent() - started,
                "mode": ready.1,
                "pid": ready.0.processIdentifier,
                "titles": ready.2
            ])
            exit(0)
        }
        usleep(50_000)
    }
    fail("target window did not become ready before timeout", code: 2)

case "set-window":
    guard let windowX, let windowY, let windowWidth, let windowHeight else {
        fail("--window-x, --window-y, --window-width, and --window-height are required for set-window")
    }

    var app: NSRunningApplication?
    while CFAbsoluteTimeGetCurrent() < deadline {
        if let ready = appReady(processName: processName, bundleID: bundleID, titleContains: titleContains, allowWindowFallback: allowWindowFallback) {
            app = ready.0
            break
        }
        usleep(50_000)
    }
    guard let targetApp = app,
          let win = targetWindow(for: targetApp, titleContains: titleContains, allowWindowFallback: allowWindowFallback) else {
        fail("target window did not become ready before timeout", code: 2)
    }

    targetApp.activate(options: [.activateAllWindows])
    let requestedPosition = CGPoint(x: windowX, y: windowY)
    let requestedSize = CGSize(width: windowWidth, height: windowHeight)
    let beforePosition = axPoint(win, kAXPositionAttribute as String) ?? .zero
    let beforeSize = axSize(win, kAXSizeAttribute as String) ?? .zero
    let positionOK = setAXPoint(win, kAXPositionAttribute as String, requestedPosition)
    let sizeOK = setAXSize(win, kAXSizeAttribute as String, requestedSize)
    usleep(150_000)
    let afterPosition = axPoint(win, kAXPositionAttribute as String) ?? .zero
    let afterSize = axSize(win, kAXSizeAttribute as String) ?? .zero

    printJSON([
        "ok": positionOK && sizeOK,
        "elapsed_seconds": CFAbsoluteTimeGetCurrent() - started,
        "pid": targetApp.processIdentifier,
        "before": [
            "x": beforePosition.x,
            "y": beforePosition.y,
            "width": beforeSize.width,
            "height": beforeSize.height
        ],
        "after": [
            "x": afterPosition.x,
            "y": afterPosition.y,
            "width": afterSize.width,
            "height": afterSize.height
        ],
        "requested": [
            "x": requestedPosition.x,
            "y": requestedPosition.y,
            "width": requestedSize.width,
            "height": requestedSize.height
        ]
    ])
    exit(positionOK && sizeOK ? 0 : 3)

case "type-save":
    guard let text = value(after: "--text") else {
        fail("--text is required for type-save")
    }

    var app: NSRunningApplication?
    while CFAbsoluteTimeGetCurrent() < deadline {
        if let ready = appReady(processName: processName, bundleID: bundleID, titleContains: titleContains, allowWindowFallback: allowWindowFallback) {
            app = ready.0
            break
        }
        usleep(50_000)
    }
    guard let targetApp = app else {
        fail("target window did not become ready before timeout", code: 2)
    }

    targetApp.activate(options: [.activateAllWindows])
    usleep(useconds_t(max(0.0, activationDelay) * 1_000_000))

    if let win = frontWindow(for: targetApp), let point = center(of: win) {
        click(at: point)
        usleep(100_000)
    }

    let previousPasteboard = copyPasteboardString()
    setPasteboardString(text)
    postKey(0x35) // Escape, to close transient UI.
    postKey(0x09, command: true) // Command-V
    let inputDispatchedSeconds = CFAbsoluteTimeGetCurrent() - started
    usleep(250_000)
    postKey(0x01, command: true) // Command-S
    usleep(250_000)
    restorePasteboardString(previousPasteboard)

    printJSON([
        "ok": true,
        "elapsed_seconds": CFAbsoluteTimeGetCurrent() - started,
        "input_dispatched_seconds": inputDispatchedSeconds,
        "pid": targetApp.processIdentifier
    ])
    exit(0)

default:
    usage()
}
