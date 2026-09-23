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

// Sized to be NOTICED. 16pt was small enough to miss entirely, which for an
// indicator whose whole job is telling you the microphone is open is the same
// as not existing. A capsule rather than a circle: it reads as a deliberate
// object at a glance, and the extra width gives the level something to move
// across instead of only in and out.
let ORB_W: CGFloat = 54        // capsule width
let ORB_H: CGFloat = 20        // capsule height
let PAD: CGFloat = 4           // breathing room inside the window
let WIN_W: CGFloat = ORB_W + PAD * 2
let WIN_H: CGFloat = ORB_H + PAD * 2

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
        return NSPoint(x: (centreX - WIN_W / 2).rounded(),
                       y: (topY - gap - WIN_H).rounded())
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
    private var bars: [CAShapeLayer] = []
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
        build()
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.accessibilityDisplayOptionsDidChangeNotification,
            object: nil, queue: .main) { [weak self] _ in
                self?.build(); self?.apply(self?.look ?? .idle, force: true)
            }
    }
    required init?(coder: NSCoder) { nil }

    private func build() {
        let r = NSRect(x: PAD, y: PAD, width: ORB_W, height: ORB_H)
        disc.path = CGPath(roundedRect: r, cornerWidth: ORB_H / 2,
                           cornerHeight: ORB_H / 2, transform: nil)
        disc.fillColor = NSColor.black.withAlphaComponent(opaqueBG ? 1.0 : 0.88).cgColor
        disc.strokeColor = NSColor.white.withAlphaComponent(0.14 + boost).cgColor
        disc.lineWidth = 0.5

        // Bars, not a dot. A row of them shows the SHAPE of what you are
        // saying, not just that something is happening, and it stays legible
        // at a glance from the corner of your eye.
        bars.forEach { $0.removeFromSuperlayer() }
        bars.removeAll()
        let n = 7
        let bw: CGFloat = 3
        let gap = (ORB_W - CGFloat(n) * bw) / CGFloat(n + 1)
        for i in 0..<n {
            let b = CAShapeLayer()
            b.fillColor = NSColor.white.withAlphaComponent(0.92).cgColor
            let x = PAD + gap + CGFloat(i) * (bw + gap)
            b.frame = NSRect(x: x, y: PAD, width: bw, height: ORB_H)
            b.path = CGPath(roundedRect: NSRect(x: 0, y: 0, width: bw, height: ORB_H),
                            cornerWidth: bw / 2, cornerHeight: bw / 2, transform: nil)
            // Scale about the middle so a bar grows both ways, like a meter.
            b.anchorPoint = CGPoint(x: 0.5, y: 0.5)
            b.position = CGPoint(x: x + bw / 2, y: PAD + ORB_H / 2)
            b.bounds = NSRect(x: 0, y: 0, width: bw, height: ORB_H)
            layer?.addSublayer(b)
            bars.append(b)
        }
        setBars(0)

        ring.path = CGPath(ellipseIn: CGRect(x: -7, y: -7, width: 14, height: 14),
                           transform: nil)
        ring.fillColor = nil
        ring.strokeColor = NSColor.white.cgColor
        ring.lineWidth = 1.5
        ring.position = CGPoint(x: bounds.midX, y: bounds.midY)
        ring.opacity = 0
    }

    /// Bars at a given level. The middle ones react most, which is what a
    /// voice actually looks like and what stops it reading as a progress bar.
    private func setBars(_ lvl: CGFloat) {
        let n = bars.count
        guard n > 0 else { return }
        let mid = CGFloat(n - 1) / 2
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        for (i, b) in bars.enumerated() {
            let d = abs(CGFloat(i) - mid) / max(mid, 1)          // 0 centre, 1 edge
            let shape = 1.0 - d * 0.55
            let h = 0.16 + 0.84 * min(1, lvl * 1.35) * shape      // fraction of height
            b.transform = CATransform3DMakeScale(1, max(0.16, h), 1)
        }
        CATransaction.commit()
    }

    func apply(_ new: Look, force: Bool = false) {
        guard new != look || force else { return }
        look = new
        bars.forEach { $0.removeAllAnimations() }
        ring.removeAllAnimations()
        stopLink()

        switch new {
        case .idle:
            // The dominant state, so it must be free. One committed animation
            // handed to the render server costs this process nothing ongoing.
            setBars(0)
            ring.opacity = 0
            bars.forEach { $0.opacity = 0.45 }
            if !reduceMotion {
                let breathe = CABasicAnimation(keyPath: "opacity")
                breathe.fromValue = 0.28
                breathe.toValue = 0.60
                breathe.duration = 1.6
                breathe.autoreverses = true
                breathe.repeatCount = .infinity
                breathe.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
                bars.forEach { $0.add(breathe, forKey: "breathe") }
            }

        case .capturing:
            bars.forEach { $0.opacity = 1 }
            ring.opacity = 0
            startLink()          // real audio each frame, so this one earns its cost

        case .working:
            setBars(0.25)
            bars.forEach { $0.opacity = 0.55 }
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
        setBars(level)
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
    private let debug = ProcessInfo.processInfo.environment["VB_ORB_DEBUG"] != nil
    private var lastDebug = ""

    /// Called directly, not from applicationDidFinishLaunching.
    ///
    /// A binary that is not inside a .app bundle does not reliably get that
    /// callback: NSApplication delivers it as part of a launch sequence a
    /// bare executable never completes. So the orb ran, stayed alive, and did
    /// nothing at all, with no error anywhere, because its entire setup lived
    /// in a method that was never called.
    func start() {
        holdOurOwnLock()

        view = OrbView(frame: NSRect(x: 0, y: 0, width: WIN_W, height: WIN_H))
        makeWindow()
        wire()
        evaluate()
    }

    private func configure() {
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

    }

    /// Once, for the life of the process.
    private func wire() {
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
    }

    /// Held for our whole life so the daemon can verify we exist before it
    /// opens the microphone. Without this, us dying silently would mean "no
    /// orb", which under our own semantics reads as "mic closed".
    private func holdOurOwnLock() {
        try? FileManager.default.createDirectory(at: STATE, withIntermediateDirectories: true)
        lock = open(STATE.appendingPathComponent("orb.lock").path, O_RDONLY | O_CREAT, 0o644)
        if lock >= 0 { flock(lock, LOCK_EX | LOCK_NB) }
    }

    /// Builds the window and nothing else. This runs again on every show, so
    /// anything else put here runs again on every show: the observers and the
    /// poll timer used to live here, and one of them called evaluate(), which
    /// called show(), which came straight back. The orb spun instead of
    /// drawing, stayed alive, and reported nothing.
    private func makeWindow() {
        win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: WIN_W, height: WIN_H),
                       styleMask: [.borderless], backing: .buffered, defer: false)
        configure()
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

    /// Show it regardless of the microphone, so "does it draw" can be tested
    /// apart from "does it decide correctly". Those failed together and looked
    /// identical: nothing on screen.
    private let forced = ProcessInfo.processInfo.environment["VB_ORB_TEST"] != nil

    private func evaluate() {
        if forced {
            view.apply(.capturing)
            view.feed(0.7)
            show()
            if debug, lastDebug != "forced" {
                lastDebug = "forced"
                let f = win.frame
                FileHandle.standardError.write(
                    "orb: FORCED on \(pickScreen()?.localizedName ?? "?") at \(f), visible=\(win.isVisible), level=\(win.level.rawValue)\n"
                        .data(using: .utf8)!)
            }
            return
        }
        let hot = presence.isHot
        let ours = weOwnTheMic()
        if debug {
            let now = "\(hot)/\(ours)"
            if now != lastDebug {
                lastDebug = now
                FileHandle.standardError.write(
                    "orb: mic hot=\(hot) ours=\(ours) -> \(hot && ours ? "SHOW" : "hide")\n"
                        .data(using: .utf8)!)
            }
        }
        guard hot, ours else { return hide() }
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
delegate.start()        // before run(), because the callback may never come
app.run()
