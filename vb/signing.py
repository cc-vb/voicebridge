"""A stable identity for the dictation app, so permissions survive a rebuild.

macOS does not remember that you granted Microphone and Accessibility to an
application by its name or its path. It remembers the code signature, and for
an ad-hoc signature that identity is the hash of the binary itself:

    designated => cdhash H"dc19729a..."

Which means every single rebuild of the app produces, as far as macOS is
concerned, a different application that has never been granted anything. The
checkbox in System Settings stays on, next to an app that no longer works.
That is the worst kind of broken, because there is nothing for the user to
see and nothing for them to fix.

A self-signed certificate fixes it for free. Signing against a certificate
makes the identity the certificate, not the binary:

    designated => identifier com.voicebridge.dictate and certificate leaf = H"d7bc..."

and that is unchanged by rebuilding. The certificate lives in its own keychain
under ~/.voicebridge so it never touches the login keychain and never asks the
user for a password.

This is not a substitute for a real Developer ID, which is what would let the
app be distributed to other people without Gatekeeper complaining. It only
fixes the identity being unstable on the machine that built it.
"""
import os
import secrets
import subprocess
from pathlib import Path

from . import core

NAME = "Voicebridge Local Signing"
KEYCHAIN = core.STATE_DIR / "signing.keychain-db"
PASSFILE = core.STATE_DIR / "signing.pass"


def _run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, timeout=120, **kw)


def _password() -> str:
    """The keychain password, generated once and kept beside the keychain.

    It is stored in the clear, which is worth being honest about: this keychain
    holds one self-signed key whose only power is to sign this app on this
    machine, and anyone who can read the file can already write the app itself.
    The password exists so the keychain can be unlocked without prompting, not
    as a security boundary."""
    if PASSFILE.exists():
        return PASSFILE.read_text().strip()
    pw = secrets.token_hex(16)
    PASSFILE.parent.mkdir(parents=True, exist_ok=True)
    PASSFILE.write_text(pw)
    os.chmod(PASSFILE, 0o600)
    return pw


def _in_search_list() -> bool:
    r = _run(["security", "list-keychains", "-d", "user"])
    return str(KEYCHAIN) in r.stdout


def _add_to_search_list() -> bool:
    """Put our keychain on the search list, keeping everything already there.

    codesign only looks at keychains on the search list. Passing --keychain is
    not enough; without this step it reports "no identity found", and if its
    stderr is being swallowed the build looks like it succeeded while leaving
    the ad-hoc signature in place."""
    r = _run(["security", "list-keychains", "-d", "user"])
    current = [line.strip().strip('"') for line in r.stdout.splitlines() if line.strip()]
    if str(KEYCHAIN) in current:
        return True
    r = _run(["security", "list-keychains", "-d", "user", "-s", *current, str(KEYCHAIN)])
    return r.returncode == 0


def _create() -> bool:
    """Make the keychain and the certificate. Runs once, ever."""
    import shutil
    if not shutil.which("openssl"):
        core.log("signing: no openssl, falling back to ad-hoc")
        return False
    pw = _password()
    tmp = core.STATE_DIR / "_signing_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        cnf = tmp / "cert.cnf"
        cnf.write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n"
            f"[dn]\nCN={NAME}\n"
            "[v3]\nbasicConstraints=critical,CA:false\n"
            "keyUsage=critical,digitalSignature\n"
            "extendedKeyUsage=critical,codeSigning\n")
        r = _run(["openssl", "req", "-x509", "-newkey", "rsa:2048",
                  "-keyout", str(tmp / "key.pem"), "-out", str(tmp / "cert.pem"),
                  "-days", "7300", "-nodes", "-config", str(cnf)])
        if r.returncode != 0:
            core.log(f"signing: cert generation failed: {r.stderr.strip()[:200]}")
            return False
        r = _run(["openssl", "pkcs12", "-export", "-inkey", str(tmp / "key.pem"),
                  "-in", str(tmp / "cert.pem"), "-out", str(tmp / "id.p12"),
                  "-passout", f"pass:{pw}", "-name", NAME])
        if r.returncode != 0:
            core.log(f"signing: pkcs12 export failed: {r.stderr.strip()[:200]}")
            return False

        _run(["security", "delete-keychain", str(KEYCHAIN)])
        r = _run(["security", "create-keychain", "-p", pw, str(KEYCHAIN)])
        if r.returncode != 0:
            core.log(f"signing: create-keychain failed: {r.stderr.strip()[:200]}")
            return False
        # No auto-lock and no idle timeout, or signing fails at some later date
        # for no reason the user could possibly connect to this.
        _run(["security", "set-keychain-settings", str(KEYCHAIN)])
        _run(["security", "unlock-keychain", "-p", pw, str(KEYCHAIN)])
        r = _run(["security", "import", str(tmp / "id.p12"), "-k", str(KEYCHAIN),
                  "-P", pw, "-T", "/usr/bin/codesign"])
        if r.returncode != 0:
            core.log(f"signing: import failed: {r.stderr.strip()[:200]}")
            return False
        # Lets codesign use the key without putting up a GUI prompt.
        _run(["security", "set-key-partition-list", "-S",
              "apple-tool:,apple:,codesign:", "-s", "-k", pw, str(KEYCHAIN)])
        return _add_to_search_list()
    except Exception as e:
        core.log(f"signing: setup failed: {e}")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def identity() -> str:
    """The signing identity to use, or "" to mean fall back to ad-hoc.

    Creates it on first call and reuses it forever after. Regenerating it would
    change the app's identity and silently revoke the user's permissions, which
    is the exact problem this module exists to prevent, so the existing
    keychain is always preferred over making a new one."""
    try:
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        if KEYCHAIN.exists() and PASSFILE.exists():
            _run(["security", "unlock-keychain", "-p", _password(), str(KEYCHAIN)])
            if _add_to_search_list():
                return NAME
            return ""
        return NAME if _create() else ""
    except Exception as e:
        core.log(f"signing: {e}")
        return ""


def requirement(path) -> str:
    """The designated requirement macOS will identify this bundle by."""
    r = _run(["codesign", "-d", "-r-", str(path)])
    for line in (r.stdout + r.stderr).splitlines():
        if "designated" in line:
            return line.split("=>", 1)[-1].strip()
    return ""
