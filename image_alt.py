"""Describe an image for alt text, using claude -p's vision.

Until August 2026 this bot's alt text was a citation: title, year, archive.
That tells a blind reader who holds the photograph and nothing whatsoever
about the photograph, and it restated the post text almost word for word, so
the same line was read out twice. On a bot whose entire content IS the picture,
that is the weakest possible alt.

describe() sends the actual image bytes to the model and asks what is visible.
The caption goes along only as context to contradict-check against, never as
something to paraphrase: paraphrasing the caption is how the old alt ended up
describing provenance instead of pixels.

Best-effort by design. Any failure returns None and the caller falls back to
the attribution-only alt it used before, on the same principle the card bots
already follow: a missing description is not worth a missing post.

The reply is then scrubbed of remarks addressed to the operator rather than
the reader (see _strip_meta): those had been shipping to screen readers.

The model runs with cwd set to the temp directory holding the one image, so
the only file it can reach by a bare name is the one it is being asked about.
"""

import re
import subprocess
import tempfile
from pathlib import Path

import limit_guard

# Vision quality is the whole point of this module, and the volume is tiny
# (a handful of calls a day), so this does not drop to haiku the way the
# translation step does.
MODEL = 'claude-sonnet-5'
TIMEOUT = 120

# How long to wait out a spent `claude -p` quota before giving up and falling
# back to the citation. Deliberately shorter than a post-critical caller's
# budget: a description is worth a short wait, because the silent fallback is
# this estate's known weak spot (bot_alt_check.py exists for it), but it is
# not worth holding a whole post back all evening for.
LIMIT_BUDGET_S = 3600

# One quota wait per run, not one per image. Without this a four-image post
# whose retry also came back limited would wait the budget four times over.
_limit_waited = False
MAX_CHARS = 600
MIN_CHARS = 20

# Prefixed by callers to any alt text built from describe()'s output.
#
# The bot's bio says the descriptions are generated, but a bio is on the
# profile and alt text travels without it: into a feed, a repost, a quote
# post, an embed. The reader who most needs to know is the one least likely
# to have seen it.
#
# It leads rather than trails because it exists to calibrate. Heard after the
# description, the listener has already built a picture on the assumption
# that someone looked at the photograph. Heard first, they weigh the rest as
# they go. Three words is a fair price for that, on descriptions the audience
# for alt text is by definition unable to check.
#
# Callers must apply it ONLY to generated text. Every caller here also has an
# attribution-only fallback for when describe() returns None, and that string
# is catalog metadata written by a human. Labeling it would be a false
# claim in the opposite direction.
DISCLOSURE = 'A.I.-generated description:'

# American spelling below matches the house-style default (CLAUDE.md, 28
# August 2026): this bot's subject is Korean, not British, so it takes the
# default rather than the everylibrary/Holmes exception for British subjects.
_PROMPT = """Write alt text for a blind reader of a social media post, describing the image ./{name}

Describe what is actually VISIBLE: the subject, the setting, the composition, any notable detail. One or two sentences, 40 words at most.

Rules:
- Describe only what you can see. Never state names, dates, places or events that are not visually evident, however likely they seem.
- That includes people: do not assign gender, age or role from clothing, hair or the caption. Write "four people" or "a figure seated on the ground" unless the photograph itself puts it beyond doubt.
- Attribute a detail only to the figures it is actually visible on. If three of four are barefoot, say three.
- Attribute a detail to the thing it actually belongs to, not to the thing nearest it. A canopy over the pavement is not the building's entrance canopy; a flower pinned to a coat is not a flower being held.
- Prefer a safe observation to a precise one. Do not name a thing you cannot actually resolve: if a small dark shape could be a figure or could be a tree trunk, leave it out rather than choose. A listener cannot check what you tell them, so an invented detail costs them more than a missing one.
- Do not give a number unless you have counted it. "Several people look on" beats "five other men look on" when you have not counted five, and the same goes for windows, storeys, doors and lawns.
- Open by naming the medium where it is not obvious, e.g. "Black-and-white photograph" or "Pen-and-ink illustration".
- Do not restate the caption below. It is read out separately, and repeating it is the flaw this replaces. Use it only to avoid contradicting what is known.
- American English. No emoji, no markdown, no surrounding quotation marks.
- If the image cannot be read at all, reply with exactly CANNOT_SEE.
- Return ONLY the alt text and nothing else.

Caption, for context only (do not restate): {context}"""

