// vb-hotkey: a single-key hold-to-talk listener for macOS.
//
// Prints one line per gesture on stdout, so any language can consume it:
//
//   READY <key>          the listener is armed
//   DOWN                 the talk key went down; start capturing now
//   UP <held_ms>         the talk key came up; stop and transcribe
//   CANCEL <held_ms>     another key joined, so this was a chord, not talking
//   LOCKED               the screen locked while held; treat as CANCEL
//   BYE                  exiting; any open hold has already been closed
//
// Design notes, each of which is load-bearing:
//
//  * HOLD ONLY, no tap gesture. macOS fires the Globe key's own action (emoji
//    picker, input switch) only on the release of a CLEAN TAP, and ignores a
//    hold. So a hold-only design collides with nothing and needs no change to
//    System Settings. A tap gesture would fight the OS.
//
//  * LISTEN ONLY, never a suppressing tap. We never need to swallow the key,
//    and a slow suppressing callback stalls keyboard input for the whole
//    machine until it returns. Not a risk worth taking for no benefit.
//
//  * We match on KEYCODE, never on the fn flag alone. Arrow keys, Home, End,
//    PageUp/PageDown and the F-keys all set the fn flag, so flag-only matching
//    starts a recording every time you press an arrow. Measured, not theoretical.
//
//  * The gesture is decided on key-UP, but DOWN is emitted immediately so the
//    caller can warm the microphone. That is what reconciles "decide late for
//    correctness" with "never lose the first syllable".

import Cocoa
import CoreGraphics

// ---------------------------------------------------------------- options

struct Options {
    var keyCode: Int64 = 63          // kVK_Function (Fn / Globe)
    var keyName  = "fn"
    var minHoldMs: Double = 120      // shorter than this is a mis-press
}

func parseArgs() -> Options {
    var o = Options()
    var it = CommandLine.arguments.dropFirst().makeIterator()
    while let a = it.next() {
        switch a {
        case "--key":
            let v = it.next() ?? "fn"
            switch v {
            case "fn", "globe":       o.keyCode = 63; o.keyName = "fn"
            case "rightcmd":          o.keyCode = 54; o.keyName = "rightcmd"
            case "rightopt":          o.keyCode = 61; o.keyName = "rightopt"
            case "leftcmd":           o.keyCode = 55; o.keyName = "leftcmd"
            default:
                FileHandle.standardError.write("unknown --key \(v)\n".data(using: .utf8)!)
                exit(2)
            }
        case "--min-hold":
            o.minHoldMs = Double(it.next() ?? "120") ?? 120
        case "--help", "-h":
            print("""
            vb-hotkey [--key fn|rightcmd|rightopt|leftcmd] [--min-hold MS]

            Emits DOWN / UP <ms> / CANCEL <ms> on stdout for a hold-to-talk key.
            Requires Accessibility (or Input Monitoring) for the running process.
            """)
            exit(0)
        default: break
        }
    }
    return o
}

let opts = parseArgs()

// ---------------------------------------------------------------- output

// Line-buffered and flushed, because the consumer reads us as a pipe.
func emit(_ s: String) {
    print(s)
    fflush(stdout)
}

// ---------------------------------------------------------------- state

final class Gesture {
    private var downAt: Date? = nil
    private var interrupted = false

    var isHeld: Bool { downAt != nil }

    func down() {
        guard downAt == nil else { return }   // ignore auto-repeat
        downAt = Date()
        interrupted = false
        emit("DOWN")
    }

    /// Another key arrived while we were held, so the user was typing a chord
    /// (fn+left is Home, fn+F1 is brightness). Not talking.
    func interrupt() {
        guard downAt != nil, !interrupted else { return }
        interrupted = true
    }

    func up(reason: String = "UP") {
        guard let d = downAt else { return }
        downAt = nil
        let ms = Date().timeIntervalSince(d) * 1000
        if interrupted {
            emit(String(format: "CANCEL %.0f", ms))
        } else if ms < opts.minHoldMs && reason == "UP" {
            // Too short to be speech. Emitted so the caller can discard the
            // warmed buffer rather than transcribe a click.
            emit(String(format: "CANCEL %.0f", ms))
        } else {
            emit("\(reason) \(Int(ms.rounded()))")
        }
    }

