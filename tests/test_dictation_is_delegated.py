"""The dictation key is a separate product, and voicebridge uses it.

voicebridge speaks back to you; dictator only listens. They were split because
plenty of people want the second thing without the first. What must not happen
is voicebridge keeping a second copy of the dictation stack, because that copy
drifts: the one in dictator has since grown a learned vocabulary, an automatic
engine fallback and a code signing identity that survives a rebuild, and none
of that would be here.
"""
import ast
from pathlib import Path
from unittest import mock

import pytest

from vb import dictator

ROOT = Path(__file__).resolve().parent.parent


def test_no_second_copy_of_the_dictation_stack():
    gone = ["dictate.py", "always.py", "hotkey.py", "signing.py"]
    still_here = [n for n in gone if (ROOT / "vb" / n).exists()]
    assert not still_here, f"voicebridge has its own copy again: {still_here}"


def test_the_commands_hand_through():
    seen = {}

    def fake(argv, *a, **kw):
        seen["argv"] = argv
        return mock.Mock(returncode=0)

    with mock.patch.object(dictator, "path", return_value="/x/dictator"), \
         mock.patch("subprocess.run", side_effect=fake):
        assert dictator.run(["on", "fn"]) == 0
    assert seen["argv"] == ["/x/dictator", "on", "fn"]


def test_a_missing_install_says_how_to_get_it(capsys):
    """Without this the key simply does nothing, which is indistinguishable
    from a broken keyboard."""
    with mock.patch.object(dictator, "path", return_value=""):
        assert dictator.run(["on"]) == 1
    out = capsys.readouterr().out
    assert "dictator" in out and "install" in out.lower()


def test_the_cli_does_not_import_the_old_modules():
    src = (ROOT / "bin" / "vb").read_text()
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "vb":
            for a in node.names:
                if a.name in ("dictate", "always", "hotkey", "signing"):
                    bad.append(f"line {node.lineno}: {a.name}")
    assert not bad, bad
