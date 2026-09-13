#!/usr/bin/env python3
"""
Harvest the Library of Congress's pre-1945 Seoul photographs into seoul_loc.json.

Why this pool exists. The archive pool is 1960 to 1989 almost entire (8,965 of
its 9,570 items), the KTV pool is 1950 to 1979, and the glass plates are
1909 to 1945 with two thirds of them undated. Before 1909 the bot has almost
nothing. The Library of Congress holds about 150 photographs of Seoul from
1895 to 1911 with dated English captions written at the time: the 1904
Underwood & Underwood stereograph series (the East Gate blacksmith, the Pekin
Pass, the Minister of War at go-ban), Frank Carpenter's 1908-1911 album, an
1895 set including the panorama from the Russian Legation, and the 1910
"Korean customs" studio scenes. Measured 12 September 2026 by reading every
pre-1945 Seoul record in the Prints & Photographs division.

    python3 seoul_loc_harvest.py               # harvest (resumable)
    python3 seoul_loc_harvest.py --sample 10   # 10 items, for testing
    python3 seoul_loc_harvest.py --force       # write a SMALLER pool anyway

⚠️⚠️ **THIS CANNOT RUN FROM SEOUL.** loc.gov's catalogue answers every
request from a Korean address with a Cloudflare challenge that never clears,
with any User-Agent and in a real browser alike, while the same JSON URLs
answer plainly from a US address (verified 12 September 2026: seven GitHub
Actions runs succeeded where curl and the browser pane on the Mac Mini both
got "Just a moment..."). The image host tile.loc.gov is NOT walled, so the
poster can fetch pictures from the Mini; only this harvest needs a US egress.
`.github/workflows/loc-harvest.yml` runs it on a GitHub runner and hands the
pool back as a workflow artifact:

    gh workflow run loc-harvest.yml
    gh run download <run-id> -n seoul_loc      # writes seoul_loc.json here

⚠️ **Rights are read off EVERY item record, and only "No known restrictions on
publication" is kept.** The search listing carries no rights field at all.
The advisory varies by lot within one search: the 1896 gate lot and half the
1911 Manchuria-and-Korea album read "Rights status not evaluated" and serve no
file, and everything from 1945 on is the New York World-Telegram morgue,
"Publication may be restricted". Matched POSITIVELY on the phrase, never as
"not restricted". A record with no JPEG in its resources is skipped whatever
its advisory says, since those two go together.

⚠️ **The catalogue rate-limits.** Item records at 1.2 s spacing drew HTTP 429
after six and then refused everything; at 6 s spacing 114 in a row passed.
ITEM_DELAY is 6 and is not a number to trim.

⚠️ **Seoul-ness is recorded, not decided, here.** The search matches "seoul"
anywhere in a record, including a lot title, so a 1919 wedding stereograph
captioned "Chosen (Korea)" is in the result set. `seoul_named` says whether
the item's own title, description or notes name the city; seoul_post.py's
select filter reads it. The rest are kept in the file so the decision can be
changed without a 25-minute re-harvest.

The 1895 Russian Legation panorama exists twice, as a 445px catalogue image
and as two 1,024px halves in lot 11948; the 1904 Chemulpo landing lot and the
1910 "Korean customs" set are in. Stereographs are flagged (`stereo`) so the
poster can crop one frame of the pair.
"""

import json
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from harvest_common import load_keyed_json, save_keyed_json

OUTPUT = Path(__file__).parent / 'seoul_loc.json'
SEARCH = 'https://www.loc.gov/photos/'
UA = 'Old-Seoul-Bot/1.0 (personal project; contact: https://chris-stanford.com)'
# Listing pages are cheap; item records are what the catalogue throttles.
PAGE_DELAY = 3
ITEM_DELAY = 6
# Everything from 1945 on is restricted news-agency material; see the module
# note. The bound is the year the record's own date field starts with.
LAST_YEAR = 1944
ALLOWED_RIGHTS = re.compile(r'^\s*no known restrictions on publication', re.I)
SEOUL = re.compile(r'seoul|s[eé]oul|keijo|kyongsong', re.I)
DIVISION = 'prints and photographs division'
COLLECTION_NOISE = re.compile(
    r'prints and photographs division|prints & photographs online catalog|'
    r'^catalog$|american memory|miscellaneous items in high demand|'
    r'^groups of images', re.I)


