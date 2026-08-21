#!/usr/bin/env python3
"""
Harvest all photo items from archives.seoul.go.kr/photo/list.
Saves to seoul_archive.json (resumable — skips already-fetched item IDs).

Usage:
    python3 seoul_harvest.py            # full harvest
    python3 seoul_harvest.py --sample N # fetch N evenly-spaced items for testing
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

BASE_URL = 'https://archives.seoul.go.kr'
OUTPUT = Path(__file__).parent / 'seoul_archive.json'
DELAY = 1.0  # seconds between requests
HEADERS = ['User-Agent: Seoul-Archive-Bot/1.0 (personal project; contact: https://chris-stanford.com)']


def fetch(url):
    args = ['curl', '-s', '--max-time', '30']
    for h in HEADERS:
        args += ['-H', h]
    args.append(url)
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f'curl failed: {result.stderr}')
    return result.stdout


def get_item_ids_from_page(page, page_size=100):
    html = fetch(f'{BASE_URL}/photo/list?page={page}&pageSize={page_size}')
    return re.findall(r'href="/item/(\d+)"', html)


def parse_item(item_id, html):
    lines = [l.strip() for l in html.split('\n') if l.strip()]
    # Remove HTML tags for text lines
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    text_lines = [l.strip() for l in soup.get_text('\n').split('\n') if l.strip()]

    # Title
    title_m = re.search(r'상세보기 - (.+?) \| 서울기록원', html)
    title = title_m.group(1).strip() if title_m else ''

    # Year from og:image URL
    og_m = re.search(r'og:image.*?content="([^"]+)"', html)
    year = None
    if og_m:
        year_m = re.search(r'(\d{4})-\d{4}-\d{4}\.jpg', og_m.group(1))
        if year_m:
            year = year_m.group(1)

    # Description (기술): line immediately after '기술' label
    description = ''
    for i, line in enumerate(text_lines):
        if line == '기술' and i + 1 < len(text_lines):
            description = text_lines[i + 1]
            break

    # Access type (접근유형)
    access = ''
    for i, line in enumerate(text_lines):
        if line == '접근유형' and i + 1 < len(text_lines):
            access = text_lines[i + 1]
            break

    # Usage type (이용유형)
    usage = ''
    for i, line in enumerate(text_lines):
        if line == '이용유형' and i + 1 < len(text_lines):
            usage = text_lines[i + 1]
            break

    # Keywords
    keywords = ''
    for line in text_lines:
        if line.startswith('키워드 :'):
            keywords = line.replace('키워드 :', '').strip()
            break

    # 2000px image URLs
    images = re.findall(
        r'src="(https://archives-files\.seoul\.go\.kr[^"]*2000_[^"]+)"', html
    )

    return {
        'id': item_id,
        'title': title,
        'year': year,
        'description': description,
        'access': access,
        'usage': usage,
        'keywords': keywords,
        'images': images,
        'posted': False,
    }


def load_existing():
    if OUTPUT.exists():
        return {item['id']: item for item in json.loads(OUTPUT.read_text())}
    return {}


def save(items_by_id):
    OUTPUT.write_text(json.dumps(list(items_by_id.values()), ensure_ascii=False, indent=2))


def main():
    sample_n = None
    if '--sample' in sys.argv:
        idx = sys.argv.index('--sample')
        sample_n = int(sys.argv[idx + 1])

    print('Loading existing archive...')
    items_by_id = load_existing()
    print(f'  {len(items_by_id)} items already fetched')

    # Step 1: collect all item IDs from list pages
    print('Collecting item IDs from photo list...')
    all_ids = []
    page = 1
    while True:
        ids = get_item_ids_from_page(page)
        if not ids:
            break
        all_ids.extend(ids)
        print(f'  Page {page}: {len(ids)} items (total so far: {len(all_ids)})', end='\r')
        time.sleep(DELAY)
        page += 1

    # Deduplicate
    all_ids = list(dict.fromkeys(all_ids))
    print(f'\nTotal unique item IDs: {len(all_ids)}')

    # Apply sample
    if sample_n:
        step = max(1, len(all_ids) // sample_n)
        all_ids = all_ids[::step][:sample_n]
        print(f'Sample mode: {len(all_ids)} items')

    # Step 2: fetch each item not already in archive
    to_fetch = [id_ for id_ in all_ids if id_ not in items_by_id]
    print(f'Items to fetch: {len(to_fetch)}')

    for i, item_id in enumerate(to_fetch, 1):
        try:
            html = fetch(f'{BASE_URL}/item/{item_id}')
            item = parse_item(item_id, html)

            # Filter: must be public and unrestricted
            if item['access'] not in ('공개', '') or item['usage'] not in ('제한없음', ''):
                print(f'  [{i}/{len(to_fetch)}] Skipping {item_id} (access={item["access"]}, usage={item["usage"]})')
                continue

            # Must have at least one image
            if not item['images']:
                print(f'  [{i}/{len(to_fetch)}] Skipping {item_id} (no images)')
                continue

            items_by_id[item_id] = item
            print(f'  [{i}/{len(to_fetch)}] {item_id}: {item["title"]} ({item["year"] or "?"})')

            # Save every 50 items
            if i % 50 == 0:
                save(items_by_id)

        except Exception as e:
            print(f'  [{i}/{len(to_fetch)}] ERROR on {item_id}: {e}')

        time.sleep(DELAY)

    save(items_by_id)
    postable = sum(1 for v in items_by_id.values() if v['images'] and not v['posted'])
    print(f'\nDone. Archive: {len(items_by_id)} items, {postable} postable.')


# Refuse anything unrecognised before the flags below are read. Bare membership
# tests silently ignore what they do not recognise, so a typo (`--sampel`) or a
# reflex (`--help`) reads as no flag at all and takes the ordinary path.
# seoul-index published a real thread that way on 20 July 2026. The posting
# bots in these repos were given this guard then; the harvest scripts were not,
# which is what harden_audit.sh check 10 was still reporting on 21 August 2026.
# Without the guard a mistyped --sample harvests the whole archive.
_KNOWN_ARGS = {'--sample'}

if __name__ == '__main__':
    # The integer after --sample belongs to that flag, so it must not be read
    # as an unknown argument.
    _skip = set()
    if '--sample' in sys.argv:
        _s = sys.argv.index('--sample')
        if _s + 1 < len(sys.argv) and sys.argv[_s + 1].isdigit():
            _skip.add(_s + 1)
    _unknown = [a for j, a in enumerate(sys.argv[1:], 1)
                if a not in _KNOWN_ARGS and j not in _skip]
    if _unknown:
        sys.exit(f'Unknown argument(s): {" ".join(_unknown)}. '
                 f'Recognised: {" ".join(sorted(_KNOWN_ARGS))} [N]. '
                 f'Refusing to run.')
    main()
