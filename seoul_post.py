#!/usr/bin/env python3
"""
Post one historical Seoul photo to Bluesky (@oldhanyang.bsky.social).
Picks a random unposted item from seoul_archive.json, translates with Claude,
posts with the image, and marks it as posted.

Requires:
    security add-generic-password -a "oldhanyang.bsky.social" -s "seoulbot-bluesky" -w

Usage:
    python3 seoul_post.py           # post one item
    python3 seoul_post.py --dry-run # translate and format without posting
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

ARCHIVE = Path(__file__).parent / 'seoul_archive.json'
HANDLE = 'oldhanyang.bsky.social'
KEYCHAIN_SERVICE = 'seoulbot-bluesky'
DRY_RUN = '--dry-run' in sys.argv

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


def translate(title_ko, description_ko, year):
    """Translate Korean title and description to concise English via claude -p."""
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
        f'- Return JSON only: {{"title": "...", "description": "..."}}'
    )
    for attempt in range(2):
        result = subprocess.run(
            ['claude', '-p', '--model', 'claude-haiku-4-5-20251001', prompt],
            capture_output=True, text=True, env=claude_env()
        )
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
    (["palace", "fortress", "gate", "landmark", "historic", "heritage",
      "대문", "광화문", "독립문", "성문", "성곽", "문화재",
      "경복궁", "창덕궁", "덕수궁", "창경궁", "고궁"], "🏯"),
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
    """
    title_ko = item.get('title') or ''
    desc_ko = item.get('description') or ''
    kw = item.get('keywords')
    kw_str = ' '.join(kw) if isinstance(kw, list) else (kw or '')
    return pick_seoul_emoji('', desc_ko, f'{title_ko} {kw_str}')


def format_post(title_en, desc_en, title_ko, year, item_id):
    """Build a TextBuilder with proper hashtag facets, trimming if needed."""
    year_str = year or '연대미상'

    # Calculate fixed overhead: everything except desc_en
    # tags as plain text for length check: '#Seoul #Korea #History #서울 #역사'
    tags_plain = ' '.join(f'#{t}' for t, _ in TAGS)
    body = (
        f'📍 Seoul, {year_str}\n\n'
        f'X {title_en}\n'
        f'{{DESC}}\n\n'
        f'서울, {year_str}\n\n'
        f'{title_ko}\n\n'
        f'{tags_plain}\n'
        f'🗃️ Seoul Metropolitan Archives'
    )
    overhead = len(body) - len('{DESC}')
    max_desc = MAX_POST_CHARS - overhead
    if len(desc_en) > max_desc:
        desc_en = desc_en[:max_desc - 1] + '…'

    topic_emoji = pick_seoul_emoji(title_en, desc_en, title_ko)
    tb = client_utils.TextBuilder()
    tb.text(f'📍 Seoul, {year_str}\n\n{topic_emoji} {title_en}\n{desc_en}\n\n서울, {year_str}\n\n{title_ko}\n\n')
    for i, (tag, tag_label) in enumerate(TAGS):
        if i > 0:
            tb.text(' ')
        tb.tag(f'#{tag}', tag_label)
    tb.text('\n')
    archive_url = f'https://archives.seoul.go.kr/item/{item_id}'
    tb.link('🗃️ Seoul Metropolitan Archives', archive_url)

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
    # Load archive
    if not ARCHIVE.exists():
        sys.exit(f'Error: {ARCHIVE} not found. Re-run seoul_harvest.py (~2.5 hrs).')
    items = json.loads(ARCHIVE.read_text())
    postable = [it for it in items if it.get('images') and not it.get('posted')]

    if not postable:
        print('No postable items remaining in archive.')
        sys.exit(0)

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
    print(f'Selected: [{item["id"]}] {item["title"]} ({item["year"] or "?"}) topic={item_cat}')

    # Translate
    print('Translating...')
    translation = translate(item['title'], item['description'], item['year'])
    title_en = group_thousands(educate_quotes(translation['title']))
    desc_en = group_thousands(educate_quotes(translation['description']))
    print(f'  EN title: {title_en}')
    print(f'  EN desc:  {desc_en}')

    # Format post
    post_text = format_post(title_en, desc_en, item['title'], item['year'], item['id'])
    post_plain = post_text.build_text()
    print(f'\nPost ({len(post_plain)} chars):\n{"-"*40}\n{post_plain}\n{"-"*40}')

    if DRY_RUN:
        print('(dry run — not posting)')
        return

    # Fetch up to 4 images. For large sets, sample 4 frames spread across the
    # whole set (kept in original order) rather than the first 4, which in an
    # event set are near-identical opening frames.
    all_images = item['images']
    if len(all_images) > 4:
        idx = sorted(random.sample(range(len(all_images)), 4))
        image_urls = [all_images[i] for i in idx]
    else:
        image_urls = all_images
    images = []
    for url in image_urls:
        print(f'Fetching image: {url}')
        images.append(fetch_image(url))

    base_alt = f'{title_en} / {item["title"]} — {item["year"] or "연대미상"} — 서울기록원'
    if len(images) == 1:
        image_alts = [base_alt]
    else:
        image_alts = [f'{base_alt} ({i + 1} of {len(images)})' for i in range(len(images))]

    # Post to Bluesky
    password = keychain_password(HANDLE, KEYCHAIN_SERVICE)
    bsky = Client()
    bsky.login(HANDLE, password)

    bsky.send_images(
        text=post_text,
        images=images,
        image_alts=image_alts,
    )

    print('Posted successfully.')

    # Mark as posted and record success timestamp in a small state file
    item['posted'] = True
    ARCHIVE.write_text(json.dumps(items, ensure_ascii=False, indent=2))
    state['last_success_at'] = datetime.now(timezone.utc).isoformat()
    if item_cat != '📷':
        recent_categories.append(item_cat)
        state['recent_categories'] = recent_categories[-CATEGORY_COOLDOWN:]
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    print(f'Marked [{item["id"]}] as posted. {len(postable) - 1} items remaining.')


if __name__ == '__main__':
    main()