def fetch_json(url, params=None):
    """Parsed JSON, or None after four tries. A 429 is waited out for 30 s
    before the retry; the listing and item endpoints both throttle."""
    params = dict(params or {}, fo='json')
    full = f'{url}?{urllib.parse.urlencode(params)}'
    for attempt in range(4):
        result = subprocess.run(
            ['curl', '-sS', '--max-time', '90', '-A', UA, '-w', '\n%{http_code}', full],
            capture_output=True, text=True)
        body, _, status = result.stdout.rpartition('\n')
        if result.returncode == 0 and status == '200':
            try:
                return json.loads(body)
            except ValueError:
                pass
        if status == '429':
            time.sleep(30)
        time.sleep(6 * (attempt + 1))
    return None


def year_of(record):
    m = re.match(r'\s*(\d{4})', str(record.get('date') or ''))
    return int(m.group(1)) if m else None


def search_candidates():
    """Every pre-1945 Seoul hit in Prints & Photographs, from the listing.

    Returns None if any page could not be read: a partial listing would be
    written as a smaller pool and read as the Library having withdrawn items.
    """
    found, page = {}, 1
    while True:
        d = fetch_json(SEARCH, {'q': 'seoul', 'c': 100, 'sp': page})
        if d is None:
            return None
        for r in d.get('results', []):
            partof = [str(p).lower() for p in (r.get('partof') or [])]
            fmt = [str(f).lower() for f in (r.get('original_format') or [])]
            y = year_of(r)
            if DIVISION not in partof or 'photo, print, drawing' not in fmt:
                continue
            if y is None or y > LAST_YEAR or not r.get('id'):
                continue
            found[r['id']] = r
        pg = d.get('pagination') or {}
        if not pg.get('next'):
            return found
        page += 1
        time.sleep(PAGE_DELAY)


def largest_jpeg(record):
    """(url, width) of the widest JPEG the item serves, or (None, 0)."""
    best, width = None, 0
    for res in record.get('resources') or []:
        for files in res.get('files') or []:
            for f in files:
                if f.get('mimetype') == 'image/jpeg' and (f.get('width') or 0) > width:
                    best, width = f.get('url'), f.get('width') or 0
    return best, width


def prose(parts):
    """The sentences of a LoC description, minus the physical description
    ('1 photographic print.', '1 negative : glass ; 5 x 7 in. or smaller.')
    that the catalogue puts in the same field."""
    out = []
    for p in parts or []:
        for piece in str(p).split(' | '):
            piece = piece.strip()
            if piece and not re.match(r'^\d+\s+(photographic|negative|print|photomechanical|stereograph)', piece, re.I):
                out.append(piece)
    return ' '.join(out)


def item_id_of(url):
    m = re.search(r'/item/([^/]+)/?', url or '')
    return m.group(1) if m else None


