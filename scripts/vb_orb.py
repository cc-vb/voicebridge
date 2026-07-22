#!/usr/bin/env python3
"""voicebridge orb: a small always-on-top widget that tells you, at a glance,
whether voice is listening RIGHT NOW, so you never talk to a dead mic.

It reads the daemon's live state (vb/core.py: read_hud) ~20x a second and
draws a circle that pulses with your voice. Colors map to state:

    listening  soft blue, gentle breathing      -> mic open, go ahead
    hearing    bright green, grows with level    -> it's catching your voice
    thinking   amber                             -> transcribing / pasting
    speaking   violet, pulsing                   -> a reply is playing
    wake       dim slate dot                     -> waiting for "hey Claude"
    away/off   grey/red, still                   -> NOT listening

Drag it anywhere. Double-click (or Esc) to close. No extra install: Tkinter
ships with Python.
"""
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import core  # noqa: E402

try:
    import tkinter as tk
except Exception:
    sys.stderr.write("The orb needs Tkinter (ships with python3). "
                     "Try: brew install python-tk\n")
    sys.exit(1)

BG = "#0e1116"
SIZE = 168
CENTER = SIZE / 2

# phase -> (label, core color, glow color)
STYLE = {
    "listening": ("LISTENING", "#3b82f6", "#1e3a5f"),
    "hearing":   ("HEARING",   "#22c55e", "#14532d"),
    "thinking":  ("THINKING",  "#f59e0b", "#5a3d0a"),
    "speaking":  ("SPEAKING",  "#a855f7", "#3b1a5c"),
    "wake":      ("WAKE WORD", "#64748b", "#1f2937"),
    "away":      ("NOT FOCUSED", "#6b7280", "#1f2937"),
    "off":       ("OFF",       "#ef4444", "#3f1414"),
}


def _blend(c1: str, c2: str, t: float) -> str:
    a = tuple(int(c1[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(c2[i:i + 2], 16) for i in (1, 3, 5))
    m = tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return f"#{m[0]:02x}{m[1]:02x}{m[2]:02x}"


class Orb:
    def __init__(self, root: "tk.Tk"):
        self.root = root
        root.title("voicebridge")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", 0.94)
        except tk.TclError:
            pass
        sw = root.winfo_screenwidth()
        root.geometry(f"{SIZE}x{SIZE}+{sw - SIZE - 28}+56")
        self.c = tk.Canvas(root, width=SIZE, height=SIZE, bg=BG,
                           highlightthickness=0)
        self.c.pack()
        self.level = 0.0     # smoothed mic level
        self.t0 = time.time()
        # drag to move
        self._drag = (0, 0)
        for w in (root, self.c):
            w.bind("<Button-1>", self._press)
            w.bind("<B1-Motion>", self._move)
            w.bind("<Double-Button-1>", lambda e: root.destroy())
        root.bind("<Escape>", lambda e: root.destroy())
        self.tick()

    def _press(self, e):
        self._drag = (e.x_root - self.root.winfo_x(),
                      e.y_root - self.root.winfo_y())

    def _move(self, e):
        self.root.geometry(f"+{e.x_root - self._drag[0]}"
                           f"+{e.y_root - self._drag[1]}")

    def tick(self):
        h = core.read_hud()
        phase = h.get("phase", "off")
        if phase not in STYLE:
            phase = "off"
        label, col, glow = STYLE[phase]
        target = float(h.get("level", 0.0))
        # smooth the meter so it glides instead of jittering
        self.level += (target - self.level) * 0.35
        t = time.time() - self.t0

        # breathing baseline + a bit of live level; "hearing"/"speaking" pulse
        breathe = 0.5 + 0.5 * math.sin(t * 2.0)
        if phase == "hearing":
            amp = 0.35 + 0.65 * self.level
        elif phase == "speaking":
            amp = 0.5 + 0.35 * (0.5 + 0.5 * math.sin(t * 6.0))
        elif phase == "listening":
            amp = 0.32 + 0.14 * breathe
        elif phase == "thinking":
            amp = 0.4 + 0.18 * (0.5 + 0.5 * math.sin(t * 8.0))
        else:  # wake / away / off: quiet, static-ish
            amp = 0.22 + (0.05 * breathe if phase == "wake" else 0.0)

        self.c.delete("all")
        rmax = SIZE * 0.40
        r = 12 + amp * rmax
        # glow rings
        for i, gt in enumerate((0.75, 0.5, 0.28)):
            rr = r * (1 + 0.22 * (i + 1))
            self.c.create_oval(CENTER - rr, CENTER - rr, CENTER + rr,
                               CENTER + rr, outline="",
                               fill=_blend(BG, glow, gt))
        # core disc
        self.c.create_oval(CENTER - r, CENTER - r, CENTER + r, CENTER + r,
                           outline="", fill=col)
        # a soft inner highlight
        hr = r * 0.55
        self.c.create_oval(CENTER - hr, CENTER - hr * 1.3, CENTER + hr,
                           CENTER + hr * 0.4, outline="",
                           fill=_blend(col, "#ffffff", 0.28))
        self.c.create_text(CENTER, SIZE - 16, text=label, fill="#e5e7eb",
                           font=("Helvetica", 11, "bold"))
        self.root.after(50, self.tick)


def main():
    if os.environ.get("VB_NO_ORB"):
        return 0
    root = tk.Tk()
    Orb(root)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
