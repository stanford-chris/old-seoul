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

The model runs with cwd set to the temp directory holding the one image, so
the only file it can reach by a bare name is the one it is being asked about.
"""

import re
import subprocess
import tempfile
from pathlib import Path

# Vision quality is the whole point of this module, and the volume is tiny
# (a handful of calls a day), so this does not drop to haiku the way the
# translation step does.
MODEL = 'claude-sonnet-5'
TIMEOUT = 120
MAX_CHARS = 600
MIN_CHARS = 20

_PROMPT = """Write alt text for a blind reader of a social media post, describing the image ./{name}

Describe what is actually VISIBLE: the subject, the setting, the composition, any notable detail. One or two sentences, 40 words at most.

Rules:
- Describe only what you can see. Never state names, dates, places or events that are not visually evident, however likely they seem.
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
        log(f'  (image description failed, exit {r.returncode}: {err})')
        return None

    text = re.sub(r'^```[a-z]*\n?|\n?```$', '', r.stdout.strip()).strip()
    text = _EMOJI.sub('', text)
    text = ' '.join(text.split()).strip().strip('"').strip()

    if 'CANNOT_SEE' in text:
        log('  (image description: model reported it could not read the image)')
        return None
    if not (MIN_CHARS <= len(text) <= MAX_CHARS):
        log(f'  (image description rejected: {len(text)} chars, outside '
            f'{MIN_CHARS}-{MAX_CHARS})')
        return None
    return text
