"""Devanagari to English letters, deterministically.

Prompt steering can pull Whisper's output into Latin script, but it is
probabilistic: it works most of the time, it costs accuracy on English when the
prompt is strong enough to be reliable on Hindi, and no flag makes it certain.
Measured on this machine: a steer strong enough to stop Devanagari leaking also
turned spoken English into Hindi.

So the script is not asked for, it is imposed. Transcribe honestly, and if
Devanagari comes back, convert it here. That is the same shape as the product
this is chasing, which pivots through Devanagari and romanizes before delivery.
The difference is that a lookup table cannot hallucinate and costs nothing.

The lexicon is Google Dakshina v1.0 (CC BY-SA 4.0), reduced to the most
commonly attested romanization per word. Attribution and share-alike apply to
that file, which matters if this is ever distributed.

Known limit, inherited from the architecture rather than the table: an English
word the recogniser committed to Devanagari cannot be recovered. "pull request"
heard as a Devanagari spelling comes back as "pool rekvest". The vendor has the
same bug and documents it.
"""
import re
from pathlib import Path

_LEX = Path(__file__).resolve().parent / "data" / "hi_roman.tsv"
_MAP = None

DEVA = re.compile(r"[ऀ-ॿ]")

# Enough to sound out a word the table has never seen. Longer keys first, so
# a two-character cluster wins over its first character.
_CHARS = [
    ("क्ष", "ksh"), ("त्र", "tr"), ("ज्ञ", "gy"), ("श्र", "shr"),
    ("ऑ", "o"), ("ओ", "o"), ("औ", "au"), ("आ", "aa"), ("अ", "a"),
    ("ई", "ee"), ("इ", "i"), ("ऊ", "oo"), ("उ", "u"), ("ए", "e"), ("ऐ", "ai"),
    ("ख", "kh"), ("घ", "gh"), ("छ", "chh"), ("झ", "jh"), ("ठ", "th"),
    ("ढ", "dh"), ("थ", "th"), ("ध", "dh"), ("फ", "ph"), ("भ", "bh"),
    ("क", "k"), ("ग", "g"), ("च", "ch"), ("ज", "j"), ("ट", "t"), ("ड", "d"),
    ("ण", "n"), ("त", "t"), ("द", "d"), ("न", "n"), ("प", "p"), ("ब", "b"),
    ("म", "m"), ("य", "y"), ("र", "r"), ("ल", "l"), ("व", "v"), ("श", "sh"),
    ("ष", "sh"), ("स", "s"), ("ह", "h"), ("ळ", "l"),
    ("ा", "a"), ("ि", "i"), ("ी", "ee"), ("ु", "u"), ("ू", "oo"),
    ("े", "e"), ("ै", "ai"), ("ो", "o"), ("ौ", "au"), ("ृ", "ri"),
    ("ं", "n"), ("ँ", "n"), ("ः", "h"), ("्", ""), ("़", ""), ("ऽ", ""),
    ("०", "0"), ("१", "1"), ("२", "2"), ("३", "3"), ("४", "4"),
    ("५", "5"), ("६", "6"), ("७", "7"), ("८", "8"), ("९", "9"),
]


def _load():
    global _MAP
    if _MAP is not None:
        return _MAP
    m = {}
    try:
        with open(_LEX, encoding="utf-8") as f:
            for line in f:
                if line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) == 2 and parts[0]:
                    m[parts[0]] = parts[1]
    except Exception:
        pass
    _MAP = m
    return m


def _sound_out(word: str) -> str:
    """A word the table has not seen. Worse than a lookup, better than leaving
    it in a script the reader cannot use."""
    out, i = [], 0
    while i < len(word):
        for src, dst in _CHARS:
            if word.startswith(src, i):
                out.append(dst)
                i += len(src)
                break
        else:
            out.append(word[i])
            i += 1
    return "".join(out)


def has_devanagari(text: str) -> bool:
    return bool(DEVA.search(text or ""))


def to_latin(text: str) -> str:
    """Rewrite only the Devanagari. English words are left exactly as they are,
    which is the whole reason this runs on the output rather than being asked
    for in the prompt."""
    if not has_devanagari(text):
        return text
    lex = _load()
    def one(m):
        w = m.group(0)
        return lex.get(w) or _sound_out(w)
    return re.sub(r"[ऀ-ॿ]+", one, text)
