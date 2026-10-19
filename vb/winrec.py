"""The Windows recorder: what sox `rec` does on a Mac, in-process Python.

Run as a subprocess, deliberately, so the rest of voicebridge keeps the exact
contract it has with sox: a Popen it can poll and terminate, writing a 16kHz
mono 16-bit WAV that GROWS while it records. Other code depends on that last
part. The live mic meter reads the tail of the file every 150ms, and talkd
starts transcribing before the take ends by reading past the 44-byte header.

Why not sox on Windows: there is no `rec` there (it is a symlink sox creates
on POSIX), no reliable winget package, and the Windows build needs a
"-t waveaudio default" device spelling the Mac code would have to special-case
everywhere. sounddevice ships PortAudio inside its Windows wheel, so `pip
install sounddevice` is the whole dependency.

Two properties are kept on purpose:

  * The RIFF and data sizes are rewritten after every block. On Windows,
    Popen.terminate() is TerminateProcess, which runs no cleanup at all, so a
    header patched only at exit would leave every push-to-talk take (the ones
    ended by letting go of the key) with a zero-length data chunk.
  * Nothing is written until speech starts, when the caller asked for silence
    gating. That is sox's `silence 1 ...` behaviour, and callers poll the file
    size to learn whether you have started talking.

The thresholds are sox's: start after 0.05s above 0.3% of full scale, stop
after STOP_SECS below 1.5%. sox measures amplitude; this measures the RMS of
10ms windows, which is close enough for "is anybody talking" and is stated
rather than hidden.

  python winrec.py OUT.wav --max 30 [--silence 2.0] [--norm] [--from-file IN.wav]

--from-file feeds a WAV through the same gating and writing code instead of
the microphone. CI runners have no audio device, and this is how the part
that matters (gating, header, growth) is tested there.
"""

import argparse
import array
import math
import struct
import sys
import wave

RATE = 16000
WIN = RATE // 100            # 10ms windows for the level gate
FULL = 32768.0


def _header(n_bytes: int) -> bytes:
    return (b"RIFF" + struct.pack("<I", 36 + n_bytes) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, RATE, RATE * 2, 2, 16)
            + b"data" + struct.pack("<I", n_bytes))


class _Writer:
    def __init__(self, path: str):
        self.f = open(path, "wb")
        self.f.write(_header(0))
        self.f.flush()
        self.n = 0

    def write(self, pcm: bytes) -> None:
        if not pcm:
            return
        self.f.seek(0, 2)
        self.f.write(pcm)
        self.n += len(pcm)
        # Keep the header true after every block; see the module docstring.
        self.f.seek(4)
        self.f.write(struct.pack("<I", 36 + self.n))
        self.f.seek(40)
        self.f.write(struct.pack("<I", self.n))
        self.f.flush()

    def close(self) -> None:
        self.f.close()


def _rms(pcm: bytes) -> float:
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if not a:
        return 0.0
    return math.sqrt(sum(x * x for x in a) / len(a)) / FULL


def _normalise(path: str) -> None:
    """sox `norm -1`: scale so the loudest sample sits at -1 dBFS."""
    with open(path, "r+b") as f:
        f.seek(44)
        a = array.array("h")
        a.frombytes(f.read())
        peak = max((abs(x) for x in a), default=0)
        if peak < 64:
            return
        k = (FULL * 0.891) / peak
        a = array.array("h", (max(-32768, min(32767, int(x * k))) for x in a))
        f.seek(44)
        f.write(a.tobytes())


def _blocks_from_file(path: str):
    with wave.open(path, "rb") as w:
        if w.getframerate() != RATE or w.getnchannels() != 1 \
                or w.getsampwidth() != 2:
            raise SystemExit(f"winrec: {path} must be 16kHz mono 16-bit")
        while True:
            b = w.readframes(WIN * 10)
            if not b:
                return
            yield b


def _blocks_from_mic():
    import queue
    import sounddevice as sd
    q = queue.Queue()

    def cb(indata, frames, t, status):
        q.put(bytes(indata))

    with sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16",
                           blocksize=WIN * 10, callback=cb):
        while True:
            yield q.get()


def run(out: str, max_secs: float, silence: float, norm: bool,
        from_file: str = "") -> int:
    w = _Writer(out)
    started = silence <= 0          # no gating: record from the first block
    quiet = 0.0
    loud_run = 0.0
    elapsed = 0.0
    source = _blocks_from_file(from_file) if from_file else _blocks_from_mic()
    try:
        for block in source:
            secs = len(block) / 2 / RATE
            elapsed += secs
            if not started:
                # sox `silence 1 0.05 0.3%`: start once 50ms is above 0.3%.
                loud_run = loud_run + secs if _rms(block) > 0.003 else 0.0
                if loud_run >= 0.05:
                    started = True
                    w.write(block)
            else:
                w.write(block)
                if silence > 0:
                    # `... 1 STOP 1.5%`: end after STOP seconds below 1.5%.
                    quiet = quiet + secs if _rms(block) < 0.015 else 0.0
                    if quiet >= silence:
                        break
            if elapsed >= max_secs:
                break
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"winrec: {e}", file=sys.stderr)
        w.close()
        return 2
    w.close()
    if norm and w.n:
        _normalise(out)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--max", type=float, default=30.0)
    ap.add_argument("--silence", type=float, default=0.0,
                    help="seconds of quiet that end the take; 0 records "
                         "until stopped, which is push-to-talk")
    ap.add_argument("--norm", action="store_true")
    ap.add_argument("--from-file", default="")
    a = ap.parse_args(argv)
    return run(a.out, a.max, a.silence, a.norm, a.from_file)


if __name__ == "__main__":
    sys.exit(main())
