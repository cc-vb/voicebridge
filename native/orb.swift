// vb-orb: a 16pt indicator that tells the truth about your microphone.
//
// The promise is one sentence: it is visible whenever our microphone is open,
// and its absence means the microphone is genuinely closed. Everything below
// exists to make that a guarantee rather than a hope.
//
// THREE LAYERS, because the daemon must not be able to lie about itself:
//
//   presence   is a mic actually capturing?   CoreAudio
//              kAudioDevicePropertyDeviceIsRunningSomewhere. Needs no
//              permission, is event driven, and is the same signal behind the
//              orange dot macOS shows you. The daemon cannot fake it.
//   ownership  is it OURS?                    an flock on mic.lock. The kernel
//              drops it on process death including SIGKILL, so there is no
//              staleness window to be wrong in.
//   detail     which state, and how loud?     hud.json, trusted for appearance
//              only, never for whether to appear.
//
// Shown if and only if presence AND ownership. If the mic is hot but the
// detail is stale we draw `capturing`: we know the mic is open, we just do not
// know what it is doing, and over-reporting is the only safe direction to err.
//
// Drawn UNDER the notch, never in it. The notch is the camera housing and has
// no pixels; a window centred there reports itself visible while sitting
// behind aluminium.

import Cocoa
import CoreAudio
import QuartzCore

// ---------------------------------------------------------------- geometry

let ORB: CGFloat = 16          // the disc
let PAD: CGFloat = 3           // breathing room inside the window
let WIN: CGFloat = ORB + PAD * 2

extension NSScreen {
    /// The dead camera-housing rect, or nil on a screen without a notch.
    /// The auxiliary areas are the usable "ears"; the gap between them is the
    /// housing itself.
    var notchRect: NSRect? {
        guard let l = auxiliaryTopLeftArea, let r = auxiliaryTopRightArea,
              r.minX > l.maxX else { return nil }
        return NSRect(x: l.maxX, y: frame.maxY - safeAreaInsets.top,
                      width: r.minX - l.maxX, height: safeAreaInsets.top)
    }

    var menuBarHeight: CGFloat {
        let h = frame.maxY - visibleFrame.maxY
        return h > 0 ? h : NSStatusBar.system.thickness
    }

    /// Where the window goes: under the notch if there is one, otherwise under
    /// the middle of the menu bar. Integral, because a half-point origin makes
    /// AppKit round the frame outward and a 16pt disc renders as a 17pt smudge.
    func orbOrigin(gap: CGFloat = 4) -> NSPoint {
        let centreX: CGFloat, topY: CGFloat
        if let n = notchRect {
            centreX = n.midX
            topY = n.minY
        } else {
            centreX = frame.midX
            topY = frame.maxY - menuBarHeight
        }
        return NSPoint(x: (centreX - WIN / 2).rounded(),
                       y: (topY - gap - WIN).rounded())
    }
}

/// Never fall back to the global origin: a transparent shadowless window at
/// (0,0) sits behind the Dock and is indistinguishable from one that never
/// appeared, which is exactly the failure this whole program exists to avoid.
func pickScreen() -> NSScreen? {
    if let notched = NSScreen.screens.first(where: { $0.notchRect != nil }) { return notched }
    if let m = NSScreen.main { return m }
    let mouse = NSEvent.mouseLocation
    return NSScreen.screens.first { $0.frame.contains(mouse) } ?? NSScreen.screens.first
}

// ---------------------------------------------------------------- truth

let STATE = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent(".voicebridge")

/// Layer 1. Is any input device actually running? Event driven, no polling,
/// no permission prompt: reading device STATE is not reading audio DATA.
final class MicPresence {
    private var device = AudioDeviceID(0)
    private var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyDeviceIsRunningSomewhere,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    private var block: AudioObjectPropertyListenerBlock?
    private let onChange: () -> Void

    init(onChange: @escaping () -> Void) {
        self.onChange = onChange
        attach()
        // The default input can change under us when a headset connects, which
        // leaves a listener bound to a device nobody is using. That is the
        // easiest way to end up with a silently wrong orb, so watch for it.
        var d = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        AudioObjectAddPropertyListenerBlock(
            AudioObjectID(kAudioObjectSystemObject), &d, DispatchQueue.main) { [weak self] _, _ in
                self?.reattach()
            }
    }

