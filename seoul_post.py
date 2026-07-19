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


TAGS = [('photography', 'photography'), ('seoul', 'seoul'), ('korea', 'korea'), ('history', 'history'), ('서울', '서울'), ('역사', '역사')]

SEOUL_EMOJIS = [
    (["song", "music", "musical", "singing", "singer", "choir", "concert", "orchestra", "band",
      "가요", "음악", "노래", "합창", "경연", "연주", "악단"], "🎵"),
    (["construct", "groundbreak", "build", "erect", "foundation", "bridge", "road", "highway", "infrastructure", "tunnel", "overpass",
      "착공", "건설", "공사", "준공", "개통", "교량", "도로"], "🏗️"),
    (["government", "office", "city hall", "mayor", "governor", "ministry", "assembly", "court", "police", "fire station",
      "청사", "시청", "구청", "경찰", "소방", "법원", "국회"], "🏛️"),
    (["school", "university", "college", "education", "library",
      "학교", "대학", "도서관", "교육"], "🏫"),
    (["hospital", "clinic", "medical", "health",
      "병원", "의원", "보건", "의료"], "🏥"),
    (["temple", "shrine", "church", "buddhist", "confucian", "pagoda",
      "사찰", "절", "교회", "성당", "향교", "불교"], "⛩️"),
    (["tram", "train", "railway", "subway", "metro", "bus", "transport", "station",
      "전차", "기차", "철도", "지하철", "버스", "역", "교통"], "🚃"),
    # NB: the Korean word 시장 means BOTH "market" and "mayor", so it is
    # deliberately omitted here — "서울시장" (Seoul Mayor) would otherwise pick
    # 🛒. Genuine markets are still caught by the English "market" (translations
    # render 시장-as-market as "market") plus the other Korean shop terms.
    (["market", "shop", "store", "commercial", "trade", "merchant",
      "상점", "상가", "가게", "상업"], "🛒"),
    (["parade", "ceremony", "festival", "celebration", "rally", "event", "commemor",
      "행사", "축제", "기념", "퍼레이드", "행진", "식전", "식장"], "🎉"),
    (["park", "garden", "nature", "mountain", "river", "han river", "forest",
      "공원", "정원", "산", "강", "한강", "자연", "숲"], "🌸"),
    (["portrait", "people", "crowd", "family", "children", "worker", "student",
      "사람", "군중", "가족", "어린이", "학생", "노동자"], "👥"),
    (["neighbourhood", "neighborhood", "village", "district", "residential", "house", "housing",
      "마을", "동네", "주택", "아파트", "골목"], "🏘️"),
    (["palace", "fortress", "gate", "landmark", "historic", "heritage",
      "궁", "성곽", "문", "대문", "문화재"], "🏯"),
    (["sport", "stadium", "athletic", "olympic", "race",
      "스포츠", "경기장", "운동", "올림픽"], "🏅"),
    (["airport", "aircraft", "aviation", "flight",
      "공항", "비행기", "항공"], "✈️"),
    (["flood", "fire", "disaster", "relief", "war", "protest",
      "홍수", "화재", "재해", "전쟁", "시위"], "⚠️"),
]


def pick_seoul_emoji(title_en, desc_en, title_ko):
    combined = f"{title_en} {desc_en} {title_ko}".lower()
    for keywords, emoji in SEOUL_EMOJIS:
        for kw in keywords:
            if kw.isascii():
                # Word-boundary match so short English roots (e.g. "road")
                # don't fire inside longer words (e.g. "broadcast").
                if re.search(rf"\b{re.escape(kw)}\b", combined):
                    return emoji
            elif kw in combined:
                # Korean has no spaces to anchor on: substring match is fine.
                return emoji
    return "📷"


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

    # Pick a random item
    item = random.choice(postable)
    print(f'Selected: [{item["id"]}] {item["title"]} ({item["year"] or "?"})')

    # Translate
    print('Translating...')
    translation = translate(item['title'], item['description'], item['year'])
    title_en = translation['title']
    desc_en = translation['description']
    print(f'  EN title: {title_en}')
    print(f'  EN desc:  {desc_en}')

    # Format post
    post_text = format_post(title_en, desc_en, item['title'], item['year'], item['id'])
    post_plain = post_text.build_text()
    print(f'\nPost ({len(post_plain)} chars):\n{"-"*40}\n{post_plain}\n{"-"*40}')

    if DRY_RUN:
        print('(dry run — not posting)')
        return

    # Fetch up to 4 images
    image_urls = item['images'][:4]
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
    state_file = Path(__file__).parent / 'seoul_state.json'
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    state['last_success_at'] = datetime.now(timezone.utc).isoformat()
    state_file.write_text(json.dumps(state, indent=2))
    print(f'Marked [{item["id"]}] as posted. {len(postable) - 1} items remaining.')


if __name__ == '__main__':
    main()