# Defensive only: the prompt forbids emoji, but alt text is exactly where a
# stray decorative glyph is most annoying, since it gets announced by name.
_EMOJI = re.compile(
    '[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]')

# The model sometimes answers the operator instead of the reader. On 15 August
# 2026 an alt shipped as:
#
#   "Note: this image doesn't match the caption, it shows a rooftop pigeon
#    coop, not a city plaza. Flagging that before giving alt text.
#    Black-and-white photograph of a wooden rooftop shed with wire-mesh
#    cages, dozens of pigeons taking flight above the roofline..."
#
# so a screen reader user heard the aside before reaching the description.
# "Return ONLY the alt text" was a prompt instruction with nothing enforcing
# it, and the contradiction-check rule invites the leak: the model is told to
# check the caption for contradictions and has nowhere to report one it finds.
#
# Two patterns rather than one, because first person has to stay case
# sensitive: a lowercase bare "i" is a stray character, not a pronoun, and
# folding the case would let it match inside a description.
_META = re.compile(r"""
    ^(note|caveat|disclaimer|warning|correction)\b[:,]  # "Note: ..."
  | ^(sure|okay|ok|certainly|here\s+is|here's)\b        # chat-assistant opener
  | \balt\s+text\b                                      # names the task itself
  | \b(the|this|that|its|provided)\s+caption\b          # the forbidden referent
  | \bflagging\s+(that|this)\b
  | \bdoes\s*n[o']?t\s+match\b
  | \blet\s+me\s+know\b
""", re.IGNORECASE | re.VERBOSE)
_FIRST_PERSON = re.compile(r"\bI\b|\bI['’](m|ll|ve|d)\b")

_SENTENCE = re.compile(r'(?<=[.!?])\s+')

# "Here's the alt text: Black-and-white photograph of..." has no sentence
# break after the colon, so sentence-level stripping would swallow the
# description along with the lead-in. Handled first, and separately: a short
# clause naming the task and ending in a colon is never part of a description.
_LEAD_IN = re.compile(r"""
    ^[^.:]{0,60}?
    \b(alt\s+text|description)\b
    [^.:]{0,20}?
    :\s*
""", re.IGNORECASE | re.VERBOSE)

# How many sentences may be dropped from the END. A sign-off runs to a line or
# two ("Let me know if you want it shorter"), so a bound costs nothing there.
#
# There is deliberately no bound on the START. A preamble has no natural
# length: the model can reason for several sentences before arriving at the
# description. The aside that shipped on 16 August 2026 was exactly two
# sentences, which the original bound of two-from-each-end caught by a margin
# of nothing; a third would have gone out with it.
#
# Removing the bound does not risk the interior, because stripping still stops
# at the first sentence that is not meta. The case it changes is a response
# that is meta the whole way down: that now comes back empty rather than
# half-salvaged, falls short of MIN_CHARS in describe(), and the caller uses
# the citation. A plain citation is the right outcome there.
_MAX_STRIPPED_TRAILING = 2


def _is_meta(sentence):
    return bool(_META.search(sentence) or _FIRST_PERSON.search(sentence))


