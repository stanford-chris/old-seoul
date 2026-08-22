#!/usr/bin/env python3
"""
Post one historical Seoul photo to Bluesky (@oldhanyang.bsky.social).
Picks a random unposted item from the combined photo pools, translates with
Claude, posts with the image, and marks it as posted in its own pool.

Three pools (see SOURCES):
  - seoul_archive.json   Seoul Metropolitan Archives, 1950s-90s municipal
                         photography, dated, with Korean descriptions.
  - seoul_dryplate.json  National Museum of Korea's 조선총독부박물관 glass plates,
                         1909-1945, mostly undated and title-only. 공공누리
                         제1유형, so the museum credit is mandatory.
  - seoul_gazette.json   서울시보, the 1982-83 city gazette. Only its 107
                         cartoons and comic strips are posted (see
                         GAZETTE_STRIPS), and an item is a region of a page
                         rather than a file, so its picture is cropped out of
                         the page scan on the way through.

Requires:
    security add-generic-password -a "oldhanyang.bsky.social" -s "seoulbot-bluesky" -w

Usage:
    python3 seoul_post.py                      # post one item
    python3 seoul_post.py --dry-run            # translate and format, no post
    python3 seoul_post.py --source gazette     # restrict the pool to one source
"""

import json
import os
import random
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from atproto import Client, client_utils

import alt_log
import image_alt
import limit_guard
import net_guard

ARCHIVE = Path(__file__).parent / 'seoul_archive.json'
DRYPLATE = Path(__file__).parent / 'seoul_dryplate.json'
GAZETTE = Path(__file__).parent / 'seoul_gazette.json'
# One JSONL line per posted item, recording the alt text that shipped and
# whether it was generated or fell back. Written only on a real post, and
# best-effort: see alt_log.
ALT_LOG = Path(__file__).parent / 'alt_history.jsonl'
HANDLE = 'oldhanyang.bsky.social'
KEYCHAIN_SERVICE = 'seoulbot-bluesky'

# Hashtags, per source rather than global since 22 August 2026. #photography
# belongs on a photograph; the gazette posts a pen-and-ink cartoon or a comic
# strip, so it takes the same set without it (user's instruction: "post
# #photography with photography posts, but remove elsewhere").
PHOTO_TAGS = [('photography', 'photography'), ('seoul', 'seoul'),
              ('korea', 'korea'), ('history', 'history'),
              ('서울', '서울'), ('역사', '역사')]
DRAWING_TAGS = [t for t in PHOTO_TAGS if t[0] != 'photography']

# The gazette's two recurring cartoon slots, and the English name each is
# published under. Both are by 정운경, whose name is therefore NOT in the
# English title: it would repeat on all 107 posts. It reaches the reader in the
# Korean line, which is the source's own title, verbatim as every pool's is.
#
# ⚠️ This mapping IS the pool filter. The gazette holds 2,573 articles and the
# other 2,466 are newspaper text, whose crop is a picture of Korean type rather
# than a picture — a different bot, not a different item. Adding a key here
# starts posting that slot, so add one only with the crop checked: 28% of
# gazette boxes contain part of a neighbouring article, though all 107 of these
# are clean.
GAZETTE_STRIPS = {
    '서울만평': 'Cartoon',
    '주사 새서울씨': 'Clerk Mr. New Seoul',
}

# The three pools. They are posted from a single combined pool, so each
# source's share of the feed is just its share of the unposted items.
#
# `dated`: the Seoul Metropolitan Archives records carry a usable year, so their
# posts lead with it. The glass plates mostly do not — only 284 of 1,452 have a
# 촬영 연도 at all — so a date alone would be "date unknown" four posts in five.
# Those posts lead with the district, which is real information the header never
# carried (the whole feed is Seoul, so the city alone said nothing; see the
# 2026-07-26 header change), and follow it with the year or, where the catalogue
# has none, "date unknown". See post_header.
SOURCES = {
    'archives': {
        'path': ARCHIVE,
        'link_label': '🗃️ Seoul Metropolitan Archives',
        'item_url': 'https://archives.seoul.go.kr/item/{id}',
        'alt_tail': 'Seoul Metropolitan Archives',
        'alt_credit': '서울기록원',
        'dated': True,
        'tags': PHOTO_TAGS,
    },
    'dryplate': {
        'path': DRYPLATE,
        # 공공누리 제1유형 (출처표시): reuse is free, attribution is mandatory,
        # so the museum credit is not decoration and must not be dropped.
        'link_label': '🗃️ National Museum of Korea',
        'item_url': 'https://www.museum.go.kr/dryplate/searchplate_view.do?relicnum={id}',
        'alt_tail': 'National Museum of Korea',
        'alt_object': 'glass plate',
        'alt_period': 'colonial-era',
        'alt_credit': '국립중앙박물관 유리건판',
        'dated': False,
        'tags': PHOTO_TAGS,
    },
    'gazette': {
        'path': GAZETTE,
        'link_label': '🗃️ Seoul Metropolitan Archives',
        # No format string: a gazette record carries its own detail_url, which
        # needs three query parameters rather than one id. See item_link.
        'item_url': None,
        'alt_tail': 'Seoul Metropolitan Archives',
        # Both a cartoon and a comic strip are honestly 'a city gazette'.
        # Naming the medium is image_alt's job: its prompt already asks for
        # an opener like "Pen-and-ink illustration" where it is not obvious.
        'alt_object': 'city gazette',
        'alt_credit': '서울기록원 서울시보',
        # Every record carries an exact publication date, so the header is the
        # day itself and the model is never asked to find one.
        'dated': True,
        'tags': DRAWING_TAGS,
        # An item is a region of a page: the picture has to be cut out of the
        # page scan after it is fetched. See crop_article.
        'crop': True,
        # Cartoons and strips only, and only where the archives gave the
        # article a box to cut to.
        'select': lambda it: it.get('tag') in GAZETTE_STRIPS and bool(it.get('box')),
        # Target share of the feed. Without it this pool is 107 items against
        # 10,956, so a cartoon would surface about once in 103 posts — every
        # seven weeks at two a day, and fifteen years to get through them.
        # At 1 in 10 it is a cartoon every five days and the set lasts about
        # eighteen months. See draw_weights.
        'share': 0.10,
    },
}

# Refuse anything unrecognised. Until August 2026 this was a bare membership
# test, so an unknown flag (`--help` above all) fell through to a LIVE post:
# the same trap that published a real thread from seoul-index on 20 Jul 2026.
_KNOWN_ARGS = {'--dry-run', '--tail', '--source'}


def _tail_n(argv):
    """N for `--tail [N]` (print recent alt text and exit), or None if absent.
    N defaults to 10 and a bare integer right after --tail overrides it."""
    if '--tail' not in argv:
        return None
    i = argv.index('--tail')
    if i + 1 < len(argv) and argv[i + 1].isdigit():
        return max(1, int(argv[i + 1]))
    return 10


