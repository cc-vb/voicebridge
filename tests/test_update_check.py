"""Issue #10: the "you are behind" signal that never existed.

The status line had a branch for it and nothing in the codebase ever wrote the
flag it reads, so testers ran weeks-old code and reported bugs against code we
had already changed.

The failure modes of a version check are all about being unwelcome: comparing
versions as strings so nobody is ever told, phoning home on every keystroke,
breaking a status line when the wifi drops, or nagging somebody who has opted
out.
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vb import update  # noqa: E402


def _isolate(tmp: Path):
    update.CACHE = tmp / "update_check.json"
    update.core.UPDATE_FLAG = tmp / "update_available"
    tmp.mkdir(parents=True, exist_ok=True)
    for f in (update.CACHE, update.core.UPDATE_FLAG):
        if f.exists():
            f.unlink()


def test_versions_compare_as_numbers_not_strings():
    """"2.10.0" < "2.9.0" as text, and that one comparison would tell every
    user they are current forever."""
    assert update.parse("2.10.0") > update.parse("2.9.0")
    assert update.parse("2.20.37") > update.parse("2.20.9")
    assert update.parse("3.0.0") > update.parse("2.99.99")
    assert update.parse("2.8") == update.parse("2.8.0")
    assert update.parse("") == (0, 0, 0)
    assert update.parse("v2.9.1-beta") == (2, 9, 1)


def test_being_behind_writes_the_flag_the_status_line_reads(tmp_path):
    _isolate(tmp_path)
    update.local_version = lambda: "2.0.0"
    update._fetch_latest = lambda: "2.1.0"
    info = update.check_latest(force=True)
    assert info["behind"] is True
    assert update.core.UPDATE_FLAG.exists(), "the flag was still never written"
    assert "2.0.0" in update.nudge() and "2.1.0" in update.nudge()


def test_catching_up_clears_the_flag(tmp_path):
    _isolate(tmp_path)
    update.local_version = lambda: "2.0.0"
    update._fetch_latest = lambda: "2.1.0"
    update.check_latest(force=True)
    update.local_version = lambda: "2.1.0"
    update.check_latest(force=True)
    assert not update.core.UPDATE_FLAG.exists(), "still nagging after updating"
    assert update.nudge() == ""


def test_the_answer_is_cached_so_a_status_line_is_free(tmp_path):
    _isolate(tmp_path)
    calls = []
    update.local_version = lambda: "2.0.0"
    update._fetch_latest = lambda: (calls.append(1), "2.1.0")[1]
    for _ in range(10):
        update.check_latest()
    assert len(calls) == 1, f"{len(calls)} network calls for 10 checks"


def test_no_network_never_invents_an_answer(tmp_path):
    """A failed request must not clear a real warning, nor raise one."""
    _isolate(tmp_path)
    update.local_version = lambda: "2.0.0"
    update._fetch_latest = lambda: ""
    info = update.check_latest(force=True)
    assert info["behind"] is False
    assert not update.core.UPDATE_FLAG.exists()
    assert update.nudge() == ""
    # and a known warning survives a later failure
    update._fetch_latest = lambda: "2.1.0"
    update.check_latest(force=True)
    update._fetch_latest = lambda: ""
    assert update.check_latest(force=True)["behind"] is True


def test_opting_out_silences_everything(tmp_path):
    _isolate(tmp_path)
    update.local_version = lambda: "2.0.0"
    update._fetch_latest = lambda: "2.1.0"
    for var in ("VB_NO_UPDATE_CHECK", "DISABLE_AUTOUPDATER"):
        os.environ[var] = "1"
        try:
            assert update.disabled() is True
            assert update.check_latest(force=True)["latest"] == ""
            assert update.nudge() == ""
        finally:
            del os.environ[var]


def test_the_command_matches_the_install_type():
    """Telling somebody the wrong update command is worse than saying nothing:
    a plugin cannot be git-pulled, and a clone does not use /plugin."""
    update._repo = lambda: Path("/tmp/not-a-clone-xyz")
    assert update.install_kind() == "plugin"
    assert "/plugin" in update.how_to_update()
    assert update.auto_update_hint(), "no word on why plugins go stale"
    update._repo = lambda: Path(__file__).resolve().parents[1]
    assert update.install_kind() == "clone"
    assert update.how_to_update() == "vb update"
    assert update.auto_update_hint() == ""


if __name__ == "__main__":
    import tempfile
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            if "tmp_path" in fn.__code__.co_varnames:
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            else:
                fn()
    print("ok  update check: numeric, cached, silent offline, and opt-outable")