def _strip_meta(text, log=print):
    """Drop operator-facing remarks from the ends of a description.

    Only the ends, so a real description is never cut out of the interior:
    the front strips until it meets a sentence that is not meta, and the back
    is bounded by `_MAX_STRIPPED_TRAILING`. If everything is meta the result
    comes back empty, falls short of MIN_CHARS in describe() and the caller
    uses the citation, which is the right outcome. Better a plain citation
    than a salvaged fragment of a hallucinated answer.
    """
    dropped = []
    lead = _LEAD_IN.match(text)
    if lead:
        dropped.append(lead.group(0).strip())
        text = text[lead.end():]

    parts = [p for p in _SENTENCE.split(text) if p.strip()]

    start, end = 0, len(parts)
    while start < end and _is_meta(parts[start]):
        start += 1
    while end > start and (len(parts) - end) < _MAX_STRIPPED_TRAILING \
            and _is_meta(parts[end - 1]):
        end -= 1

    dropped += parts[:start] + parts[end:]
    if not dropped:
        return text

    log(f'  (image description: dropped operator aside: '
        f'{" ".join(dropped)!r})')
    return ' '.join(parts[start:end]).strip()

# ---------------------------------------------------------------------------
# The verification pass
#
# Measured 27 August 2026 across all 118 descriptions the three model-written
# bots had shipped since 16 August: 35 of them (30%) asserted something the
# image does not support, and 6 (5%) named an object that is not in the frame
# at all — a newspaper in a cartoon figure's empty hands, a mountain behind a
# city lot, a "small figure walking" that is a tree trunk.
#
# ⚠️ THE PROMPT RULES ABOVE ARE NOT ENOUGH, and that is measured rather than
# assumed. everylibrary_describe.py already carries an explicit, well-argued
# rule against guessing storey counts; in the same sweep it guessed wrong five
# times in twenty-nine images. A rule written at a failure did not stop the
# failure. Asking more carefully is not a fix, so the description is now
# checked against the image before it ships.
#
# ⚠️ The check asks a DIFFERENT QUESTION from the one that wrote the text —
# locate each asserted thing, rather than judge the sentence. A verifier asked
# "is this description good?" largely agrees with itself. It is also
# deliberately NOT given the caption: the caption is where imported facts come
# from, and a verifier holding it will happily confirm them.
#
# ⚠️ It runs INSIDE describe(), on the bare description. Callers prepend the
# citation ("Seoul Metropolitan Archives, 1965.") and everylibrary appends a
# note from Commons, and both assert things no image can show — an archive's
# name, a date, "the council contact center out the back". Verifying the
# assembled alt reports every one of those as unsupported. The sweep that led
# to this made exactly that mistake and scored three human-written claims as
# model hallucinations before they were caught by hand.
#
# ⚠️ A CHECK THAT COULD NOT BE MADE DOES NOT BLOCK THE POST. It logs that the
# description is unverified and ships it anyway, which is this module's
# standing contract: it must never be the thing that ends a run. Falling back
# to the citation whenever the verifier has a bad morning would trade a rare
# wrong description for a frequent absent one, and the absent one is worse for
# every reader on every other day. What it must never do is report that
# silence as a pass — hence the unparseable-reply guard in _unsupported().
#
# ⛔⛔ WHAT THIS DOES NOT CATCH, measured on the image that prompted it.
# Old Seoul's post of 27 August 2026 (3mtzk5pwq4w2q, Deoksu Palace in snow)
# shipped "a small figure walking along a cleared path". There is no figure:
# it is the trunk of a snow-laden tree, about 36px tall in a 539x447 frame,
# roughly 8% of the height. The verifier was run against that image and
# reported "FOUND | small figure walking | tiny person standing/walking on the
# cleared path in front of the building". It shares the describer's blind spot
# exactly, and the rewritten prompt above does not help either: a fresh
# describe() on the same image still says "a small figure standing".
#
# So this check is worth having and is NOT a solution to the general problem.
# It catches the large classes measured in the same sweep — miscounted people
# and storeys, wrong roofs, wrong colors, a canopy borrowed from a bus
# shelter next door — and it does not catch a small, low-contrast feature that
# genuinely looks like the thing it is mistaken for. The 30% figure above is
# therefore a FLOOR, not a measurement of everything wrong: the snow post was
# scored clean by the very sweep that produced it.
#
# What actually settled that image was cropping the region and looking at it
# enlarged, which nothing in this path does. If this failure class is ever
# worth closing, that is the direction — magnify the asserted detail and ask
# again — not a third rewording of either prompt.

