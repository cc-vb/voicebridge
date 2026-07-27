"""Phone attachments: files land on disk, the turn names their paths.

Injection is a clipboard paste of TEXT, so an attachment cannot ride along
inside the prompt. It is written under ~/.voicebridge/uploads and the session
opens it by path. That makes three things load-bearing, and this covers them:
a phone-supplied filename must not escape the directory, the bytes decide the
extension (an iPhone calls a HEIC photo .jpg), and nothing is left on disk
forever.

Run: python3 tests/test_uploads.py   (no pytest needed)
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vb import call, core, oslayer  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 32
PDF = b"%PDF-1.7\n" + b"\x00" * 32


def _fresh(tmp):
    core.STATE_DIR = tmp
    call.UPLOAD_DIR = tmp / "uploads"
    call._active_sid = lambda: "SID1"


def test_safe_name_cannot_escape_the_upload_directory():
    # the name arrives in a header from a public URL: it is untrusted input
    assert "/" not in call._safe_name("../../.ssh/id_rsa")
    assert ".." not in call._safe_name("....//evil.png")
    assert call._safe_name("/etc/passwd") == "passwd"
    assert call._safe_name("") == "attachment"
    assert call._safe_name("...") == "attachment"     # nothing but dots
    assert call._safe_name("a" * 300).count("a") <= 80
    # ordinary names survive intact
    assert call._safe_name("IMG_0001.jpg") == "IMG_0001.jpg"


def test_extension_comes_from_the_bytes_not_the_label():
    assert call._sniff_ext(PNG) == ".png"
    assert call._sniff_ext(JPG) == ".jpg"
    assert call._sniff_ext(HEIC) == ".heic"
    assert call._sniff_ext(PDF) == ".pdf"
    assert call._sniff_ext(b"nothing recognisable") == ""


def test_save_writes_the_file_and_reports_a_readable_kind():
    tmp = Path(tempfile.mkdtemp())
    _fresh(tmp)
    info = call._save_upload(PNG, "shot.png", "image/png")
    assert os.path.exists(info["path"])
    assert info["kind"] == "image"          # the session can view it
    assert info["size"] == len(PNG)
    assert Path(info["path"]).parent.name == "SID1"   # scoped per session
    with open(info["path"], "rb") as f:
        assert f.read() == PNG

    doc = call._save_upload(PDF, "notes.pdf", "application/pdf")
    assert doc["kind"] == "file"            # not an image, still attachable
    assert doc["path"].endswith(".pdf")


def test_a_mislabelled_photo_is_stored_by_its_real_type():
    # phones hand you 'photo.jpg' that is really PNG bytes
    tmp = Path(tempfile.mkdtemp())
    _fresh(tmp)
    info = call._save_upload(PNG, "photo.jpg", "image/jpeg")
    assert info["path"].endswith(".png")


def test_same_name_twice_does_not_overwrite_the_first():
    tmp = Path(tempfile.mkdtemp())
    _fresh(tmp)
    a = call._save_upload(PNG, "IMG_0001.png", "image/png")
    b = call._save_upload(JPG, "IMG_0001.png", "image/png")
    assert a["path"] != b["path"]
    assert os.path.exists(a["path"]) and os.path.exists(b["path"])


def test_traversal_name_still_lands_inside_the_upload_directory():
    tmp = Path(tempfile.mkdtemp())
    _fresh(tmp)
    info = call._save_upload(PNG, "../../../../tmp/pwned.png", "image/png")
    assert str(call.UPLOAD_DIR) in os.path.realpath(info["path"])


def test_uploads_are_not_world_readable():
    """This directory is fed by a public URL; it has no business being open.

    POSIX only: on Windows chmod moves just the read-only bit, so the mode
    assertion would fail there for a reason that says nothing about the
    code. The uploads directory inherits its parent's ACL instead."""
    if os.name != "posix":
        print("  (skipped: not a POSIX filesystem)")
        return
    tmp = Path(tempfile.mkdtemp())
    _fresh(tmp)
    info = call._save_upload(PNG, "shot.png", "image/png")
    assert oct(os.stat(info["path"]).st_mode)[-3:] == "600"
    assert oct(os.stat(call.UPLOAD_DIR).st_mode)[-3:] == "700"


def test_old_attachments_are_pruned_new_ones_are_kept():
    tmp = Path(tempfile.mkdtemp())
    _fresh(tmp)
    keep = call._save_upload(PNG, "fresh.png", "image/png")
    old = Path(keep["path"]).parent / "stale.png"
    old.write_bytes(PNG)
    ancient = time.time() - (call.UPLOAD_KEEP_DAYS + 1) * 86400
    os.utime(old, (ancient, ancient))
    call._prune_uploads()
    assert not old.exists()                 # past the keep window
    assert os.path.exists(keep["path"])     # today's photo stays


def test_heic_is_converted_so_the_session_can_actually_view_it():
    """An iPhone photo picked out of Files arrives as HEIC, which Claude
    cannot read. Without this it lands as an unopenable file.

    The converter itself is per-OS, so it is stubbed at the oslayer seam;
    everything above it (routing, the .jpg name, the 'image' kind, deleting
    the original) is the real code path."""
    tmp = Path(tempfile.mkdtemp())
    _fresh(tmp)
    seen = {}

    def fake_convert(src, out):
        seen["src"] = src
        with open(out, "wb") as f:
            f.write(JPG)
        return True

    real = oslayer.heic_to_jpeg
    oslayer.heic_to_jpeg = fake_convert
    try:
        info = call._save_upload(HEIC, "IMG_0002.heic", "image/heic")
    finally:
        oslayer.heic_to_jpeg = real
    assert seen["src"].endswith(".heic")
    assert info["path"].endswith(".jpg")
    assert info["kind"] == "image"
    assert not os.path.exists(seen["src"])   # the unreadable original is gone


def test_heic_survives_a_machine_with_no_converter():
    """Windows and bare Linux may have nothing that can decode HEIC. The
    attachment must still arrive rather than vanish: degraded, not lost."""
    tmp = Path(tempfile.mkdtemp())
    _fresh(tmp)
    real = oslayer.heic_to_jpeg
    oslayer.heic_to_jpeg = lambda src, out: False
    try:
        info = call._save_upload(HEIC, "IMG_0003.heic", "image/heic")
    finally:
        oslayer.heic_to_jpeg = real
    assert os.path.exists(info["path"])      # still on disk for the session
    assert info["path"].endswith(".heic")
    assert info["kind"] == "file"            # honestly NOT reported as an image


def test_android_jpeg_needs_no_conversion_at_all():
    """Android hands you JPEG, not HEIC, so the converter must never run."""
    tmp = Path(tempfile.mkdtemp())
    _fresh(tmp)
    real = oslayer.heic_to_jpeg

    def boom(src, out):
        raise AssertionError("converter ran on a plain JPEG")

    oslayer.heic_to_jpeg = boom
    try:
        info = call._save_upload(JPG, "PXL_20260726_123456.jpg", "image/jpeg")
    finally:
        oslayer.heic_to_jpeg = real
    assert info["path"].endswith(".jpg")
    assert info["kind"] == "image"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("\nall upload tests passed")
