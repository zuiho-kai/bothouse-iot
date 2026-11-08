"""Chinese voice-command matching for role wake words and microphone controls."""
from __future__ import annotations

import re
from dataclasses import dataclass

from pypinyin import Style, lazy_pinyin

_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)
_TONE = re.compile(r"[1-5]$")
_NL = re.compile(r"^n(?=[aeiouv])")
_NASAL = re.compile(r"([iea])ng(?=[1-5]?$)")

PAUSE, MUTE, UNMUTE, WAKE, IGNORE = "pause", "mute", "unmute", "wake", "ignore"


def _syllables(text: str) -> list[str]:
    return [_NASAL.sub(r"\1n", _NL.sub("l", syllable))
            for syllable in lazy_pinyin(_PUNCT.sub("", text).lower(),
                                        style=Style.TONE3, neutral_tone_with_five=True)]


def _bare(syllables: list[str]) -> list[str]:
    return [_TONE.sub("", syllable) for syllable in syllables]


def sounds_like_suffix(text: str, spoken: str) -> bool:
    heard, output = _bare(_syllables(text)), _bare(_syllables(spoken))
    return bool(heard and len(heard) <= len(output) and output[-len(heard):] == heard)


class Term:
    def __init__(self, text: str):
        self.text = text
        self.syllables = _syllables(text)
        self.single = len(self.syllables) == 1

    def heard_in(self, text: str) -> bool:
        heard = _syllables(text)
        if self.single:
            return any(heard[i:i + 1] == self.syllables
                       for i in range(len(heard)))
        needle = _bare(self.syllables)
        haystack = _bare(heard)
        return any(haystack[i:i + len(needle)] == needle
                   for i in range(len(haystack) - len(needle) + 1))

    def is_command(self, text: str, variants: tuple[str, ...]) -> bool:
        heard = _syllables(text)
        return any(_bare(heard) == _bare(_syllables(variant)) for variant in variants)


@dataclass(frozen=True)
class Heard:
    action: str
    targets: tuple[str, ...] = ()


class Words:
    def __init__(self, roles: list[tuple[str, str]], pause: str = "暂停",
                 mute: str = "闭麦", unmute: str = "开麦"):
        self.roles = [(role, name, Term(name)) for role, name in roles if name]
        self.pause, self.mute, self.unmute = Term(pause), Term(mute), Term(unmute)

    def classify(self, text: str, muted: bool = False) -> Heard:
        if self.unmute.is_command(text, ("开麦",)):
            return Heard(UNMUTE)
        if self.mute.is_command(text, ("闭麦",)):
            return Heard(MUTE)
        if self.pause.is_command(text, ("暂停", "暂停一下")):
            return Heard(PAUSE)
        if muted:
            return Heard(IGNORE)
        targets = tuple(role for role, _, term in self.roles if term.heard_in(text))
        return Heard(WAKE, targets) if targets else Heard(IGNORE)
