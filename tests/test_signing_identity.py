"""The dictation app's identity must not be its own binary hash.

An ad-hoc signature makes macOS identify the app by cdhash, so every rebuild
revokes Microphone and Accessibility while leaving the checkbox in System
Settings switched on. Nothing about that is visible: the app is there, the
toggle is on, and the key does nothing. It cost a real user a confusing
install, so it gets a test.
"""
import subprocess
from unittest import mock

import pytest

from vb import always, signing


def _requirement():
    r = subprocess.run(["codesign", "-d", "-r-", str(always.APP)],
                       capture_output=True, text=True)
    return r.stdout + r.stderr


@pytest.mark.skipif(not always.APP.exists(), reason="app not built here")
def test_identity_is_the_certificate_not_the_binary():
    req = _requirement()
    assert "designated" in req, f"app is not signed at all: {req!r}"
    assert "cdhash" not in req, (
        "app is ad-hoc signed, so rebuilding it will silently revoke the "
        f"user's permissions: {req.strip()!r}")
    assert "certificate leaf" in req, req.strip()


def test_search_list_keeps_what_is_already_there():
    """Adding our keychain must not evict the user's login keychain.

    security list-keychains -s REPLACES the list rather than appending to it,
    so getting this wrong locks the user out of every password they have
    stored, which is a far worse bug than the one being fixed."""
    login = "/Users/someone/Library/Keychains/login.keychain-db"
    calls = []

    def fake(args, **kw):
        calls.append(args)
        out = f'    "{login}"\n' if "-s" not in args else ""
        return mock.Mock(returncode=0, stdout=out, stderr="")

    with mock.patch.object(signing, "_run", side_effect=fake):
        assert signing._add_to_search_list()

    setter = [c for c in calls if "-s" in c]
    assert len(setter) == 1, calls
    assert login in setter[0], setter[0]
    assert str(signing.KEYCHAIN) in setter[0], setter[0]


def test_existing_keychain_is_never_regenerated():
    """Making a new certificate would revoke the permissions all over again."""
    with mock.patch.object(signing, "KEYCHAIN", mock.Mock(exists=lambda: True)), \
         mock.patch.object(signing, "PASSFILE", mock.Mock(exists=lambda: True)), \
         mock.patch.object(signing, "_password", return_value="pw"), \
         mock.patch.object(signing, "_run", return_value=mock.Mock(returncode=0, stdout="", stderr="")), \
         mock.patch.object(signing, "_add_to_search_list", return_value=True), \
         mock.patch.object(signing, "_create") as create:
        assert signing.identity() == signing.NAME
    create.assert_not_called()


def test_signing_failure_is_logged_not_swallowed():
    """A silent signing failure is how this bug survived in the first place."""
    with mock.patch.object(signing, "identity", return_value="Some Identity"), \
         mock.patch("subprocess.run",
                    return_value=mock.Mock(returncode=1, stdout="", stderr="no identity found")), \
         mock.patch.object(always.core, "log") as log:
        always._sign(always.APP)
    assert log.called, "signing failed and nothing said so"
    assert "no identity found" in " ".join(str(c) for c in log.call_args_list)
