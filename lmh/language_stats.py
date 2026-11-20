"""Approximate English profanity counts; retain counts, never matched text."""

import re
import unicodedata


LEVELS = {
    "mild": frozenset("damn hell crap bloody piss pissed bastard".split()),
    "strong": frozenset(
        "fuck fucked fucking fucker fuckers fuckedup motherfucker motherfucking "
        "shit shitty bullshit shitting asshole assholes bitch bitches cunt cunts dickhead".split()
    ),
    "slurs": frozenset(
        "nigger niggers nigga niggas faggot faggots kike kikes chink chinks spic spics "
        "wetback wetbacks retard retards retarded tranny trannies dyke dykes".split()
    ),
}
_LEXICON = {word: level for level, words in LEVELS.items() for word in words}
_VARIANTS = {"fuk": "fuck", "fck": "fuck", "fukin": "fucking", "fuckingg": "fucking",
             "shyt": "shit", "shite": "shit", "btch": "bitch", "niga": "nigga",
             "fagot": "faggot", "fgot": "faggot"}
_LEET = str.maketrans("0134578@$!", "oieastbasi")
_LOOKALIKES = str.maketrans("аеіорсхкѕΑΒΕΙΚΜΝΟΡΤΧ", "aeiopcxksabeikmnop tx".replace(" ", ""))
_TOKENS = re.compile(r"[a-z0-9@$!]+")
_RUNS = re.compile(r"(.)\1{2,}")
_SEPARATED = re.compile(r"(?<!\w)(?:[a-z0-9@$!][ ._*-]){2,17}[a-z0-9@$!](?!\w)")
_PUNCTUATED = re.compile(r"(?<!\w)[a-z0-9@$!]+(?:[._*-][a-z0-9@$!]+)+(?!\w)")
_MAX_TEXT = 256 * 1024
_TYPO_LEXICON = {
    word: level for word, level in _LEXICON.items()
    if len(word) >= 6 and level in {"strong", "slurs"}
}
_MISSING_LETTER = {}
for _word, _severity in _TYPO_LEXICON.items():
    for _index in range(len(_word)):
        _variant = _word[:_index] + _word[_index + 1:]
        _MISSING_LETTER[_variant] = _severity


def _plain(text):
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text.translate(_LOOKALIKES).casefold())
        if unicodedata.category(ch) not in {"Mn", "Cf"}
    )


def _level(token):
    candidate = token.translate(_LEET)
    for word in (token, candidate, _RUNS.sub(r"\1\1", candidate), _RUNS.sub(r"\1", candidate)):
        level = _LEXICON.get(_VARIANTS.get(word, word))
        if level:
            return level
    if 4 <= len(candidate) <= 16:
        # Only try a single adjacent transposition, not substring or general fuzzy matches.
        for index in range(len(candidate) - 1):
            swapped = candidate[:index] + candidate[index:index + 2][::-1] + candidate[index + 2:]
            if (level := _LEXICON.get(swapped)) in {"strong", "slurs"}:
                return level
    if 5 <= len(candidate) <= 16:
        if level := _MISSING_LETTER.get(candidate):
            return level
        for index in range(len(candidate)):
            if level := _TYPO_LEXICON.get(candidate[:index] + candidate[index + 1:]):
                return level
    return None


def count_profanity(text):
    counts = dict.fromkeys(LEVELS, 0)
    normalized = _plain(text)
    for pattern in (_SEPARATED, _PUNCTUATED):
        def count_obscured(match):
            candidate = "".join(_TOKENS.findall(match[0]))
            level = _level(candidate)
            if level:
                counts[level] += 1
                return " "
            return match[0]
        normalized = pattern.sub(count_obscured, normalized)
    for token in _TOKENS.findall(normalized):
        level = _level(token) or _level(token.strip("!@$"))
        if level:
            counts[level] += 1
        elif any(mark in token for mark in "!@$"):
            for part in re.split(r"[!@$]+", token):
                if level := _level(part):
                    counts[level] += 1
    return counts


class OutputSample:
    """Collect a bounded sample of generated text, preserving stream token boundaries."""

    def __init__(self):
        self.parts = {}
        self.remaining = _MAX_TEXT
        self.truncated = False

    def add(self, choice, text):
        if not isinstance(text, str) or not text:
            return
        sample = text[:self.remaining]
        self.truncated |= len(sample) != len(text)
        self.remaining -= len(sample)
        if sample:
            self.parts.setdefault(choice, []).append(sample)

    def feed(self, result, *, streaming=False):
        choices = result.get("choices")
        if isinstance(choices, list):
            for index, choice in enumerate(choices):
                if not isinstance(choice, dict):
                    continue
                message = choice.get("delta" if streaming else "message")
                key = choice.get("index", index)
                if type(key) is not int:
                    key = index
                if isinstance(message, dict):
                    self.add(key, message.get("content"))
                self.add(key, choice.get("text"))
        elif not streaming:
            self.add(0, result.get("text"))

    def record(self, state, model):
        if self.parts:
            counts = dict.fromkeys(LEVELS, 0)
            for parts in self.parts.values():
                for level, count in count_profanity("".join(parts)).items():
                    counts[level] += count
            state.record_language(model, counts, self.truncated)