VERIFY_PROMPT = """Look at the image ./{name}

A description of that image appears at the end of this message. Your job is to LOCATE things in the image, not to judge the writing.

Take every concrete thing the description asserts is present — each object, person, structure, material, number or feature — and for each one output exactly one line:

FOUND | <the claim in a few words> | <where it is in the image>
ABSENT | <the claim in a few words> | <what is actually there instead>

Rules:
- Be strict. If you cannot point to it, it is ABSENT. Do not give it the benefit of the doubt.
- Look carefully at small and low-contrast details before calling them FOUND.
- Judge presence only. Never judge wording, style, tone or completeness.
- Skip any claim about the medium itself, e.g. "black-and-white photograph".
- Output only those lines and nothing else.

Description: {alt}"""

# Appended to the original prompt for the one retry. It names what failed, so
# the second attempt is not simply a reroll of the same dice.
_REDO = """

An earlier attempt at this description asserted the following, and a check against the image could not find them:
{bad}

Write the description again, leaving out anything you cannot actually resolve. A shorter, safer description is the right answer here."""

# One retry, not more. A second failure means the model keeps seeing something
# that is not there, and a third roll of the same dice is not evidence.
MAX_REDESCRIBE = 1

_ABSENT_LINE = re.compile(r'^\s*ABSENT\s*\|\s*(.+?)\s*(?:\||$)')
_FOUND_LINE = re.compile(r'^\s*FOUND\s*\|')