    private func defaultInput() -> AudioDeviceID {
        var id = AudioDeviceID(0)
        var sz = UInt32(MemoryLayout<AudioDeviceID>.size)
        var a = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject),
                                   &a, 0, nil, &sz, &id)
        return id
    }

    private func attach() {
        device = defaultInput()
        guard device != 0 else { return }
        let b: AudioObjectPropertyListenerBlock = { [weak self] _, _ in self?.onChange() }
        block = b
        AudioObjectAddPropertyListenerBlock(device, &addr, DispatchQueue.main, b)
    }

    private func reattach() {
        if device != 0, let b = block {
            AudioObjectRemovePropertyListenerBlock(device, &addr, DispatchQueue.main, b)
        }
        attach()
        onChange()
    }

    /// True while something, anyone, is capturing from the default input.
    var isHot: Bool {
        guard device != 0 else { return false }
        var v = UInt32(0)
        var sz = UInt32(MemoryLayout<UInt32>.size)
        let err = AudioObjectGetPropertyData(device, &addr, 0, nil, &sz, &v)
        return err == noErr && v != 0
    }
}

/// Layer 2. Is the open mic ours? Acquiring the lock proves nobody holds it.
func weOwnTheMic() -> Bool {
    let path = STATE.appendingPathComponent("mic.lock").path
    let fd = open(path, O_RDONLY | O_CREAT, 0o644)
    guard fd >= 0 else { return false }
    defer { close(fd) }
    if flock(fd, LOCK_EX | LOCK_NB) == 0 {
        flock(fd, LOCK_UN)
        return false            // we took it, so the daemon does not hold it
    }
    return errno == EWOULDBLOCK  // still held: the daemon is alive and capturing
}

/// Layer 3. Appearance only. Never consulted about whether to appear.
struct Detail { var phase = "capturing"; var level: CGFloat = 0; var fresh = false }

func readDetail() -> Detail {
    var d = Detail()
    guard let raw = try? Data(contentsOf: STATE.appendingPathComponent("hud.json")),
          let j = (try? JSONSerialization.jsonObject(with: raw)) as? [String: Any]
    else { return d }
    let ts = (j["ts"] as? Double) ?? 0
    d.fresh = Date().timeIntervalSince1970 - ts < 1.0
    d.level = CGFloat((j["level"] as? Double) ?? 0)
    if let p = j["phase"] as? String { d.phase = p }
    return d
}

// ---------------------------------------------------------------- the view

enum Look { case idle, capturing, working }

final class OrbView: NSView {
    private let disc = CAShapeLayer()
    private let core = CAShapeLayer()
    private let ring = CAShapeLayer()
    private var link: CADisplayLink?
    private var level: CGFloat = 0          // smoothed
    private var target: CGFloat = 0
    private(set) var look: Look = .idle

