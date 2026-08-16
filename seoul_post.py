#!/usr/bin/env python3
"""
Post one historical Seoul photo to Bluesky (@oldhanyang.bsky.social).
Picks a random unposted item from the combined photo pools, translates with
Claude, posts with the image, and marks it as posted in its own pool.

Two pools (see SOURCES):
  - seoul_archive.json   Seoul Metropolitan Archives, 1950s-90s municipal
                         photography, dated, with Korean descriptions.
  - seoul_dryplate.json  National Museum of Korea's 조선총독부박물관 glass plates,
                         1909-1945, mostly undated and title-only. 공공누리
                         제1유형, so the museum credit is mandatory.

Requires:
    security add-generic-password -a "oldhanyang.bsky.social" -s "seoulbot-bluesky" -w

Usage:
    python3 seoul_post.py                      # post one item
    python3 seoul_post.py --dry-run            # translate and format, no post
    python3 seoul_post.py --source dryplate    # restrict the pool to one source
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
import net_guard

ARCHIVE = Path(__file__).parent / 'seoul_archive.json'
DRYPLATE = Path(__file__).parent / 'seoul_dryplate.json'
# One JSONL line per posted item, recording the alt text that shipped and
# whether it was generated or fell back. Written only on a real post, and
# best-effort: see alt_log.
ALT_LOG = Path(__file__).parent / 'alt_history.jsonl'
HANDLE = 'oldhanyang.bsky.social'
KEYCHAIN_SERVICE = 'seoulbot-bluesky'

# The two photo pools. They are posted from a single combined pool, so each
# source's share of the feed is just its share of the unposted items.
#
# `dated`: the Seoul Metropolitan Archives records carry a usable year, so their
# posts lead with it. The glass plates mostly do not — only 284 of 1,452 have a
# 촬영 연도 at all — so leading with a year would mean printing "date unknown"
# four posts in five. Those posts lead with the district instead, which is real
# information the header never carried (the whole feed is Seoul, so the city
# alone said nothing; see the 2026-07-26 header change).
SOURCES = {
    'archives': {
        'path': ARCHIVE,
        'link_label': '🗃️ Seoul Metropolitan Archives',
        'item_url': 'https://archives.seoul.go.kr/item/{id}',
        'alt_tail': 'Seoul Metropolitan Archives',
        'alt_credit': '서울기록원',
        'dated': True,
    },
    'dryplate': {
        'path': DRYPLATE,
        # 공공누리 제1유형 (출처표시): reuse is free, attribution is mandatory,
        # so the museum credit is not decoration and must not be dropped.
        'link_label': '🗃️ National Museum of Korea',
        'item_url': 'https://www.museum.go.kr/dryplate/searchplate_view.do?relicnum={id}',
        'alt_tail': 'National Museum of Korea, colonial-era glass plate',
        'alt_credit': '국립중앙박물관 유리건판',
        'dated': False,
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

    A testing and backfill aid: without it the pool is both sources combined.
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
    """
    return item.get('description') or ''


def item_year(item):
    return item.get('year') or ''


def item_images(item):
    """Image URLs as a list. The glass plates are one plate, one image."""
    if item.get('images'):
        return item['images']
    return [item['image_url']] if item.get('image_url') else []


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
        f'- Do not add interpretation, context or anything not in the Korean\n'
        f'- Return JSON only: {{"title": "..."}}'
    )
    out = _claude_json(prompt)
    # Downstream expects all three keys; only the title is ever populated here.
    return {'title': out.get('title', ''), 'description': '', 'date': ''}