def _source_filter(argv):
    """Key for `--source <name>` (restrict the pool to one source), or None.

    A testing and backfill aid: without it the pool is all sources combined.
    """
    if '--source' not in argv:
        return None
    i = argv.index('--source')
    if i + 1 >= len(argv) or argv[i + 1] not in SOURCES:
        sys.exit(f'--source needs one of: {" ".join(sorted(SOURCES))}.')
    return argv[i + 1]


if __name__ == '__main__':
    # Indices of values belonging to a preceding flag, so they are not mistaken
    # for unknown arguments.
    _skip = set()
    if '--tail' in sys.argv:
        _t = sys.argv.index('--tail')
        if _t + 1 < len(sys.argv) and sys.argv[_t + 1].isdigit():
            _skip.add(_t + 1)
    if '--source' in sys.argv:
        _s = sys.argv.index('--source')
        if _s + 1 < len(sys.argv):
            _skip.add(_s + 1)
    _unknown = [a for j, a in enumerate(sys.argv[1:], 1)
                if a not in _KNOWN_ARGS and j not in _skip]
    if _unknown:
        sys.exit(f'Unknown argument(s): {" ".join(_unknown)}. '
                 f'Recognised: {" ".join(sorted(_KNOWN_ARGS))} [N]. '
                 f'Refusing to run (a bare run posts live).')

DRY_RUN = '--dry-run' in sys.argv
TAIL_N = _tail_n(sys.argv)
SOURCE_FILTER = _source_filter(sys.argv)

MAX_POST_CHARS = 290  # leave 10 char buffer under Bluesky's 300 limit

# Topic cooldown: don't post an item whose topic emoji matches any of the last
# N posted topics, so ceremony/construction/official shots don't come in runs.
CATEGORY_COOLDOWN = 4

# How many candidates to try before giving up, when their pictures will not
# load. About 3% of the archive pool is dead (see the draw loop in main), so
# five consecutive failures is ~1 in 350 million by bad luck alone: reaching
# this limit means the archive host is down, not that the draw was unlucky.
DRAW_ATTEMPTS = 5