    private var reduceMotion: Bool {
        NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
    }
    private var opaqueBG: Bool {
        NSWorkspace.shared.accessibilityDisplayShouldReduceTransparency
    }
    private var boost: CGFloat {
        NSWorkspace.shared.accessibilityDisplayShouldIncreaseContrast ? 0.15 : 0
    }

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.addSublayer(disc)
        layer?.addSublayer(ring)
        layer?.addSublayer(core)
        build()
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.accessibilityDisplayOptionsDidChangeNotification,
            object: nil, queue: .main) { [weak self] _ in
                self?.build(); self?.apply(self?.look ?? .idle, force: true)
            }
    }
    required init?(coder: NSCoder) { nil }

    private func build() {
        let c = CGPoint(x: bounds.midX, y: bounds.midY)

        disc.path = CGPath(ellipseIn: CGRect(x: c.x - ORB / 2, y: c.y - ORB / 2,
                                             width: ORB, height: ORB), transform: nil)
        disc.fillColor = NSColor.black.withAlphaComponent(opaqueBG ? 1.0 : 0.92).cgColor
        // A hairline edge so the disc still reads against a black window.
        disc.strokeColor = NSColor.white.withAlphaComponent(0.12 + boost * 2).cgColor
        disc.lineWidth = 0.5

        core.fillColor = NSColor.white.cgColor
        core.position = c

        ring.path = CGPath(ellipseIn: CGRect(x: -6, y: -6, width: 12, height: 12), transform: nil)
        ring.fillColor = nil
        ring.strokeColor = NSColor.white.cgColor
        ring.lineWidth = 1
        ring.position = c
        ring.opacity = 0
    }

    /// The pupil, drawn at a given radius. Everything the eye reads is here.
    private func setCore(_ r: CGFloat) {
        core.path = CGPath(ellipseIn: CGRect(x: -r, y: -r, width: r * 2, height: r * 2),
                           transform: nil)
    }

    func apply(_ new: Look, force: Bool = false) {
        guard new != look || force else { return }
        look = new
        core.removeAllAnimations()
        ring.removeAllAnimations()
        stopLink()

        switch new {
        case .idle:
            // The dominant state, so it must be free. One committed animation
            // handed to the render server costs this process nothing ongoing.
            setCore(2.0)
            ring.opacity = 0
            if reduceMotion {
                core.opacity = 0.45
            } else {
                core.opacity = 0.45
                let breathe = CABasicAnimation(keyPath: "opacity")
                breathe.fromValue = 0.30
                breathe.toValue = 0.65
                breathe.duration = 1.6
                breathe.autoreverses = true
                breathe.repeatCount = .infinity
                breathe.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
                core.add(breathe, forKey: "breathe")
            }

        case .capturing:
            core.opacity = 1
            ring.opacity = 0
            startLink()          // real audio each frame, so this one earns its cost

        case .working:
            setCore(2.2)
            core.opacity = 0.7
            ring.opacity = Float(0.8 + boost)
            if reduceMotion {
                // A full static ring still separates this from the other two.
                ring.strokeEnd = 1
            } else {
                ring.strokeEnd = 0.17          // a 60 degree arc
                let spin = CABasicAnimation(keyPath: "transform.rotation.z")
                spin.fromValue = 0
                spin.toValue = -Double.pi * 2
                spin.duration = 1.1
                spin.repeatCount = .infinity
                ring.add(spin, forKey: "spin")
            }
        }
    }

    func feed(_ v: CGFloat) { target = max(0, min(1, v)) }

    private func startLink() {
        guard link == nil else { return }
        let l = displayLink(target: self, selector: #selector(step))
        // A range, not a fixed rate: the system picks the efficient one, and
        // 30fps is ample for a level meter whose release envelope is 180ms.
        l.preferredFrameRateRange = CAFrameRateRange(minimum: 20, maximum: 30, preferred: 30)
        l.add(to: .main, forMode: .common)
        link = l
    }
    private func stopLink() { link?.invalidate(); link = nil }

    @objc private func step() {
        // Fast attack so it feels instant, slow release so it does not strobe.
        let coeff: CGFloat = target > level ? 0.55 : 0.12
        level += (target - level) * coeff
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        setCore(2.5 + 4.5 * level)
        CATransaction.commit()
    }

    func pause() { stopLink() }
    func resume() { if look == .capturing { startLink() } }
}

// ---------------------------------------------------------------- the app

final class App: NSObject, NSApplicationDelegate {
    private var win: NSWindow!
    private var view: OrbView!
    private var presence: MicPresence!
    private var poll: Timer?
    private var lock: Int32 = -1

    func applicationDidFinishLaunching(_ n: Notification) {
        holdOurOwnLock()

        view = OrbView(frame: NSRect(x: 0, y: 0, width: WIN, height: WIN))
        makeWindow()
        buildRest()
    }