def _unsupported(image_bytes, text, *, env, model, timeout, suffix, log):
    """Claims in `text` that cannot be located in the image.

    [] when every claim checks out, a list of claims when they do not, and
    None when the check could not be made at all.

    ⚠️ None is NOT [] and callers must not treat it as one. A failed call, a
    timeout and a model that ignored the format would all yield an empty
    list of ABSENT lines, which reads exactly like a clean verification: the
    dangerous state and the healthy one producing identical silence.
    """
    # ⚠️ Unlike _generate() below, this does NOT wait out a spent quota through
    # limit_guard, and the asymmetry is deliberate. A description is worth a
    # short wait because the alternative is a bare citation; a VERIFICATION is
    # not, because the alternative is the description shipping unverified,
    # which is a good outcome held an hour late. The cost is that a quota
    # exhausted mid-run puts every description through unchecked — logged each
    # time, never silent, but worth knowing before reading a quiet log as a
    # clean bill of health.
    try:
        with tempfile.TemporaryDirectory() as td:
            name = f'image{suffix}'
            Path(td, name).write_bytes(image_bytes)
            r = subprocess.run(
                ['claude', '-p', '--model', model,
                 VERIFY_PROMPT.format(name=name, alt=text)],
                capture_output=True, text=True, env=env, cwd=td,
                timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log(f'  (description not verified: {exc.__class__.__name__})')
        return None

    if r.returncode != 0:
        err = (r.stderr or r.stdout or '').strip()[:200] or '(no output)'
        log(f'  (description not verified, exit {r.returncode}: {err})')
        return None

    lines = r.stdout.strip().splitlines()
    absent = [m.group(1) for m in (_ABSENT_LINE.match(ln) for ln in lines) if m]
    if not absent and not any(_FOUND_LINE.match(ln) for ln in lines):
        log('  (description not verified: reply carried no verdict lines)')
        return None
    return absent


def _generate(image_bytes, prompt, *, env, model, timeout, suffix, log):
    """One generation attempt: the model's reply, cleaned, or None."""
    global _limit_waited
    retried = False
    while True:
        try:
            with tempfile.TemporaryDirectory() as td:
                name = f'image{suffix}'
                Path(td, name).write_bytes(image_bytes)
                r = subprocess.run(
                    ['claude', '-p', '--model', model, prompt],
                    capture_output=True, text=True, env=env, cwd=td,
                    timeout=timeout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            log(f'  (image description unavailable: {exc.__class__.__name__})')
            # A verification failure already gets one retry, below. A raw
            # call failure did not, so a single transient timeout or network
            # blip was enough to drop the description and ship a bare
            # citation. One retry here matches the tolerance _unsupported()'s
            # caller already has, rather than adding a new one.
            if not retried:
                retried = True
                continue
            return None

        if r.returncode != 0:
            err = (r.stderr or r.stdout or '').strip()[:200] or '(no output)'
            # A spent quota is the one failure here worth waiting out. Falling
            # back is silent by design, so a quota exhausted mid-run strips the
            # descriptions off a whole post and nothing says so at the time.
            # Anything else still falls back at once: this module must never be
            # the thing that ends a run.
            if not _limit_waited and limit_guard.is_usage_limit(err):
                _limit_waited = True
                if limit_guard.wait_for_reset(err, budget_s=LIMIT_BUDGET_S,
                                              log=lambda m: log(f'  {m}')):
                    continue
            log(f'  (image description failed, exit {r.returncode}: {err})')
            return None
        break

    text = re.sub(r'^```[a-z]*\n?|\n?```$', '', r.stdout.strip()).strip()
    text = _EMOJI.sub('', text)
    text = ' '.join(text.split()).strip().strip('"').strip()

    if 'CANNOT_SEE' in text:
        log('  (image description: model reported it could not read the image)')
        return None

    text = _strip_meta(text, log=log)

    if not (MIN_CHARS <= len(text) <= MAX_CHARS):
        log(f'  (image description rejected: {len(text)} chars, outside '
            f'{MIN_CHARS}-{MAX_CHARS})')
        return None
    return text


def describe(image_bytes, context='', *, env=None, model=MODEL,
             timeout=TIMEOUT, suffix='.jpg', log=print, verify=True):
    """One or two sentences describing the image, or None if unavailable.

    `env` is passed straight to the subprocess, so callers hand in whatever
    they already use to put the Keychain token in front of claude -p.

    The description is checked against the image before it is returned (see
    the block above). A description carrying a claim the check cannot find is
    regenerated once with that claim named, and dropped if it fails again —
    the caller then falls back to its citation, which is the right outcome:
    a plain attribution beats a confident sentence about a person who is not
    in the photograph, because the reader cannot tell the difference.

    `verify=False` skips the check. It exists for callers doing a dry run and
    for the tests, not as a performance option.
    """
    # A caller with no image yet (a dry run that skips the download, a fetch
    # that fell through) gets None, not a TypeError: this module exists to
    # improve alt text, and it must never be the thing that ends a run.
    if not image_bytes:
        log('  (image description skipped: no image bytes)')
        return None

    prompt = _PROMPT.format(name=f'image{suffix}', context=context or '(none)')
    kw = dict(env=env, model=model, timeout=timeout, suffix=suffix, log=log)

    text = _generate(image_bytes, prompt, **kw)
    if text is None or not verify:
        return text

    for attempt in range(MAX_REDESCRIBE + 1):
        bad = _unsupported(image_bytes, text, **kw)
        if bad is None:
            log('  (description shipped UNVERIFIED: the check could not be made)')
            return text
        if not bad:
            return text
        log(f'  (description failed verification: {"; ".join(bad)})')
        if attempt == MAX_REDESCRIBE:
            break
        redone = _generate(
            image_bytes,
            prompt + _REDO.format(bad='\n'.join(f'- {b}' for b in bad)), **kw)
        if redone is None:
            break
        text = redone

    log('  (image description dropped: could not be verified against the image)')
    return None
