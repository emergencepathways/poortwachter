"""A light check that a note is actually written in Dutch.

It isn't a grammar check. It just stops "asdf asdf" or an English summary from
ticking a task off: enough words, and clearly more Dutch function words than
English ones."""

import re

DUTCH = set("""
de het een en is ik je jij u hij zij ze we wij jullie niet van dat die dit deze op te in
met voor aan er zijn was waren heb heeft hebben had ook maar als dan om naar bij nog wel
geen al wat wie waar hoe waarom omdat want toen nu hier daar mijn mij me zijn haar ons onze
hun hen kan kun kunt kunnen wil wilt willen moet moeten zal zullen zou zouden ga gaat gaan
ging was word wordt worden werd heel veel weinig meer meest erg echt over uit door tegen
tussen na sinds tot zonder onder boven achter naast vandaag gisteren morgen ook nooit altijd
soms vaak graag leuk mooi goed slecht groot klein nieuw oud ja nee iets niets iemand niemand
alles elke iedere andere zich zelf eens toch dus even weer samen alleen wanneer welke welk
""".split())

ENGLISH = set("""
the a an and is i you he she it we they not of that this these those on to in with for at
there are was were have has had also but if then about from by what who where how why
because when now here my me your his her our their can could will would should do does did
very much more most really over out through against between after since until without under
today yesterday tomorrow never always sometimes often like nice good bad big small new old
yes no something nothing someone everyone everything every other just again together only
""".split())

WORD = re.compile(r"[A-Za-zÀ-ÿ'’-]+")


def words(text):
    return WORD.findall(text or "")


def check(text, min_words):
    ws = [w.lower().strip("'’-") for w in words(text)]
    n = len(ws)
    if n < min_words:
        return False, f"{n} / {min_words} words"
    if n == 0:
        return True, "ok"
    nl = sum(1 for w in ws if w in DUTCH)
    en = sum(1 for w in ws if w in ENGLISH and w not in DUTCH)
    if nl / n < 0.12 or nl < en * 1.5:
        return False, "That doesn't look like Dutch yet. Schrijf in het Nederlands!"
    # Repetition guard: the same word over and over isn't a summary.
    if n >= 10 and len(set(ws)) / n < 0.35:
        return False, "Too much repetition. Write real sentences."
    return True, f"{n} words"