    private func buildRest() {
        win.isOpaque = false
        win.backgroundColor = .clear
        win.hasShadow = false
        win.level = NSWindow.Level(rawValue: NSWindow.Level.statusBar.rawValue + 3)
        win.ignoresMouseEvents = true
        win.collectionBehavior = [.canJoinAllSpaces, .stationary,
                                  .fullScreenAuxiliary, .ignoresCycle]
        win.isReleasedWhenClosed = false
        win.animationBehavior = .none
        // Opt-in: keep the orb off a shared or recorded stream while leaving it
        // fully visible to the person at the machine. The alternative people
        // reach for is a "hide the orb" setting, and the moment that exists
        // absence stops meaning anything and the whole design is worthless.
        if ProcessInfo.processInfo.environment["VB_ORB_PRIVATE"] != nil {
            win.sharingType = .none
        }
        win.contentView = view
        place()

        presence = MicPresence { [weak self] in self?.evaluate() }

        let nc = NSWorkspace.shared.notificationCenter
        // Rebuild rather than resume across sleep: the default input can change
        // while we are out, which leaves every cached handle stale.
        nc.addObserver(forName: NSWorkspace.didWakeNotification, object: nil,
                       queue: .main) { [weak self] _ in self?.place(); self?.evaluate() }
        nc.addObserver(forName: NSWorkspace.sessionDidResignActiveNotification, object: nil,
                       queue: .main) { [weak self] _ in self?.hide() }
        nc.addObserver(forName: NSWorkspace.sessionDidBecomeActiveNotification, object: nil,
                       queue: .main) { [weak self] _ in self?.evaluate() }
        NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification, object: nil,
            queue: .main) { [weak self] _ in self?.place() }
        NotificationCenter.default.addObserver(
            forName: NSWindow.didChangeOcclusionStateNotification, object: win,
            queue: .main) { [weak self] _ in
                guard let s = self else { return }
                // Pause the drawing, never the state machine: it must be right
                // the instant it becomes visible again.
                s.win.occlusionState.contains(.visible) ? s.view.resume() : s.view.pause()
            }

        // The level and phase still need reading; the expensive question (is a
        // mic open at all) is answered by the event-driven listener above.
        poll = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            self?.evaluate()
        }
        evaluate()
    }

    /// Held for our whole life so the daemon can verify we exist before it
    /// opens the microphone. Without this, us dying silently would mean "no
    /// orb", which under our own semantics reads as "mic closed".
    private func holdOurOwnLock() {
        try? FileManager.default.createDirectory(at: STATE, withIntermediateDirectories: true)
        lock = open(STATE.appendingPathComponent("orb.lock").path, O_RDONLY | O_CREAT, 0o644)
        if lock >= 0 { flock(lock, LOCK_EX | LOCK_NB) }
    }

    private func makeWindow() {
        win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: WIN, height: WIN),
                       styleMask: [.borderless], backing: .buffered, defer: false)
        buildRest()
    }

    private func place() {
        guard let s = pickScreen() else { return }
        win.setFrameOrigin(s.orbOrigin())
    }

    /// Rebuild rather than reuse. A window kept across a sleep/wake cycle can
    /// silently stop rendering, and from then on orderFrontRegardless() is a
    /// no-op that still reports success: the object exists, every API you would
    /// query looks healthy, and nothing is drawn. That is precisely the silent
    /// lie this program exists to prevent, and no amount of state-machine
    /// correctness catches it. Construction is sub-millisecond, so rebuilding
    /// on each open is cheap insurance against the one failure we cannot see.
    private func show() {
        if win.isVisible { return }
        let look = view.look
        makeWindow()
        view.apply(look, force: true)
        win.orderFrontRegardless()
    }
    private func hide() {
        if win.isVisible { win.orderOut(nil) }
    }

    private func evaluate() {
        guard presence.isHot, weOwnTheMic() else { return hide() }
        let d = readDetail()
        // Hot but stale: we know for certain the mic is open and not what it is
        // doing, so claim the most-open state. Over-report, never under-report.
        let phase = d.fresh ? d.phase : "capturing"
        switch phase {
        case "hearing", "capturing":
            view.apply(.capturing); view.feed(d.level)
        case "thinking", "working":
            view.apply(.working)
        default:
            view.apply(.idle)
        }
        show()
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = App()
app.delegate = delegate
app.run()
