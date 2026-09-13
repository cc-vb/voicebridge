"""Reading the last twelve records without reading the whole file.

_last_records used to be `read_text().splitlines()[-n:]`: to get twelve
records it read the entire transcript into one string and then built a list
holding every line in it as its own object. The biggest transcript on this
machine is 83MB, and roster() runs over every recent one, so a single fleet
read allocated about 280MB and did it again every few seconds.

The memory was never handed back. macOS malloc keeps large freed blocks, so
RSS climbed and stayed: vmmap showed MALLOC_LARGE (empty) holding 1.8GB
resident with 1.5GB swapped, on a 16GB laptop. tracemalloc showed nothing,
because Python had genuinely freed it all.

The cost was not really the memory. The machine is fanless, so a swapping busy
laptop throttles, and the local model's generation falls by nearly half.
Reading twelve lines badly was making the model slow.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vb import sessions  # noqa: E402

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)
    print(f"{'ok  ' if cond else 'FAIL'} {name}")


def old_way(path, n=12):
    """Exactly what it used to do, as the thing to match."""
    lines = Path(path).read_text(errors="ignore").splitlines()[-n:]
    out = []
    for ln in lines:
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def write(path, records):
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


d = tempfile.mkdtemp()

# ---- small file: read whole, unchanged behaviour ---------------------------
small = os.path.join(d, "small.jsonl")
write(small, [{"type": "user", "i": i} for i in range(5)])
check("a short file gives every record",
      sessions._last_records(small) == old_way(small))
check("and that is all five", len(sessions._last_records(small)) == 5)

# ---- exactly the window boundary -------------------------------------------
for size in (sessions._TAIL_BYTES - 1, sessions._TAIL_BYTES,
             sessions._TAIL_BYTES + 1):
    p = os.path.join(d, f"b{size}.jsonl")
    rec = {"type": "assistant", "pad": "x" * 200}
    n = size // 220 + 2
    write(p, [dict(rec, i=i) for i in range(n)])
    check(f"at ~{size} bytes the tail matches a whole-file read",
          sessions._last_records(p) == old_way(p))

# ---- big file: the case this exists for ------------------------------------
big = os.path.join(d, "big.jsonl")
write(big, [{"type": "assistant", "i": i, "pad": "y" * 400}
            for i in range(60000)])
mb = os.path.getsize(big) / 1048576
check(f"the big file is actually big ({mb:.0f} MB)", mb > 20)
check("a big file gives the same last records as reading all of it",
      sessions._last_records(big) == old_way(big))
check("and the very last record is the last one written",
      sessions._last_records(big)[-1]["i"] == 59999)

# ---- a half-line at the window edge must not poison the result -------------
check("a record split by the window is dropped, not half-parsed",
      all(isinstance(r, dict) and "i" in r
          for r in sessions._last_records(big)))

# ---- and it must not read the whole thing ----------------------------------
# The property, stated as code: bytes read must not scale with file size.
reads = []
real_open = sessions.open if hasattr(sessions, "open") else open
import builtins  # noqa: E402

orig = builtins.open


def spy(path, *a, **k):
    fh = orig(path, *a, **k)
    if str(path).endswith("big.jsonl"):
        real_read = fh.read

        def counted(*ra, **rk):
            data = real_read(*ra, **rk)
            reads.append(len(data))
            return data
        fh.read = counted
    return fh


builtins.open = spy
try:
    sessions._last_records(big)
finally:
    builtins.open = orig

check(f"it reads a window, not the file ({sum(reads)/1048576:.2f} MB "
      f"of {mb:.0f} MB)",
      sum(reads) <= sessions._TAIL_BYTES + 4096)

# ---- things that are not files ---------------------------------------------
check("a missing file is empty, not an error",
      sessions._last_records(os.path.join(d, "nope.jsonl")) == [])
empty = os.path.join(d, "empty.jsonl")
open(empty, "w").close()
check("an empty file is empty", sessions._last_records(empty) == [])

print()
if fails:
    print(f"{len(fails)} FAILED: " + ", ".join(fails))
    sys.exit(1)
print("all good")
