"""Guards that decide whether audio becomes a prompt.

Two failures motivated these tests, both observed live:

  1. A one-word "Adios" cut off a reply mid-sentence, because the
     low-overlap barge-in path accepted any transcribed text.
  2. Text captured while we were speaking reached inject.paste_text
     without passing the echo guard, so Claude's own voice came back in
     as a prompt.

Everything here is a pure function, so no mic, no whisper, no daemon.
"""

import json
import time

import pytest

from vb import core, talkd


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Point every bit of on-disk state at a tmp dir, so tests never read
    the developer's real speech history (and never leave residue)."""
    monkeypatch.setattr(core, "STATE_DIR", tmp_path)
    monkeypatch.setattr(talkd, "STATE", tmp_path / "talk")
    monkeypatch.setattr(talkd, "VOICED", tmp_path / "talk" / "voiced")
    (tmp_path / "talk" / "voiced").mkdir(parents=True)
    monkeypatch.setattr(talkd, "SENS", tmp_path / "talk" / "sens")
    return tmp_path


def spoken(tmp_path, text, ago=0.0):
    """Record `text` as something we said `ago` seconds back."""
    rec = {"text": text, "ts": time.time() - ago}
    (tmp_path / "spoken_history").write_text(json.dumps(rec) + "\n")
    (tmp_path / "last_spoken_text").write_text(json.dumps(rec))


LOUD = 0.02      # comfortably above the 'normal' RMS floor
CONF = 0.9       # confident transcription


# ---------- barge_decision: what may cut off a reply -------------------------

def test_single_stray_word_does_not_cut_off_a_reply(isolated_state):
    """The live regression: 'Adios.' killed a reply half a second in."""
    assert talkd.barge_decision("Adios.", CONF, LOUD) == ""


@pytest.mark.parametrize("word", ["Adios.", "Hmm", "yeah", "okay", "Right."])
def test_short_interjections_never_barge(isolated_state, word):
    assert talkd.barge_decision(word, CONF, LOUD) == ""


@pytest.mark.parametrize("cmd", ["stop", "wait", "hold on", "shut up", "chup"])
def test_attention_words_always_cut_through(isolated_state, cmd):
    """Deliberate interrupts must work even at one word."""
    assert talkd.barge_decision(cmd, CONF, LOUD) != ""


def test_real_sentence_barges(isolated_state):
    heard = "no stop that is the wrong file"
    assert talkd.barge_decision(heard, CONF, LOUD) != ""


def test_quiet_background_chatter_never_barges(isolated_state):
    """Below the RMS floor: a TV in the room must not cut off a reply."""
    assert talkd.barge_decision("could you open the door", CONF, 0.001) == ""


def test_low_confidence_transcription_never_barges(isolated_state):
    assert talkd.barge_decision("could you open the door", 0.10, LOUD) == ""


def test_noise_tags_never_barge(isolated_state):
    for tag in ["(air whooshing)", "[BLANK_AUDIO]", "(wind blowing)", "..."]:
        assert talkd.barge_decision(tag, CONF, LOUD) == ""


def test_our_own_speech_echoing_back_never_barges(isolated_state):
    """Speakers, not headphones: the mic hears our TTS. Keep talking."""
    reply = "the migration script rewrites the index before the backfill runs"
    spoken(isolated_state, reply)
    assert talkd.barge_decision(reply, CONF, LOUD) == ""


def test_user_talking_over_our_echo_is_heard_first_try(isolated_state):
    """Echo subtraction: their words survive, ours are stripped.

    They interrupt using the reply's own vocabulary, so overlap is 0.44 --
    too high for the strict tier, but five new words is a person talking,
    not echo bleed. Caught by the partial-overlap tier."""
    spoken(isolated_state, "the migration script rewrites the index")
    heard = "the migration script no use the other branch instead"
    barge = talkd.barge_decision(heard, CONF, LOUD)
    assert "other branch" in barge
    assert "migration" not in barge