def parse_item(record, listing):
    """One pool entry, or (None, reason) where the record cannot be posted."""
    it = record.get('item') or {}
    rights = str(it.get('rights_advisory') or it.get('rights_information') or '')
    if isinstance(it.get('rights_advisory'), list):
        rights = ' '.join(map(str, it['rights_advisory']))
    if not ALLOWED_RIGHTS.search(rights):
        return None, f'rights: {rights[:70] or "(none stated)"}'
    image, width = largest_jpeg(record)
    if not image:
        return None, 'no JPEG served'
    title = str(it.get('title') or listing.get('title') or '').strip()
    if not title:
        return None, 'no title'
    created = it.get('created_published')
    created = ' '.join(map(str, created)) if isinstance(created, list) else str(created or '')
    date_raw = str(it.get('date') or listing.get('date') or '')
    m = re.search(r'between (\d{4}) and (\d{4})', created)
    year = f'{m.group(1)}-{m.group(2)}' if m else (date_raw[:4] if re.match(r'\d{4}', date_raw) else '')
    description = prose(it.get('description'))
    notes = [str(n) for n in (it.get('notes') or [])]
    partof = [str(p.get('title') if isinstance(p, dict) else p) for p in (it.get('partof') or [])]
    collection = [p for p in partof if not COLLECTION_NOISE.search(p)]
    medium = ' '.join(map(str, it.get('medium') or []))
    stereo = bool(re.search(r'stereograph', f'{medium} {" ".join(partof)}', re.I))
    named = bool(SEOUL.search(f'{title} {description} {" ".join(notes)}'))
    return {
        'id': item_id_of(listing.get('id')) or item_id_of(it.get('id')),
        'title': title,
        'year': year,
        'date': date_raw,
        'created_published': created,
        'description': description,
        'collection': collection[:3],
        'medium': medium,
        'stereo': stereo,
        'seoul_named': named,
        'image_url': image,
        'image_width': width,
        'detail_url': listing.get('id') or it.get('id'),
        'rights': rights,
        'licence': 'no-known-restrictions',
        'author': 'Library of Congress',
    }, ''


def load_existing():
    return load_keyed_json(OUTPUT)


def save(items_by_id):
    save_keyed_json(OUTPUT, items_by_id)


def main():
    argv = sys.argv[1:]
    unknown = [a for a in argv if a not in ('--sample', '--force') and not a.isdigit()]
    if unknown:
        sys.exit(f'unknown argument: {unknown[0]}')
    force = '--force' in argv
    sample = None
    if '--sample' in argv:
        idx = argv.index('--sample')
        sample = int(argv[idx + 1]) if idx + 1 < len(argv) else 10

    existing = load_existing()
    print(f'{len(existing)} items already harvested', flush=True)

    print('Searching Prints & Photographs for "seoul"...', flush=True)
    candidates = search_candidates()
    if candidates is None:
        sys.exit('a listing page could not be read; nothing written')
    print(f'  {len(candidates)} pre-{LAST_YEAR + 1} photograph records', flush=True)

    to_fetch = sorted(u for u in candidates if item_id_of(u) not in existing)
    if sample:
        to_fetch = to_fetch[:sample]
    print(f'{len(to_fetch)} item records to read, {ITEM_DELAY} s apart', flush=True)

    skipped = []
    for n, url in enumerate(to_fetch, 1):
        record = fetch_json(url)
        if record is None:
            skipped.append((url, 'record could not be read'))
            print(f'  [{n}/{len(to_fetch)}] {url}: could not be read', flush=True)
        else:
            item, reason = parse_item(record, candidates[url])
            if item is None:
                skipped.append((url, reason))
                print(f'  [{n}/{len(to_fetch)}] {url}: SKIPPED ({reason})', flush=True)
            else:
                existing[item['id']] = item
                print(f'  [{n}/{len(to_fetch)}] {item["id"]}: {item["title"][:70]} '
                      f'({item["year"] or "no date"}, {item["image_width"]}px'
                      f'{", stereo" if item["stereo"] else ""}'
                      f'{", Seoul" if item["seoul_named"] else ""})', flush=True)
        if n % 25 == 0:
            save(existing)
        time.sleep(ITEM_DELAY)

    on_disk = len(load_existing())
    if len(existing) < on_disk and not force:
        sys.exit(f'refusing to write {len(existing)} items over {on_disk} '
                 f'already on disk; pass --force if this is deliberate')

    save(existing)
    named = sum(1 for it in existing.values() if it['seoul_named'])
    stereo = sum(1 for it in existing.values() if it['stereo'])
    print(f'\n{len(existing)} items in {OUTPUT.name}: {named} name Seoul, '
          f'{stereo} stereographs', flush=True)
    if skipped:
        print(f'{len(skipped)} skipped:')
        for url, reason in skipped:
            print(f'  {url}: {reason}')


if __name__ == '__main__':
    main()
