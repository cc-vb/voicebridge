"""Can a turn actually reach the session right now, and if not, why.

A phone can hold a perfect connection to a healthy relay and still have
nowhere to put your words: injection is a clipboard paste into the frontmost
Mac app, so a locked screen or a stray Slack window is enough to stop it.
That used to surface one turn too late, as an apology after you had already
spoken, and until then the phone showed a spinner that meant any of four
different things.

So the contract this covers is that the reason is always specific and always
honest: never "ok" when a paste would refuse, never the word "locked" unless
we actually saw a locked screen, and never a claim about a frontmost app we
could not read. Fail closed, and say which thing is in the way.

Run: python3 tests/test_readiness.py   (no pytest needed)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import call, inject, oslayer  # noqa: E402


class _Stub:
    """Swap the three probes readiness() leans on, and put them back."""

    def __init__(self, front="Ghostty", bound="Ghostty", locked=False,
                 session="/tmp/t.jsonl"):
        self.front, self.bound, self.locked, self.session = (
            front, bound, locked, session)

    def __enter__(self):
        import vb.talkd as talkd
        self._saved = (inject.frontmost_app, oslayer.screen_locked,
                       call._target_transcript, talkd.bound_app, call.DRYRUN)
        inject.frontmost_app = lambda: self.front
        oslayer.screen_locked = lambda: self.locked
        call._target_transcript = lambda: self.session
        talkd.bound_app = lambda: self.bound
        call.DRYRUN = False
        call._READY_VAL = {}          # never serve a cached answer into a test
        return self

    def __exit__(self, *exc):
        import vb.talkd as talkd
        (inject.frontmost_app, oslayer.screen_locked, call._target_transcript,
         talkd.bound_app, call.DRYRUN) = self._saved
        call._READY_VAL = {}
        return False


# ---------- the happy path ---------------------------------------------------

def test_bound_app_in_front_is_ready():
    with _Stub(front="Ghostty", bound="Ghostty"):
        r = call.readiness()
    assert r["ok"] is True
    assert r["reason"] == call.READY_OK
    assert call.readiness_reason(r) == ""


def test_match_ignores_case_and_padding():
    """Same comparison talkd.app_focused makes; readiness must not be stricter
    than the gate it is predicting, or it reports a block that never happens."""
    with _Stub(front=" ghostty ", bound="Ghostty"):
        assert call.readiness()["ok"] is True


def test_unbound_is_ready_because_injection_would_accept_it():
    """Pre-binding installs are still always-on in app_focused(), so readiness
    must agree. Reporting a block that the paste would not make would ground
    the phone for a setup that actually works."""
    with _Stub(front="Google Chrome", bound=""):
        r = call.readiness()
    assert r["ok"] is True
    assert r["reason"] == call.READY_OK


# ---------- each blocker names itself ----------------------------------------

def test_another_app_in_front_names_both_apps():
    with _Stub(front="Slack", bound="Ghostty"):
        r = call.readiness()
    assert r["ok"] is False
    assert r["reason"] == call.READY_NOT_FRONTMOST
    said = call.readiness_reason(r)
    assert "Slack" in said and "Ghostty" in said


def test_locked_screen_is_reported_as_locked():
    """The one blocker a person can fix from across the room, so it is worth
    naming instead of folding into a generic failure."""
    with _Stub(front="", bound="Ghostty", locked=True):
        r = call.readiness()
    assert r["ok"] is False
    assert r["reason"] == call.READY_LOCKED
    assert "locked" in call.readiness_reason(r).lower()


def test_unreadable_frontmost_never_claims_a_locked_screen():
    """No frontmost app AND no lock detected: a switched macOS user or a
    revoked Accessibility grant. Blocked either way, but saying "locked"
    would be a specific claim we did not verify, and the user would go
    unlock an already-unlocked Mac."""
    with _Stub(front="", bound="Ghostty", locked=False):
        r = call.readiness()
    assert r["ok"] is False
    assert r["reason"] == call.READY_UNKNOWN_FRONT
    assert "locked" not in call.readiness_reason(r).lower()


def test_no_session_beats_every_other_reason():
    """Nothing to inject INTO outranks where the keyboard is pointing: telling
    someone to bring a terminal forward when no session exists sends them to
    fix the wrong thing."""
    with _Stub(front="Slack", bound="Ghostty", session=""):
        r = call.readiness()
    assert r["ok"] is False
    assert r["reason"] == call.READY_NO_SESSION


# ---------- fail closed ------------------------------------------------------

def test_every_blocked_reason_has_something_to_say():
    """A reason code with no sentence would reach the phone as an empty
    banner, which reads as "fine" precisely when it is not."""
    for reason in (call.READY_NO_SESSION, call.READY_LOCKED,
                   call.READY_NOT_FRONTMOST, call.READY_UNKNOWN_FRONT):
        said = call.readiness_reason(
            {"ok": False, "reason": reason, "front": "Slack",
             "bound": "Ghostty"})
        assert said and not said.startswith("{")


def test_unknown_reason_still_refuses_rather_than_reassures():
    r = {"ok": False, "reason": "something_new", "front": "", "bound": ""}
    assert call.readiness_reason(r) == ""      # no invented sentence
    assert r["ok"] is False                    # but still not ready


def test_dryrun_is_ready_without_touching_the_mac():
    """VB_CALL_DRYRUN exists to test the relay off-desk; probing a frontmost
    app there would block every dry run on a CI box with no window server."""
    import vb.talkd as talkd
    saved = (call.DRYRUN, inject.frontmost_app, talkd.bound_app)
    call.DRYRUN = True
    inject.frontmost_app = lambda: (_ for _ in ()).throw(
        AssertionError("probed the Mac during a dry run"))
    talkd.bound_app = lambda: ""
    try:
        assert call.readiness()["ok"] is True
    finally:
        call.DRYRUN, inject.frontmost_app, talkd.bound_app = saved


# ---------- the cache ---------------------------------------------------------

def test_cache_serves_one_probe_to_many_streams():
    """Every open event stream asks once a second and the mac probe spawns a
    process, so an uncached answer would multiply by connected phones."""
    calls = []
    with _Stub(front="Ghostty", bound="Ghostty"):
        real = inject.frontmost_app

        def counted():
            calls.append(1)
            return real()

        inject.frontmost_app = counted
        for _ in range(10):
            call.readiness_cached()
    assert len(calls) == 1


def test_cache_expires_so_a_window_switch_is_noticed():
    with _Stub(front="Ghostty", bound="Ghostty") as s:
        assert call.readiness_cached()["ok"] is True
        s.front = "Slack"
        assert call.readiness_cached(ttl=0)["reason"] == call.READY_NOT_FRONTMOST


# ---------- the OS probes ----------------------------------------------------

def test_screen_locked_is_a_bool_and_says_no_off_mac():
    """Off macOS we cannot tell, and False is the honest direction: this value
    only picks the WORDING, frontmost_app() is what fails closed."""
    v = oslayer.screen_locked()
    assert isinstance(v, bool)
    if not oslayer.IS_MAC:
        assert v is False


def test_frontmost_probe_lives_in_the_os_layer():
    """House rule: anything shelling out to a per-platform binary belongs in
    oslayer. inject.frontmost_app stays as the seam the focus tests patch."""
    assert hasattr(oslayer, "frontmost_app")
    import inspect
    src = inspect.getsource(inject.frontmost_app)
    assert "osascript" not in src


def test_run_out_answers_empty_on_a_failed_probe():
    assert oslayer._run_out(["false"]) == ""
    assert oslayer._run_out(["definitely-not-a-real-binary-xyz"]) == ""


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("\nall readiness tests passed")