def test_partial_overlap_needs_a_sentence_of_new_words(isolated_state):
    """The tier that makes the test above pass must not open the door to
    echo bleed. Same 0.44-ish overlap, but only a couple of stray words --
    which is what imperfect echo subtraction leaves behind -- stays rejected.
    Five new words is a person; two is our own voice coming back."""
    spoken(isolated_state, "the migration script rewrites the index")
    assert talkd.barge_decision("the migration script rewrites no use",
                                CONF, LOUD) == ""


def test_mostly_our_own_voice_never_barges(isolated_state):
    """Above the partial ceiling it is our reply, however many words the
    transcriber hallucinates on top. This is the regression that forced the
    strict gate originally: long replies cutting themselves off mid-sentence."""
    reply = ("the deployment pipeline builds the container and pushes it to "
             "the registry before the rollout begins")
    spoken(isolated_state, reply)
    assert talkd.barge_decision(reply + " uh huh yeah okay right",
                                CONF, LOUD) == ""


def test_barge_returns_their_words_not_ours(isolated_state):
    """A barge-in becomes the next prompt, so it must not carry our own
    sentence into it. Every accepting path returns the echo-stripped residue."""
    spoken(isolated_state, "i will rewrite the index in the migration script")
    barge = talkd.barge_decision(
        "no use the other branch instead please", CONF, LOUD)
    assert barge, "a clear interruption should be heard"
    assert "migration" not in barge and "index" not in barge


# ---------- screen_capture: what may become a prompt -------------------------

def test_echo_is_never_injected_as_a_prompt(isolated_state):
    """The second live regression: our own voice arriving as a prompt."""
    reply = "i can switch the endpoint over to the staging host if you want"
    spoken(isolated_state, reply)
    assert talkd.screen_capture(reply, CONF, LOUD, "all") == "echo"


def test_barge_text_is_screened_not_trusted(isolated_state):
    """Barge-ins used to bypass this entirely. Sentinels skip the conf and
    loudness gates, already judged against the wav; echo must still bite."""
    reply = "i can switch the endpoint over to the staging host if you want"
    spoken(isolated_state, reply)
    assert talkd.screen_capture(reply, 0.0, -1.0, "all") == "echo"


def test_sentinels_do_not_accidentally_fail_a_clean_barge(isolated_state):
    assert talkd.screen_capture(
        "run the tests again please", 0.0, -1.0, "all") == ""


def test_undirected_remark_is_dropped_in_agent_mode(isolated_state):
    assert talkd.screen_capture("mm sure", CONF, LOUD, "all") == "not directed"


def test_directed_question_passes(isolated_state):
    assert talkd.screen_capture(
        "can you check the logs", CONF, LOUD, "all") == ""


def test_wake_mode_skips_the_directedness_gate(isolated_state):
    """In wake mode the wake word already proved intent."""
    assert talkd.screen_capture("mm sure", CONF, LOUD, "wake") == ""


def test_too_quiet_is_dropped(isolated_state):
    assert talkd.screen_capture(
        "can you check the logs", CONF, 0.001, "all") == "too-quiet (0.001)"


def test_empty_and_noise_are_dropped(isolated_state):
    assert talkd.screen_capture("", CONF, LOUD, "all") == "noise"
    assert talkd.screen_capture("(coughs)", CONF, LOUD, "all") == "noise"


def test_foreign_script_media_is_dropped(isolated_state):
    """A Korean drama playing nearby is not the user."""
    assert talkd.screen_capture("안녕하세요 반갑습니다", CONF, LOUD, "all") == "noise"


def test_stale_echo_history_stops_blocking(isolated_state):
    """Say the same words a few minutes later and they're yours, not echo."""
    reply = "i can switch the endpoint over to the staging host if you want"
    spoken(isolated_state, reply, ago=600)
    assert talkd.screen_capture(reply, CONF, LOUD, "all") == ""