    /// Close any open hold. Used on exit and on screen lock, so the caller can
    /// never be left believing the mic is still held.
    func closeIfHeld(reason: String) {
        if isHeld { up(reason: reason) }
    }
}

let gesture = Gesture()

// ---------------------------------------------------------------- guards

/// Refuse to act at the login window, on a locked screen, or in a switched-out
/// user session. Otherwise a held key at lock time leaves a hot microphone.
func sessionUsable() -> Bool {
    guard let d = CGSessionCopyCurrentDictionary() as? [String: Any] else { return false }
    let onConsole = d["kCGSSessionOnConsoleKey"] as? Bool ?? false
    let loginDone = d["kCGSessionLoginDoneKey"] as? Bool ?? true
    let locked    = d["CGSSessionScreenIsLocked"] as? Bool ?? false
    return onConsole && loginDone && !locked
}

// ---------------------------------------------------------------- the tap

let FN_MASK = CGEventFlags.maskSecondaryFn.rawValue

/// Is our talk key currently down, according to this event's flags?
func keyIsDown(_ event: CGEvent) -> Bool {
    let f = event.flags
    switch opts.keyCode {
    case 63: return f.contains(.maskSecondaryFn)
    case 54, 55: return f.contains(.maskCommand)
    case 61: return f.contains(.maskAlternate)
    default: return false
    }
}

var tapRef: CFMachPort? = nil
var signalSources: [DispatchSourceSignal] = []

let callback: CGEventTapCallBack = { _, type, event, _ in
    // The system disables a tap that is slow or that sees certain input. It
    // stays dead until explicitly re-enabled, which is a stuck-hot-mic bug if
    // you miss it.
    if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
        gesture.closeIfHeld(reason: "UP")
        if let t = tapRef { CGEvent.tapEnable(tap: t, enable: true) }
        return Unmanaged.passUnretained(event)
    }

    if !sessionUsable() {
        gesture.closeIfHeld(reason: "LOCKED")
        return Unmanaged.passUnretained(event)
    }

    let kc = event.getIntegerValueField(.keyboardEventKeycode)

    if type == .flagsChanged {
        if kc == opts.keyCode {
            keyIsDown(event) ? gesture.down() : gesture.up()
        } else if gesture.isHeld {
            // A different modifier joined the hold: cmd, shift, ctrl. Chord.
            gesture.interrupt()
        }
        return Unmanaged.passUnretained(event)
    }

    // Any ordinary key pressed while held means the user is typing, not talking.
    if type == .keyDown && gesture.isHeld {
        gesture.interrupt()
    }
    return Unmanaged.passUnretained(event)
}

let mask = (1 << CGEventType.flagsChanged.rawValue)
         | (1 << CGEventType.keyDown.rawValue)
         | (1 << CGEventType.tapDisabledByTimeout.rawValue)
         | (1 << CGEventType.tapDisabledByUserInput.rawValue)

guard let tap = CGEvent.tapCreate(tap: .cgSessionEventTap,
                                  place: .headInsertEventTap,
                                  options: .listenOnly,
                                  eventsOfInterest: CGEventMask(mask),
                                  callback: callback,
                                  userInfo: nil) else {
    FileHandle.standardError.write("""
    vb-hotkey: could not create an event tap.

    Grant the app running this process Accessibility permission:
      System Settings > Privacy & Security > Accessibility

    Note macOS only honours a fresh grant for taps created by a NEW process,
    so restart this after granting.

    """.data(using: .utf8)!)
    exit(1)
}
tapRef = tap

CFRunLoopAddSource(CFRunLoopGetCurrent(),
                   CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0),
                   .commonModes)
CGEvent.tapEnable(tap: tap, enable: true)

// The disable callback is not reliable; Ghostty shipped without a watchdog and
// its hotkey died after every sleep/wake cycle.
Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { _ in
    guard let t = tapRef else { return }
    if !CGEvent.tapIsEnabled(tap: t) {
        gesture.closeIfHeld(reason: "UP")
        CGEvent.tapEnable(tap: t, enable: true)
    }
}

// Never exit leaving the caller believing the key is still held.
for sig in [SIGINT, SIGTERM, SIGHUP] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler {
        gesture.closeIfHeld(reason: "UP")
        emit("BYE")
        exit(0)
    }
    src.resume()
    signalSources.append(src)
}

emit("READY \(opts.keyName)")
CFRunLoopRun()