def keychain_password(account, service):
    result = subprocess.run(
        ['security', 'find-generic-password', '-a', account, '-s', service, '-w'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(
            f'No Keychain password for account="{account}" service="{service}".\n'
            f'Add it with:\n'
            f'  security add-generic-password -a "{account}" -s "{service}" -w'
        )
    return result.stdout.strip()


CLAUDE_TOKEN_ACCOUNT = 'seoulbot'
CLAUDE_TOKEN_SERVICE = 'claude-oauth-token'

# Hard ceiling on each `claude -p` call. Without it a wedged CLI hangs the
# launchd job indefinitely (the failure mode seen in seoul-index and
# scan_filer before they grew the same guard, July 2026).
CLAUDE_TIMEOUT = 300


def claude_env():
    """Env for the `claude -p` subprocess.

    If a long-lived OAuth token (from `claude setup-token`) is stored in the
    Keychain, inject it as CLAUDE_CODE_OAUTH_TOKEN so the headless launchd job
    doesn't depend on the interactive login's short-lived token (which expires
    intra-day and 401s). Falls back to the ambient environment if absent, so
    manual runs with a logged-in CLI still work.
    """
    env = os.environ.copy()
    result = subprocess.run(
        ['security', 'find-generic-password',
         '-a', CLAUDE_TOKEN_ACCOUNT, '-s', CLAUDE_TOKEN_SERVICE, '-w'],
        capture_output=True, text=True
    )
    if result.returncode == 0 and result.stdout.strip():
        env['CLAUDE_CODE_OAUTH_TOKEN'] = result.stdout.strip()
    return env


def write_json_atomic(path, data, **dumps_kwargs):
    """Write JSON via a sibling temp file and an atomic rename, so a crash
    mid-write can never leave a truncated archive or state file behind."""
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(data, **dumps_kwargs))
    os.replace(tmp, path)


# The two pools store different field names for the same things, so everything
# downstream goes through these accessors rather than indexing items directly.
def item_id(item):
    return item.get('id') or item.get('relicnum')


def item_desc(item):
    """Korean description, or '' where the source records none.

    The glass plates have no description field at all: the catalogue gives a
    title, a subject path and a place, and nothing prose-like. That empty string
    is load-bearing — translate() must not be asked to write a description from
    nothing, or it invents one.

    A gazette record's equivalent is `text`, the archives' own transcription of
    the article. For a cartoon that is its caption; for a strip, its speech
    bubbles.
    """
    return item.get('description') or item.get('text') or ''


def item_year(item):
    return item.get('year') or ''


def item_images(item):
    """Image URLs as a list. The glass plates are one plate, one image.

    A gazette record points at the whole page scan, which is not what gets
    posted: crop_article cuts the article out of it after the fetch.
    """
    if item.get('images'):
        return item['images']
    if item.get('page_image'):
        return [item['page_image']]
    return [item['image_url']] if item.get('image_url') else []


def item_date_en(item):
    """'20 January 1982' from a gazette record's own date, or '' if it has none.

    Only the gazette knows the exact day for certain, and it knows it for every
    record, so that day is taken from the data rather than asked of the model.
    The photo pools keep the existing route: translate() reports a precise date
    only where the Korean caption states one.
    """
    raw = (item.get('date') or '').strip()
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
        return ''
    d = datetime.strptime(raw, '%Y-%m-%d')
    # No leading zero on the day: UK style is "1 February", not "01 February".
    return f'{d.day} {d.strftime("%B %Y")}'


def item_link(item, source):
    """Where the credit links. A record that carries its own detail_url wins:
    a gazette article is addressed by three query parameters, not by an id."""
    if item.get('detail_url'):
        return item['detail_url']
    return source['item_url'].format(id=item_id(item))


# Seoul's 18 districts as they appear in the glass-plate region field, with the
# city's own English names. Used for the header line on undated plates.
DISTRICTS_EN = {
    '강남구': 'Gangnam-gu', '강동구': 'Gangdong-gu', '광진구': 'Gwangjin-gu',
    '노원구': 'Nowon-gu', '도봉구': 'Dobong-gu', '동대문구': 'Dongdaemun-gu',
    '동작구': 'Dongjak-gu', '마포구': 'Mapo-gu', '서대문구': 'Seodaemun-gu',
    '서초구': 'Seocho-gu', '성동구': 'Seongdong-gu', '성북구': 'Seongbuk-gu',
    '송파구': 'Songpa-gu', '용산구': 'Yongsan-gu', '은평구': 'Eunpyeong-gu',
    '종로구': 'Jongno-gu', '중구': 'Jung-gu', '중랑구': 'Jungnang-gu',
}


def item_district(item):
    """English district name from the region path, or '' if it has none.

    Region reads '한국_서울특별시_종로구'. Eleven of the 1,452 records stop at the
    city, and those simply get no header line rather than a made-up one. Note
    the region field is modern administrative geography while titles use the
    period names, so the two can legitimately disagree (우이리 is filed under
    Seoul but titled 경기 고양).
    """
    parts = (item.get('region') or '').split('_')
    return DISTRICTS_EN.get(parts[2], '') if len(parts) > 2 else ''


def translate(title_ko, description_ko, year):
    """Translate Korean title and description to concise English via claude -p.

    With no Korean description (the glass plates), only the title is translated
    and the description comes back empty: asking for a description of a photo
    the model cannot see would be invention, not translation.
    """
    if not description_ko:
        return translate_title_only(title_ko)
    prompt = (
        f'Translate this Korean text from a historical Seoul photo archive into concise English.\n\n'
        f'Title (Korean): {title_ko}\n'
        f'Description (Korean): {description_ko}\n'
        f'Year: {year or "unknown"}\n\n'
        f'Rules:\n'
        f'- Title translation: accurate, natural English, max 60 characters\n'
        f'- Description translation: one clear sentence, max 100 characters\n'
        f'- Description must not restate or repeat the title — add new information (who, what is happening, context)\n'
        f'- Do not add interpretation or extra context\n'
        f'- UK English spelling: modernisation not modernization, harbour not harbor, centre not center\n'
        f'- Write "percent" as one word, never "per cent"\n'
        f'- Quote with double quotation marks, never single ones — a "comfort women" camp, not a \'comfort women\' camp\n'
        f'- Use British date format for any dates (e.g. 9 June 1972, not June 9 1972 or 06/09/1972)\n'
        f'- Write quantities of one thousand or more with thousands separators (e.g. 3,000 officials, 25,000 spectators), but never put a separator in a year (write 1972, not 1,972)\n'
        f'- date: the precise calendar date of the event ONLY if the Korean source explicitly states a specific day (e.g. 1968년 7월 17일, or 7월 17일). Format British: "17 July 1968". If a day and month are given without a year, complete it using the known Year above. If no specific day is stated, return an empty string. Never infer or guess a day.\n'
        f'- Return JSON only: {{"title": "...", "description": "...", "date": "..."}}'
    )
    return _claude_json(prompt)


def translate_title_only(title_ko):
    """Translate a bare catalogue title, with no description invented.

    The glass-plate titles are museum catalogue entries ('창덕궁 돈화문 공포'), so
    the job is a faithful rendering of a named structure, not prose.
    """
    prompt = (
        f'Translate this Korean title from a museum catalogue of historical '
        f'Seoul photographs into concise English.\n\n'
        f'Title (Korean): {title_ko}\n\n'
        f'Rules:\n'
        f'- Accurate, natural English, max 60 characters\n'
        f'- These are catalogue entries for buildings, monuments and sites. '
        f'Keep Korean proper nouns in Revised Romanisation (Gyeongbokgung, '
        f'Donhwamun), and translate the architectural terms that follow them\n'
        f'- UK English spelling: harbour not harbor, centre not center\n'
        f'- Quote with double quotation marks, never single ones\n'
        f'- Do not add interpretation, context or anything not in the Korean\n'
        f'- Return JSON only: {{"title": "..."}}'
    )
    out = _claude_json(prompt)
    # Downstream expects all three keys; only the title is ever populated here.
    return {'title': out.get('title', ''), 'description': '', 'date': ''}


def translate_gazette(item):
    """Translate one gazette cartoon or comic strip.

    ⚠️ THE MODEL CANNOT SEE THE DRAWING, and this prompt must never let it
    pretend otherwise. What it gets is the archives' transcription: a caption
    for a cartoon, the speech bubbles for a strip. Everything it writes has to
    come out of that text. The temptation is real and specific — a caption
    reading "지하철 급진전" invites "Chung Woon-kyung draws the diggings as a
    star-shaped crater", which is a perfectly good sentence about a picture
    nobody in this function has looked at. The drawing does get described, by
    image_alt.describe(), which is shown the actual pixels.

    The strip's English name is prefixed in code, not asked for, so all 107
    posts name the slot identically. The model supplies only what follows the
    colon.
    """
    label = GAZETTE_STRIPS[item['tag']]
    prompt = (
        f'This is one item from 서울시보, the Seoul city government newspaper of '
        f'1982-83. It is a {"political cartoon" if item["tag"] == "서울만평" else "four-panel comic strip"} '
        f'published under the running title "{item["tag"]}" ({label}).\n\n'
        f'Below is the archive\'s transcription of the text printed in it — '
        f'the caption, and for a strip the speech bubbles. You CANNOT see the '
        f'drawing itself.\n\n'
        f'Transcription (Korean):\n{item_desc(item)}\n\n'
        f'Rules:\n'
        f'- Write ONLY from the transcription. Never describe the drawing, the '
        f'characters, the composition or the artist\'s style: you have not seen '
        f'them, and inventing them is the one thing this must not do.\n'
        f'- gist: a short English phrase, max 45 characters, giving what this '
        f'one is about. It follows a colon after "{label}", so do not repeat '
        f'that name. Start it with a capital letter.\n'
        f'- description: one sentence, max 100 characters, rendering what the '
        f'transcribed lines actually SAY — for a strip, what happens in it and '
        f'what the characters tell each other. Not a summary of the topic: '
        f'never open with "Discussion of", "A cartoon about", "Depicts" or any '
        f'other framing of that kind. It must add something the gist does not '
        f'already carry; if it cannot, return an empty description rather than '
        f'padding one out.\n'
        f'- UK English spelling: modernisation not modernization, harbour not '
        f'harbor, centre not center\n'
        f'- Write "percent" as one word, never "per cent"\n'
        f'- Quote with double quotation marks, never single ones\n'
        f'- Use British date format for any dates (e.g. 9 June 1972)\n'
        f'- Write quantities of one thousand or more with thousands separators '
        f'(e.g. 35,000 trees), but never put a separator in a year\n'
        f'- Return JSON only: {{"gist": "...", "description": "..."}}'
    )
    out = _claude_json(prompt)
    gist = (out.get('gist') or '').strip()
    # The label is ours, so a missing gist degrades to the slot's name alone
    # rather than to a stray colon.
    return {
        'title': f'{label}: {gist}' if gist else label,
        'description': (out.get('description') or '').strip(),
        # Never asked for: item_date_en already knows the exact day.
        'date': '',
    }


def _claude_json(prompt):
    """Run `claude -p` and parse a JSON object from its output, with one retry
    on a timeout or malformed JSON, and one wait-and-retry on a spent quota.

    The quota wait deliberately does NOT consume the retry budget: a run that
    waited two hours for a reset should still get its ordinary retry if the
    call then comes back malformed.
    """
    attempt = 0
    limit_waited = False
    while True:
        try:
            result = subprocess.run(
                ['claude', '-p', '--model', 'claude-haiku-4-5-20251001', prompt],
                capture_output=True, text=True, env=claude_env(),
                timeout=CLAUDE_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            if attempt == 0:
                print(f'Warning: claude -p timed out after {CLAUDE_TIMEOUT}s (attempt 1), retrying...')
                attempt += 1
                continue
            raise RuntimeError(
                f'claude -p timed out after {CLAUDE_TIMEOUT}s, twice')
        if result.returncode != 0:
            # The claude CLI writes some errors (e.g. auth 401s) to stdout, not
            # stderr, so include both to keep failures diagnosable.
            err = (result.stderr or result.stdout or '').strip() or '(no output)'
            if limit_guard.is_usage_limit(err) and not limit_waited:
                limit_waited = True
                if limit_guard.wait_for_reset(err):
                    continue
                # Exit 0, not a raise: a spent quota is not a fault in this
                # bot, and a traceback per firing is what made the August 2026
                # network outage unreadable. Silence that persists is caught
                # instead by bot_health_check.py, which alerts when
                # seoul_state.json's last_success_at is over 26 hours old, and
                # a skipped run never touches that stamp.
                sys.exit(0)
            raise RuntimeError(f'claude -p failed (exit {result.returncode}): {err}')
        text = result.stdout.strip()
        text = re.sub(r'^```[a-z]*\n?|\n?```$', '', text).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            if attempt == 0:
                print(f'Warning: malformed JSON from claude -p (attempt 1), retrying...')
                attempt += 1
                continue
            raise RuntimeError(f'claude -p returned invalid JSON after 2 attempts: {repr(text[:200])}')


# A colon followed by a space and a lowercase letter. Times (11:30) and ratios
# have no space after the colon, so they are left alone.
_AFTER_COLON = re.compile(r'(:[  ]+)([a-z])')


def capitalize_after_colon(text):
    """Capitalize the first word after a colon: "Cartoon: The subway races
    ahead", not "Cartoon: the subway races ahead".

    House style (user, 22 August 2026). Enforced here rather than left to the
    prompt, for the reason promote_single_quotes exists: an instruction the
    model follows most of the time still ships the exception. Applies to every
    source, not just the gazette, though the gazette is where the shape is
    routine — its titles are built as "<strip name>: <gist>".
    """
    return _AFTER_COLON.sub(lambda m: m.group(1) + m.group(2).upper(), text)


def promote_single_quotes(text):
    """Rewrite paired single quotation marks as double ones.

    House style quotes with " ", and the translate() prompt says so, but the
    model returns 'like this' often enough that the instruction alone cannot
    be relied on. Only a matched pair is promoted: a lone ' is an apostrophe
    (don't, 1970's, the '60s), and a closer must not be followed by a letter
    or digit, so the apostrophe inside a quoted contraction ('don't stop')
    cannot be mistaken for the closing mark.
    """
    out = list(text)
    open_at = None
    for i, ch in enumerate(out):
        if ch != "'":
            continue
        prev = text[i - 1] if i > 0 else ''
        nxt = text[i + 1] if i + 1 < len(text) else ''
        if ((i == 0) or prev.isspace() or prev in '([{') and not nxt.isspace():
            if open_at is None:
                open_at = i
        elif open_at is not None and not nxt.isalnum():
            out[open_at] = '"'
            out[i] = '"'
            open_at = None
    return ''.join(out)


def educate_quotes(text):
    """Convert straight ASCII quotes/apostrophes to typographic (curly) marks.

    English captions read as crude with straight ' and " (Bluesky renders
    whatever bytes we send). This applies the standard smart-quotes heuristic
    deterministically rather than trusting the LLM to emit curly marks:
    a quote at the start of the text or after whitespace/an opening bracket
    opens (left); anything else closes (right), which also turns in-word
    apostrophes (don't, it's, 1970's) into the right single quote. Only the
    English fields are passed through this — Korean text is left untouched.

    Curly marks in the model's output are flattened to straight ones first, so
    text that arrives already typeset goes through the same pairing rules and
    the same promotion of single-quoted phrases to double.
    """
    if not text:
        return text
    left_single, right_single = '‘', '’'
    left_double, right_double = '“', '”'
    text = (text.replace(left_double, '"').replace(right_double, '"')
                .replace(left_single, "'").replace(right_single, "'"))
    text = promote_single_quotes(text)
    out = []
    for i, ch in enumerate(text):
        prev = text[i - 1] if i > 0 else ''
        nxt = text[i + 1] if i + 1 < len(text) else ''
        opening = (i == 0) or prev.isspace() or prev in '([{'
        if ch == '"':
            out.append(left_double if opening else right_double)
        elif ch == "'":
            # A surviving ' before a digit is an elision, not an opener: the
            # '60s. Anything paired has already become a double quote above.
            out.append(left_single if opening and not nxt.isdigit() else right_single)
        else:
            out.append(ch)
    return ''.join(out)


def group_thousands(text):
    """Add thousands separators to large integers in English captions, without
    touching years (3000 -> 3,000, but 1972 stays 1972).

    The hard case is that a year and a four-digit quantity look identical, so a
    four-digit run in the plausible photographic-era year window (1850-2099) is
    treated as a year and left unseparated; everything else with four or more
    digits gets grouped. That means 3,000/25,000 get commas while 1972 does not.
    The trade-off is that a genuine quantity that happens to fall in 1850-2099
    (e.g. '2000 spectators') is left unseparated rather than risk mangling a
    year — the translate() prompt asks Claude to separate those itself, and an
    already-comma'd number ('2,000') has no four-digit run left for this to
    touch, so the two passes don't fight. Only the English fields go through
    this; Korean text is untouched.
    """
    if not text:
        return text

    def repl(m):
        digits = m.group(0)
        value = int(digits)
        if len(digits) == 4 and 1850 <= value <= 2099:
            return digits
        return format(value, ',')

    return re.sub(r'\d{4,}', repl, text)


# Tags moved to PHOTO_TAGS / DRAWING_TAGS beside SOURCES on 22 August 2026 and
# are now read per source: see source['tags'] in format_post.

# Specific-subject topics, scanned first (most specific wins). General
# government and ceremony are deliberately NOT in this list: they are last-resort
# catch-alls (CATCHALL_EMOJIS below), applied only after this scan fails. In a
# municipal archive a large share of photos are official/ceremonial ("Mayor
# visits X", "opening ceremony for Y"); keeping government/ceremony out of the
# main scan lets those photos take the icon of their actual subject (the school,
# the bridge, the hospital) instead of all collapsing to one 🏛️/🎉 bucket. This
# mirrors the NYT lead bot, where politics/government is a catch-all.
#
# Korean keywords must be long enough to be unambiguous. Korean has no spaces to
# anchor a word boundary, so a short root substring-matches unrelated words: bare
# 문 "gate" is buried in 방문 "visit", 역 "station" in 역사 "history", 강 "river"
# in 건강 "health", 절 "temple" in 계절 "season", 산 "mountain" in 부산/생산. Those
# promiscuous roots are replaced with specific compounds (남대문, 지하철, 한강,
# 남산, 경복궁, ...); some real matches are traded away for far fewer false ones.
SEOUL_EMOJIS = [
    (["song", "music", "musical", "singing", "singer", "choir", "concert", "orchestra", "band",
      "가요", "음악", "노래", "합창", "경연", "연주", "악단"], "🎵"),
    (["construct", "groundbreak", "build", "erect", "foundation", "bridge", "road", "highway", "infrastructure", "tunnel", "overpass",
      "착공", "건설", "공사", "준공", "개통", "교량", "도로"], "🏗️"),
    (["school", "university", "college", "education", "library",
      "학교", "대학", "도서관", "교육"], "🏫"),
    # NB: bare 의원 dropped — it means "clinic" but also "legislator" (국회의원),
    # which would wrongly pick 🏥 now that government is only a catch-all.
    (["hospital", "clinic", "medical", "health",
      "병원", "보건", "의료", "진료소"], "🏥"),
    # NB: bare 절 dropped — buried in 계절/친절/명절/조절.
    (["temple", "shrine", "church", "buddhist", "confucian", "pagoda",
      "사찰", "사원", "불교", "향교", "교회", "성당"], "⛩️"),
    # NB: bare 역 dropped — buried in 역사/지역/역할. 서울역 kept explicitly.
    (["tram", "train", "railway", "subway", "metro", "bus", "transport", "station",
      "전차", "기차", "철도", "지하철", "전철", "버스", "서울역", "교통"], "🚃"),
    # NB: the Korean word 시장 means BOTH "market" and "mayor", so it is
    # deliberately omitted here — "서울시장" (Seoul Mayor) would otherwise pick
    # 🛒. Genuine markets are still caught by the English "market" (translations
    # render 시장-as-market as "market") plus the other Korean shop terms.
    (["market", "shop", "store", "commercial", "trade", "merchant",
      "상점", "상가", "가게", "상업"], "🛒"),
    # NB: bare 산/강 dropped — buried in 부산/생산/계산 and 건강/강남/강연. Specific
    # mountains and the Han river kept instead.
    (["park", "garden", "nature", "mountain", "river", "han river", "forest",
      "공원", "정원", "자연", "숲",
      "남산", "북한산", "관악산", "인왕산", "도봉산", "등산",
      "한강", "하천", "개천", "청계천", "강변"], "🌸"),
    (["portrait", "people", "crowd", "family", "children", "worker", "student",
      "사람", "군중", "가족", "어린이", "학생", "노동자"], "👥"),
    (["neighbourhood", "neighborhood", "village", "district", "residential", "house", "housing",
      "마을", "동네", "주택", "아파트", "골목"], "🏘️"),
    # NB: bare 문/궁 dropped — 문 buried in 방문/신문/문화 (2,700+ items), 궁 in
    # 궁금. Specific gates and palaces kept.
    # 궁궐 is the glass-plate catalogue's own term for a palace, and appears in
    # its subject path rather than the title; unambiguous, so safe to match.
    (["palace", "fortress", "gate", "landmark", "historic", "heritage",
      "대문", "광화문", "독립문", "성문", "성곽", "문화재",
      "경복궁", "창덕궁", "덕수궁", "창경궁", "고궁", "궁궐"], "🏯"),
    (["sport", "stadium", "athletic", "olympic", "race",
      "스포츠", "경기장", "체육", "올림픽"], "🏅"),
    (["airport", "aircraft", "aviation", "flight",
      "공항", "비행기", "항공"], "✈️"),
    (["flood", "fire", "disaster", "relief", "war", "protest",
      "홍수", "화재", "재해", "전쟁", "시위"], "⚠️"),
]

# Last-resort catch-alls, scanned only after every specific subject above fails.
# Government first, then ceremony: a photo that is merely "an official occasion"
# still gets a sensible icon without pre-empting concrete subjects. Demoting
# these from the main scan is what stops official/visit photos — the bulk of a
# municipal archive — from swamping the feed with a single icon.
CATCHALL_EMOJIS = [
    # 서울시장 (Seoul Mayor) is included as an unambiguous compound: a market is
    # never written "서울시장" (that would be 서울 …시장 / 남대문시장), so it dodges the
    # market/mayor collision that keeps bare 시장 out of the lists. It matters
    # because most official archive photos name the mayor, and this lets the
    # Korean-only cooldown path see them as government (the English display path
    # already catches them via "mayor").
    (["government", "office", "city hall", "mayor", "governor", "ministry", "assembly", "court", "police", "fire station",
      "청사", "시청", "구청", "경찰", "소방", "법원", "국회", "서울시장"], "🏛️"),
    (["parade", "ceremony", "festival", "celebration", "rally", "event", "commemor",
      "행사", "축제", "기념", "퍼레이드", "행진", "식전", "식장"], "🎉"),
]


def _emoji_scan(table, combined):
    for keywords, emoji in table:
        for kw in keywords:
            if kw.isascii():
                # Word-boundary match so short English roots (e.g. "road")
                # don't fire inside longer words (e.g. "broadcast").
                if re.search(rf"\b{re.escape(kw)}\b", combined):
                    return emoji
            elif kw in combined:
                # Korean has no spaces to anchor on: substring match. The keyword
                # lists above are curated to keep these roots unambiguous.
                return emoji
    return None


def pick_seoul_emoji(title_en, desc_en, title_ko):
    combined = f"{title_en} {desc_en} {title_ko}".lower()
    # Specific subjects win; general government/ceremony are only a fallback.
    return (_emoji_scan(SEOUL_EMOJIS, combined)
            or _emoji_scan(CATCHALL_EMOJIS, combined)
            or "📷")


def draw_weights(candidates):
    """Per-item draw weights, so a small pool can hold a set share of the feed.

    An unweighted draw gives a source exactly its share of the unposted items,
    which is right for the two photo pools and wrong for the gazette: 107
    cartoons against 10,956 photographs is one cartoon every seven weeks, and
    fifteen years to reach the end of them.

    A source with a `share` gets that fraction of the draw however few items it
    has left, split evenly among them. Everything else divides what remains in
    proportion to its own size, which is what the unweighted draw already did,
    so the photo pools keep their relative standing exactly.

    Three states this has to survive, all of them ordinary:
      - the shared source is absent (exhausted, or every item on cooldown), in
        which case the rest simply split the whole draw;
      - it is the ONLY source (`--source gazette`), where holding back 90% for
        pools that are not here would be meaningless, so the draw goes uniform;
      - shares summing past 1, which starves the rest rather than going
        negative.
    """
    counts = {}
    for it in candidates:
        counts[it['_source']] = counts.get(it['_source'], 0) + 1

    weight = {k: SOURCES[k]['share'] for k in counts if SOURCES[k].get('share')}
    rest = [k for k in counts if k not in weight]
    if rest:
        remainder = max(0.0, 1.0 - sum(weight.values()))
        rest_total = sum(counts[k] for k in rest)
        for k in rest:
            weight[k] = remainder * counts[k] / rest_total

    total = sum(weight.values()) or 1.0
    return [weight[it['_source']] / total / counts[it['_source']]
            for it in candidates]


def item_category(item):
    """Topic emoji for an item derived from its Korean fields only.

    Used at selection time so cooldown candidates don't have to be translated
    first. The 📷 fallback means "uncategorised" — those items are visually
    varied already, so they are never placed on cooldown.

    For the glass plates the classifiable text is the subject path
    ('건축_건물_일반건축_궁궐') rather than free keywords, so it stands in for them.
    That pool is ~79% architecture, so most of it lands on 🏯 and the cooldown
    will hold plates back while a palace shot is recent — deliberately, since
    otherwise the palaces would arrive in runs.
    """
    title_ko = item.get('title') or ''
    desc_ko = item_desc(item)
    kw = item.get('keywords')
    kw_str = ' '.join(kw) if isinstance(kw, list) else (kw or '')
    subject = (item.get('subject') or '').replace('_', ' ')
    return pick_seoul_emoji('', desc_ko, f'{title_ko} {kw_str} {subject}')


# Words that add no information when they are all a description contributes
# beyond the title itself. Articles and prepositions, plus the vocabulary of
# saying "this is a photograph of the thing the title names".
GENERIC_DESC_WORDS = {
    'a', 'an', 'the', 'of', 'in', 'at', 'on', 'and', 'to', 'from', 'with',
    'photo', 'photograph', 'photographed', 'picture', 'pictured', 'image',
    'view', 'scene', 'shot', 'shows', 'showing', 'shown', 'taken', 'depicts',
    'depicting', 'seoul', 'city',
}


def desc_restates_title(title_en, desc_en):
    """True if the description adds nothing beyond the title. The translate()
    prompt forbids restating the title, but nothing enforced it and restatements
    reached the live account ('Samseong-dong Linear Park' described as
    'Photograph of the linear park in Samseong-dong', 20 Jul 2026). Token test:
    strip generic words, and if every remaining description word already
    appears in the title, the description is a restatement."""
    def tokens(s):
        return set(re.findall(r'[a-z0-9]+(?:-[a-z0-9]+)*', s.lower()))
    fresh = tokens(desc_en) - tokens(title_en) - GENERIC_DESC_WORDS
    return not fresh


# The Government-General museum's plates are catalogued as colonial-era, and
# for all but ten of the 284 with a known year that is exactly what they are.
# The exceptions are one 1899 plate and nine shot in 1946-49, after liberation.
# The window opens at 1909, the collection's own earliest year (36 plates) and
# a year before annexation, rather than at 1910: the museum dates the set
# 1909-1945, and second-guessing that would stop the label on its own opening.
COLONIAL_YEARS = range(1909, 1946)


def alt_tail(source, item, year_en):
    """Provenance for generated alt text: credit, what it is, when.

    Named for the `alt_tail` field it reads, which is a leftover: since 18
    August 2026 this leads the alt rather than trailing it, so that the
    A.I. disclosure sits next to the generated description alone.

    The period label is dropped when the catalogue's own year contradicts it,
    so a 1946 photograph is no longer read out as colonial-era. A plate with no
    year keeps the label: an undated plate in this collection is a colonial
    plate by every other piece of evidence, and dropping the period there would
    lose real context on 80% of the pool to protect against nothing.
    """
    obj = source.get('alt_object', '')
    period = source.get('alt_period', '')
    if obj and period:
        year = re.search(r'\d{4}', item_year(item))
        if not year or int(year.group()) in COLONIAL_YEARS:
            obj = f'{period} {obj}'
    parts = [source['alt_tail']] + ([obj] if obj else []) + [year_en]
    return ', '.join(parts) + '.'


def post_header(item, source, date_en=''):
    """The bare line above the caption, or '' for no header line at all.

    A dated source leads with the date (no calendar emoji: Apple draws that
    glyph as a fixed 'JUL 17', which would contradict every date that isn't
    17 July). When the caption stated a precise day, date_en carries it
    ('17 July 1968') and it replaces the bare year; otherwise the year, or
    'date unknown'.

    An undated source leads with the district instead. Printing 'date unknown'
    on four posts in five would be a header that never says anything, so the
    glass plates trade it for the one fact their catalogue does record. Where
    even that is missing (11 of 1,452) the post simply has no header.

    'Undated' is the pool's habit, not a property of every plate in it: 284 of
    the 1,452 do carry a 촬영 연도, and until August 2026 the header threw it
    away, so a plate whose year the alt text stated ('… 1946.') was published
    under a header that gave no date at all. Those now print both, district
    first ('Jongno-gu, 1946'), which keeps the district's new information and
    stops the post contradicting its own alt text.

    The rest say 'Jongno-gu, date unknown' (user, 17 August 2026). The August
    15th rule dropped the date from the plates entirely on the grounds that
    'date unknown' four posts in five was a header saying nothing. It says
    something now that it no longer stands alone: the district carries the
    information, and the missing year is stated rather than left for a reader
    to assume the photograph is undatable or, worse, that we simply didn't say.
    It also matches what the archives pool has always printed for a year it
    doesn't have.
    """
    when = date_en or item_year(item) or 'date unknown'
    if source['dated']:
        return when
    return ', '.join(part for part in (item_district(item), when) if part)


def format_post(title_en, desc_en, title_ko, header, item, source):
    """Build a TextBuilder with proper hashtag facets, trimming if needed.

    An empty desc_en (dropped as a restatement, or never written because the
    source has no description) omits the description line rather than leaving a
    blank one. An empty header omits the header line and its blank line too.
    The Korean block is the title alone, so the header is not repeated.

    The tag set comes from the source: a drawing does not get #photography.
    """
    tags = source['tags']
    # Calculate fixed overhead: everything except desc_en
    # tags as plain text for length check: '#Seoul #Korea #History #서울 #역사'
    tags_plain = ' '.join(f'#{t}' for t, _ in tags)
    header_block = f'{header}\n\n' if header else ''
    body = (
        f'{header_block}'
        f'X {title_en}\n'
        f'{{DESC}}\n\n'
        f'{title_ko}\n\n'
        f'{tags_plain}\n'
        f'{source["link_label"]}'
    )
    overhead = len(body) - len('{DESC}')
    max_desc = MAX_POST_CHARS - overhead
    if len(desc_en) > max_desc:
        desc_en = desc_en[:max_desc - 1] + '…'

    topic_emoji = pick_seoul_emoji(title_en, desc_en, title_ko)
    en_block = (f'{topic_emoji} {title_en}\n{desc_en}' if desc_en
                else f'{topic_emoji} {title_en}')
    tb = client_utils.TextBuilder()
    tb.text(f'{header_block}{en_block}\n\n{title_ko}\n\n')
    for i, (tag, tag_label) in enumerate(tags):
        if i > 0:
            tb.text(' ')
        tb.tag(f'#{tag}', tag_label)
    tb.text('\n')
    tb.link(source['link_label'], item_link(item, source))

    return tb


class ImageFetchError(RuntimeError):
    """One candidate's picture could not be fetched, or was not a picture."""


# The opening bytes of the formats Bluesky accepts.
#
# ⚠️ The bytes are checked, NOT the Content-Type header, and that is
# load-bearing: archives-files.seoul.go.kr serves its live photographs with an
# EMPTY Content-Type, so a header test would reject every image that actually
# works. Verified 22 August 2026 against five sampled photoarchive URLs, all
# 200 with no Content-Type at all, and all posting fine for months.
_IMAGE_MAGIC = (
    b'\xff\xd8\xff',        # JPEG
    b'\x89PNG\r\n\x1a\n',   # PNG
    b'GIF87a',
    b'GIF89a',
)


def _looks_like_image(data):
    if data.startswith(_IMAGE_MAGIC):
        return True
    # WebP is RIFF, a 4-byte length, then WEBP, so its marker is not a prefix.
    return len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP'


def fetch_image(url):
    """Fetch one image, or raise ImageFetchError.

    ⚠️ Both guards are load-bearing, and the bot lost the 9 p.m. post on
    21 August 2026 for want of them.

    `curl -s` alone exits 0 on a 404, so the archive's 204-byte HTML error page
    came back as image bytes and was uploaded to Bluesky as a blob. The PDS
    sniffs a blob's type from its content — atproto sends every upload as
    `input_encoding='*/*'` and lets the server decide — could not identify
    HTML, and echoed `*/*` back. The record validator then rejected the post
    with `Expected "image/*" (got "*/*")`, and the unhandled exception took the
    whole run down.

    `-f` makes curl exit non-zero on an HTTP error. The magic-byte check covers
    what `-f` cannot: a 200 carrying a placeholder, a login page or an error
    body. Neither guard alone is enough.
    """
    result = subprocess.run(
        ['curl', '-fsSL', '--max-time', '30', '-o', '-', url],
        capture_output=True
    )
    if result.returncode != 0:
        err = result.stderr.decode('utf-8', 'replace').strip()
        raise ImageFetchError(
            f'{url}: curl exit {result.returncode}{": " + err if err else ""}')
    if not _looks_like_image(result.stdout):
        raise ImageFetchError(
            f'{url}: not an image ({len(result.stdout)} bytes, '
            f'starts {result.stdout[:16]!r})')
    return result.stdout


# Padding around a gazette article's box before it is cut out.
#
# ⚠️ Not cosmetic. The archives' boxes are tight and sometimes short: the box
# for the 7 January 1982 strip stops inside its fourth panel, and about 20px
# recovers it. Measured on the real pages, not guessed.
CROP_PAD = 20


def crop_article(data, item):
    """Cut one gazette article out of its page scan.

    A gazette item is a region of a page, so what is fetched is the whole
    broadsheet and what gets posted is this rectangle of it.

    ⚠️ The page's real size is checked against the size the archives declared,
    because the coordinates are expressed in the declared one. A mismatch would
    not fail: it would silently mis-crop every article on that page, which is
    the kind of fault that ships for months. It raises instead, and the draw
    loop moves on to another candidate.

    Pillow is imported here rather than at module scope so that a broken or
    missing install costs the gazette its posts and leaves the two photo pools
    running. Going quiet is the failure this bot guards against everywhere else.
    """
    try:
        from PIL import Image
    except ImportError as exc:                       # pragma: no cover
        raise ImageFetchError(f'Pillow needed to crop a gazette page: {exc}')

    import io
    page = Image.open(io.BytesIO(data))
    declared = (item.get('page_width'), item.get('page_height'))
    if page.size != declared:
        raise ImageFetchError(
            f'{item.get("page_image")}: page is {page.size} but the record '
            f'says {declared}; coordinates would not line up')

    left, top, right, bottom = item['box']
    box = (max(0, left - CROP_PAD), max(0, top - CROP_PAD),
           min(page.width, right + CROP_PAD), min(page.height, bottom + CROP_PAD))
    out = io.BytesIO()
    page.crop(box).convert('RGB').save(out, format='JPEG', quality=90)
    print(f'  cropped to {box} ({out.tell()} bytes)')
    return out.getvalue()


def fetch_item_images(item):
    """Every picture for one item, or ImageFetchError if any one of them fails.

    All-or-nothing on purpose: posting the three frames of an event that did
    load would silently drop the fourth, and nothing in the post would say so.
    """
    source = SOURCES[item['_source']]
    all_images = item_images(item)
    # For large sets, sample 4 frames spread across the whole set (kept in
    # original order) rather than the first 4, which in an event set are
    # near-identical opening frames.
    if len(all_images) > 4:
        idx = sorted(random.sample(range(len(all_images)), 4))
        image_urls = [all_images[i] for i in idx]
    else:
        image_urls = all_images
    images = []
    for url in image_urls:
        print(f'Fetching image: {url}')
        data = fetch_image(url)
        if source.get('crop'):
            data = crop_article(data, item)
        images.append(data)
    return images


def main():
    # --tail is a read-only viewer: print recent alt text and exit before the
    # archive, the network or any post. Never touches state.
    if TAIL_N is not None:
        alt_log.tail(ALT_LOG, TAIL_N)
        return

    # Load every pool into one combined candidate list. Each item is tagged with
    # the key of the pool it came from, so the post format, the credit and the
    # file to mark it in all follow the item rather than being global.
    pools, postable = {}, []
    for key, source in SOURCES.items():
        if SOURCE_FILTER and key != SOURCE_FILTER:
            continue
        if not source['path'].exists():
            # A missing pool is survivable while the other one has items: the
            # bot posts from what it has rather than going silent.
            print(f'Warning: {source["path"].name} not found — skipping {key}.')
            continue
        pools[key] = json.loads(source['path'].read_text())
        for it in pools[key]:
            it['_source'] = key
        # `select` narrows a pool to the part of it this bot posts. Only the
        # gazette has one, and it is what keeps 2,466 pages of newspaper text
        # out of a feed of pictures.
        select = source.get('select') or (lambda it: True)
        postable += [it for it in pools[key]
                     if select(it) and item_images(it) and not it.get('posted')]

    if not pools:
        sys.exit('Error: no photo pool found. Re-run the harvest scripts '
                 '(seoul_harvest.py, ~2.5 hrs; seoul_dryplate_harvest.py, ~3 min; '
                 'seoul_gazette_harvest.py, ~9 min).')
    if not postable:
        print('No postable items remaining in any pool.')
        sys.exit(0)

    # Everything from here on needs the network: translate() shells out to
    # `claude -p`, which raised EHOSTUNREACH on every firing through the
    # August 2026 outage. Posts run twice daily, so half an hour of waiting is
    # free. Gated after the archive checks above, which are purely local.
    net_guard.require_network(1800)

    # Load rolling state early (used for the topic cooldown below and the
    # success timestamp at the end).
    state_file = Path(__file__).parent / 'seoul_state.json'
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    recent_categories = state.get('recent_categories', [])

    # Topic cooldown: drop candidates whose topic matches a recently posted one.
    candidates = [it for it in postable if item_category(it) not in recent_categories]
    if not candidates:
        # Every remaining topic is on cooldown (only near the very end): fall
        # back to the full pool so the bot never goes silent.
        candidates = postable

    # Draw an item and fetch its pictures, re-drawing if they will not load.
    #
    # Three things here are deliberate.
    #
    # The fetch runs BEFORE translate() rather than after it, which is where it
    # used to sit. A re-draw then costs a couple of HTTP requests instead of a
    # wasted `claude -p` call.
    #
    # A candidate whose pictures will not load is dropped and another drawn,
    # rather than taking the run down. 298 of the archive's 9,570 items point
    # at the C001 collection, which went 404 some time before 21 August 2026,
    # so roughly one draw in 32 lands on a dead item. Before this loop that was
    # a silently lost post every fortnight or so.
    #
    # ⚠️ A failed item is NOT marked in the pool, however tempting. At this
    # level a dead image is indistinguishable from an archive having a bad
    # morning, so marking would permanently burn good items the first time the
    # host wobbled. The cost of not marking is a couple of wasted requests when
    # a dead item comes up again, which is nothing.
    item = images = None
    for attempt in range(1, DRAW_ATTEMPTS + 1):
        # Weighted, so the gazette's 107 cartoons hold their share against
        # 10,956 photographs. Recomputed each attempt: a failed candidate is
        # dropped from the list, which changes the per-item weights.
        pick = random.choices(candidates, weights=draw_weights(candidates))[0]
        print(f'Selected: [{item_id(pick)}] {pick["title"]} '
              f'({item_year(pick) or "?"}) topic={item_category(pick)} '
              f'source={pick["_source"]}')
        try:
            images = fetch_item_images(pick)
        except ImageFetchError as exc:
            print(f'  !! image fetch failed: {exc}')
            candidates = [it for it in candidates if it is not pick]
            if not candidates:
                sys.exit('Error: no candidate left with a working image.')
            print(f'  re-drawing (attempt {attempt} of {DRAW_ATTEMPTS})')
            continue
        item = pick
        break

    if item is None:
        # Exits non-zero on purpose: this is the archive host being down, not a
        # bad item, and harden_audit.sh check 5 is what should notice.
        sys.exit(f'Error: {DRAW_ATTEMPTS} candidates in a row had unusable '
                 f'images. The archive host is probably down.')

    item_cat = item_category(item)
    source = SOURCES[item['_source']]

    # Translate
    print('Translating...')
    if item['_source'] == 'gazette':
        translation = translate_gazette(item)
    else:
        translation = translate(item['title'], item_desc(item), item_year(item))
    title_en = capitalize_after_colon(
        group_thousands(educate_quotes(translation['title'])))
    desc_en = capitalize_after_colon(
        group_thousands(educate_quotes(translation['description'])))
    # A record that states its own exact day beats anything the model found.
    date_en = item_date_en(item) or (translation.get('date') or '').strip()
    print(f'  EN title: {title_en}')
    print(f'  EN desc:  {desc_en}')
    if date_en:
        print(f'  Precise date: {date_en}')
    if desc_en and desc_restates_title(title_en, desc_en):
        print('  EN desc restates the title — dropped.')
        desc_en = ''

    # Format post
    header = post_header(item, source, date_en)
    post_text = format_post(title_en, desc_en, item['title'], header,
                            item, source)
    post_plain = post_text.build_text()
    print(f'\nPost ({len(post_plain)} chars):\n{"-"*40}\n{post_plain}\n{"-"*40}')

    # Alt text describes the PHOTOGRAPH, behind a short provenance lead.
    #
    # Until August 2026 the alt was the citation alone: title, year, archive.
    # That is provenance, not description, and it restated the post text almost
    # word for word, so a screen reader read the same line twice and conveyed
    # nothing about the picture. On a bot whose whole content is the picture,
    # that was the weakest alt in the estate.
    #
    # image_alt.describe() shows the model the actual image. It is best-effort:
    # if the call fails, times out, or comes back unusable, the old citation is
    # still a valid caption and the post goes out with it. Up to four images
    # means up to four calls on a twice-daily job, which is affordable.
    #
    # The credit in both the citation and the tail is required, not decorative:
    # the glass plates are 공공누리 제1유형, whose one condition is 출처표시.
    year_ko = item_year(item) or '연대미상'
    year_en = date_en or item_year(item) or 'date unknown'
    citation = (f'{title_en} / {item["title"]} — {year_ko} — '
                f'{source["alt_credit"]}')
    tail = alt_tail(source, item, year_en)
    context = f'{title_en} / {item["title"]} / {item_year(item) or "year unknown"}'
    env = claude_env()

    image_alts, alt_generated = [], []
    for i, img in enumerate(images):
        desc = image_alt.describe(img, context=context, env=env)
        # Provenance FIRST, then the disclosure, then the description.
        #
        # Until 18 August 2026 the disclosure led the whole string and the
        # provenance tail trailed it, so a listener heard "A.I.-generated
        # description." as a header over everything that followed, the
        # archive's own credit and year included. That labels a human
        # catalogued fact as model output, which is the exact inverse of what
        # the disclosure is for. Leading with the provenance puts the
        # trustworthy statement first and leaves the disclosure adjacent to
        # the only text it covers. holmes_post.py was corrected the same day,
        # for the same reason, in the same shape.
        #
        # The counter goes with the provenance rather than the description for
        # the same reason: which of four images this is, is counted here, not
        # seen by the model.
        #
        # The disclosure rides the generated branch only: the citation
        # fallback is the archive's own catalogue entry, not model output.
        lead = tail if desc else citation
        if len(images) > 1:
            lead = f'{lead} ({i + 1} of {len(images)})'
        alt = f'{lead} {image_alt.DISCLOSURE} {desc}' if desc else lead
        image_alts.append(alt)
        alt_generated.append(desc is not None)
        print(f'  alt {i + 1}/{len(images)}: {alt}')

    # The dry run now stops HERE rather than before the fetch. Alt text became
    # generated content in August 2026, so previewing a post without it would
    # leave the half most worth checking unreviewed. The cost is that a dry run
    # fetches the images and spends a model call on each.
    if DRY_RUN:
        print('(dry run — not posting)')
        return

    # Post to Bluesky
    password = keychain_password(HANDLE, KEYCHAIN_SERVICE)
    bsky = Client()
    bsky.login(HANDLE, password)

    resp = bsky.send_images(
        text=post_text,
        images=images,
        image_alts=image_alts,
    )

    print('Posted successfully.')

    # Record what shipped. `generated` is the flag worth watching: a failed
    # model call falls back silently by design, which keeps posts going out and
    # makes a run of failures invisible. A tail showing every recent post
    # falling back means the description step is broken, not that the pictures
    # resist description.
    alt_log.append(ALT_LOG, {
        'at': datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S'),
        'id': item_id(item),
        'source': item['_source'],
        'title': title_en,
        'url': alt_log.post_url(HANDLE, getattr(resp, 'uri', None)),
        'generated': all(alt_generated),
        'generated_count': f'{sum(alt_generated)}/{len(alt_generated)}',
        'alts': image_alts,
    })

    # Mark as posted, in the pool the item actually came from. `_source` is an
    # in-memory tag, so it is stripped before the pool is written back.
    item['posted'] = True
    pool = pools[item['_source']]
    write_json_atomic(source['path'],
                      [{k: v for k, v in it.items() if k != '_source'}
                       for it in pool],
                      ensure_ascii=False, indent=2)
    state['last_success_at'] = datetime.now(timezone.utc).isoformat()
    if item_cat != '📷':
        recent_categories.append(item_cat)
        state['recent_categories'] = recent_categories[-CATEGORY_COOLDOWN:]
    write_json_atomic(state_file, state, ensure_ascii=False, indent=2)
    print(f'Marked [{item_id(item)}] as posted in {item["_source"]}. '
          f'{len(postable) - 1} items remaining across all pools.')


if __name__ == '__main__':
    main()