def _claude_json(prompt):
    """Run `claude -p` and parse a JSON object from its output, with one retry
    on a timeout or malformed JSON."""
    for attempt in range(2):
        try:
            result = subprocess.run(
                ['claude', '-p', '--model', 'claude-haiku-4-5-20251001', prompt],
                capture_output=True, text=True, env=claude_env(),
                timeout=CLAUDE_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            if attempt == 0:
                print(f'Warning: claude -p timed out after {CLAUDE_TIMEOUT}s (attempt 1), retrying...')
                continue
            raise RuntimeError(
                f'claude -p timed out after {CLAUDE_TIMEOUT}s, twice')
        if result.returncode != 0:
            # The claude CLI writes some errors (e.g. auth 401s) to stdout, not
            # stderr, so include both to keep failures diagnosable.
            err = (result.stderr or result.stdout or '').strip() or '(no output)'
            raise RuntimeError(f'claude -p failed (exit {result.returncode}): {err}')
        text = result.stdout.strip()
        text = re.sub(r'^```[a-z]*\n?|\n?```$', '', text).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            if attempt == 0:
                print(f'Warning: malformed JSON from claude -p (attempt 1), retrying...')
                continue
            raise RuntimeError(f'claude -p returned invalid JSON after 2 attempts: {repr(text[:200])}')


def educate_quotes(text):
    """Convert straight ASCII quotes/apostrophes to typographic (curly) marks.

    English captions read as crude with straight ' and " (Bluesky renders
    whatever bytes we send). This applies the standard smart-quotes heuristic
    deterministically rather than trusting the LLM to emit curly marks:
    a quote at the start of the text or after whitespace/an opening bracket
    opens (left); anything else closes (right), which also turns in-word
    apostrophes (don't, it's, 1970's) into the right single quote. Only the
    English fields are passed through this — Korean text is left untouched.
    """
    if not text:
        return text
    left_single, right_single = '‘', '’'
    left_double, right_double = '“', '”'
    out = []
    for i, ch in enumerate(text):
        prev = text[i - 1] if i > 0 else ''
        opening = (i == 0) or prev.isspace() or prev in '([{'
        if ch == '"':
            out.append(left_double if opening else right_double)
        elif ch == "'":
            out.append(left_single if opening else right_single)
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


TAGS = [('photography', 'photography'), ('seoul', 'seoul'), ('korea', 'korea'), ('history', 'history'), ('서울', '서울'), ('역사', '역사')]

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
    """
    if source['dated']:
        return date_en or item_year(item) or 'date unknown'
    return date_en or item_district(item)


def format_post(title_en, desc_en, title_ko, header, item_id, source):
    """Build a TextBuilder with proper hashtag facets, trimming if needed.

    An empty desc_en (dropped as a restatement, or never written because the
    source has no description) omits the description line rather than leaving a
    blank one. An empty header omits the header line and its blank line too.
    The Korean block is the title alone, so the header is not repeated.
    """
    # Calculate fixed overhead: everything except desc_en
    # tags as plain text for length check: '#Seoul #Korea #History #서울 #역사'
    tags_plain = ' '.join(f'#{t}' for t, _ in TAGS)
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
    for i, (tag, tag_label) in enumerate(TAGS):
        if i > 0:
            tb.text(' ')
        tb.tag(f'#{tag}', tag_label)
    tb.text('\n')
    tb.link(source['link_label'], source['item_url'].format(id=item_id))

    return tb


def fetch_image(url):
    result = subprocess.run(
        ['curl', '-s', '--max-time', '30', '-o', '-', url],
        capture_output=True
    )
    if result.returncode != 0:
        raise RuntimeError(f'Failed to fetch image: {url}')
    return result.stdout


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
        postable += [it for it in pools[key]
                     if item_images(it) and not it.get('posted')]

    if not pools:
        sys.exit('Error: no photo pool found. Re-run the harvest scripts '
                 '(seoul_harvest.py, ~2.5 hrs; seoul_dryplate_harvest.py, ~3 min).')
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

    # Pick a random item
    item = random.choice(candidates)
    item_cat = item_category(item)
    source = SOURCES[item['_source']]
    print(f'Selected: [{item_id(item)}] {item["title"]} '
          f'({item_year(item) or "?"}) topic={item_cat} source={item["_source"]}')

    # Translate
    print('Translating...')
    translation = translate(item['title'], item_desc(item), item_year(item))
    title_en = group_thousands(educate_quotes(translation['title']))
    desc_en = group_thousands(educate_quotes(translation['description']))
    date_en = (translation.get('date') or '').strip()
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
                            item_id(item), source)
    post_plain = post_text.build_text()
    print(f'\nPost ({len(post_plain)} chars):\n{"-"*40}\n{post_plain}\n{"-"*40}')

    # Fetch up to 4 images. For large sets, sample 4 frames spread across the
    # whole set (kept in original order) rather than the first 4, which in an
    # event set are near-identical opening frames.
    all_images = item_images(item)
    if len(all_images) > 4:
        idx = sorted(random.sample(range(len(all_images)), 4))
        image_urls = [all_images[i] for i in idx]
    else:
        image_urls = all_images
    images = []
    for url in image_urls:
        print(f'Fetching image: {url}')
        images.append(fetch_image(url))

    # Alt text describes the PHOTOGRAPH, with provenance as a short tail.
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
    tail = f'{source["alt_tail"]}, {year_en}.'
    context = f'{title_en} / {item["title"]} / {item_year(item) or "year unknown"}'
    env = claude_env()

    image_alts, alt_generated = [], []
    for i, img in enumerate(images):
        desc = image_alt.describe(img, context=context, env=env)
        # The disclosure rides the generated branch only: the citation
        # fallback is the archive's own catalogue entry, not model output.
        alt = (f'{image_alt.DISCLOSURE} {desc} {tail}' if desc else citation)
        if len(images) > 1:
            alt = f'{alt} ({i + 1} of {len(images)})'
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
