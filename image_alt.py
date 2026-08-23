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
# is catalogue metadata written by a human. Labelling it would be a false
# claim in the opposite direction.
DISCLOSURE = 'A.I.-generated description:'

_PROMPT = """Write alt text for a blind reader of a social media post, describing the image ./{name}

Describe what is actually VISIBLE: the subject, the setting, the composition, any notable detail. One or two sentences, 40 words at most.

Rules:
- Describe only what you can see. Never state names, dates, places or events that are not visually evident, however likely they seem.
- That includes people: do not assign gender, age or role from clothing, hair or the caption. Write "four people" or "a figure seated on the ground" unless the photograph itself puts it beyond doubt.
- Attribute a detail only to the figures it is actually visible on. If three of four are barefoot, say three.
- Open by naming the medium where it is not obvious, e.g. "Black-and-white photograph" or "Pen-and-ink illustration".
- Do not restate the caption below. It is read out separately, and repeating it is the flaw this replaces. Use it only to avoid contradicting what is known.
- UK English. No emoji, no markdown, no surrounding quotation marks.
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


def describe(image_bytes, context='', *, env=None, model=MODEL,
             timeout=TIMEOUT, suffix='.jpg', log=print):
    """One or two sentences describing the image, or None if unavailable.

    `env` is passed straight to the subprocess, so callers hand in whatever
    they already use to put the Keychain token in front of claude -p.
    """
    # A caller with no image yet (a dry run that skips the download, a fetch
    # that fell through) gets None, not a TypeError: this module exists to
    # improve alt text, and it must never be the thing that ends a run.
    if not image_bytes:
        log('  (image description skipped: no image bytes)')
        return None

    global _limit_waited
    while True:
        try:
            with tempfile.TemporaryDirectory() as td:
                name = f'image{suffix}'
                Path(td, name).write_bytes(image_bytes)
                prompt = _PROMPT.format(name=name, context=context or '(none)')
                r = subprocess.run(
                    ['claude', '-p', '--model', model, prompt],
                    capture_output=True, text=True, env=env, cwd=td,
                    timeout=timeout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            log(f'  (image description unavailable: {exc.__class__.__name__})')
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
