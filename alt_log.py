"""Append-only log of the alt text each post shipped, plus a viewer for it.

Mirrors seoul-index's card_history.jsonl and the reasoning behind it. As of
August 2026 this bot's alt text is generated from the image by a model and
goes out unreviewed on a schedule, so there needs to be one place to skim a
week of it and catch a dud: a description that drifted, that read text off the
image wrongly, or that quietly fell back to the old citation every time
because the model call was failing.

That last case is the one worth watching. A failed call degrades silently by
design, which is right for keeping posts going out and wrong for noticing, so
each record carries whether the alt was generated or fell back.

Writing is best-effort. The post is already live by the time this runs, so a
logging failure is warned about and swallowed, never raised: a lost log line
must not turn a successful post into a crashed run.
"""

import json


def post_url(handle, uri):
    """A bsky.app permalink from an at:// URI, or None if there isn't one."""
    if not uri:
        return None
    rkey = uri.rsplit('/', 1)[-1]
    return f'https://bsky.app/profile/{handle}/post/{rkey}' if rkey else None


def append(path, record, log=print):
    """Append one JSONL record. Never raises."""
    try:
        with path.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + '\n')
    except Exception as exc:                # noqa: BLE001 - never fail a live post
        log(f'(alt log failed: {exc})')


def read(path):
    """Every readable record. A torn final line from a crash mid-write is
    skipped rather than treated as corruption of the whole file."""
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding='utf-8').splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def tail(path, n, log=print):
    """Print the last n logged posts, newest last, for eyeballing recent alt
    text. Read-only: no archive, no model, no post, so it is safe to run at any
    time, including while a scheduled post is composing."""
    recs = read(path)
    if not recs:
        log(f'No alt log yet at {path} — written after the first real post.')
        return
    shown = recs[-n:]
    generated = sum(1 for r in recs if r.get('generated'))
    log(f'Last {len(shown)} of {len(recs)} post(s); '
        f'{generated}/{len(recs)} had generated alt text.')
    for r in shown:
        head = f'\n{r.get("at", "?")}  {r.get("title", "")}'.rstrip()
        head += '' if r.get('generated') else '  (FELL BACK — not generated)'
        log(head)
        if r.get('url'):
            log(f'  {r["url"]}')
        alts = r.get('alts') or []
        for i, alt in enumerate(alts, 1):
            prefix = f'  {i}/{len(alts)} ' if len(alts) > 1 else '  '
            log(f'{prefix}{alt}')
