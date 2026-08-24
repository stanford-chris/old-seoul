#!/usr/bin/env python3
"""
Harvest the 한국정책방송원 (KTV) photographs from 공유마당 into seoul_gongu.json.

Why this pool exists. The Seoul Metropolitan Archives pool is a magnificent
record of the city's ADMINISTRATION and a thin record of its street: measured
24 August 2026, 63% of its 9,570 items carry a mayor's name or an
official-event word, and eight of its twenty commonest keywords are the names
of Seoul mayors. 87 records are 행운의 열쇠, the key to the city. That is what
the archive is, not a harvesting fault -- its own year facet sums to 9,112 and
this bot already holds essentially all of it, so no amount of re-harvesting
changes the mix.

This pool is the corrective. Of the KTV photographs naming a Seoul place, 10%
are official events. What the rest are: 도동 일대의 판자촌, 미아동 난민정착촌,
무교동 재개발 철거 현장, 영등포 수해현장, 와우아파트 붕괴, 대연각호텔 화재,
광화문 거리, 세종로 거리, 서소문거리, 주택가 어린이 놀이터, 서울시 공설 무료
스케이트장. Shanty towns, floods, demolition, a hotel fire, children's
playgrounds and a free public ice rink.

    python3 seoul_gongu_harvest.py              # harvest (resumable)
    python3 seoul_gongu_harvest.py --sample 20  # 20 items, for testing
    python3 seoul_gongu_harvest.py --force      # write a SMALLER pool anyway

⚠️ **The licence is checked per item and only `01` is kept.** 공유마당 mixes
공공누리 types, CC licences, 기증저작물 and 만료저작물 in one listing, and only
공공누리 제1유형 (출처표시) permits what this bot does -- 제2유형 forbids
commercial use, 제3유형 forbids derivatives, and a bot cannot judge which of
those it is doing. Every KTV item read on 24 August 2026 was `01`, so this
check has never yet excluded anything: that is the point. It is here for the
day the provider adds one that is not, which would otherwise be posted in
breach without a word. Matched POSITIVELY on the badge, never as "not 02".

⚠️ **Seoul-ness is decided at harvest, never at post time.** KTV's 1,000
photographs are national; only about a third are Seoul. A pool that held the
rest would eventually post Busan to an account whose every word promises Seoul.
Two signals are combined because each misses what the other catches: the site's
own keyword search (which reads the description too) and a place-name test on
the title. Title-matching alone finds 167, the search alone 325.

⚠️ **The full-size image is the `filePath` URL WITHOUT `thumbAt`, and an item
whose full image cannot be found is SKIPPED rather than falling back to the
thumbnail.** The thumbnail is a few hundred pixels and would post as a smeared
square, which is the sort of failure that looks like a bad photograph rather
than a bad harvest. Note the originals are modest anyway: 서울 광화문 거리 is
600x499, against the archives pool's 2000px scans.
"""

import json
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

OUTPUT = Path(__file__).parent / 'seoul_gongu.json'
BASE = 'https://gongu.copyright.or.kr'
LIST = f'{BASE}/gongu/wrt/wrtCl/listWrtImage.do'
VIEW = f'{BASE}/gongu/wrt/wrt/view.do'
# 한국정책방송원. The provider codes come from the listing page's own
# searchSrcTrgetInttCd select; 49 is KTV and holds 1,000 images.
PROVIDER = '49'
# 공공누리 제1유형 출처표시, read from the badge image on each listing row.
ALLOWED_LICENCE = '01'
DELAY = 0.7
# A bare curl gets HTTP 400 from this host; it wants a browser User-Agent.
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'

# Place names that make a title Seoul. Districts, the landmarks KTV actually
# photographed, and the neighbourhoods its captions name. Deliberately literal:
# a looser test (any Korean place word) would drag in the national material
# this pool exists to exclude.
# ⚠️ 한양 is NOT in this list, though it is Seoul's own historical name: the
# only title it matched in the whole corpus was 한양호 진수식, the launch of a
# SHIP named after the city, which would have been posted as a Seoul street
# photograph. A place name that is also a proper noun earns its place only if
# it actually finds something.
PLACES = (
    '서울', '경성',
    '명동', '종로', '청계천', '남대문', '동대문', '광화문', '세종로', '서소문',
    '을지로', '충무로', '소공동', '무교동', '한강', '여의도', '남산', '시청',
    '경복궁', '창경궁', '창덕궁', '덕수궁', '독립문', '노량진', '영등포', '신촌',
    '마포', '왕십리', '뚝섬', '미아동', '창신동', '금호동', '도동', '이촌동',
    '장충', '용산', '성북', '종암', '흑석동', '상도동', '북악', '워커힐',
)

# On the listing each row pairs its id and title with its licence badge, in
# that order. Read together in ONE pattern so an item can never be paired with
# the neighbouring row's licence.
ROW_RE = re.compile(
    r'view\.do\?wrtSn=(\d+)&amp;menuNo=200018">\s*'
    r'<div class="photoCon_head">\s*(.*?)\s*</div>'
    r'.*?img_license(\d+)\.png', re.S)
# ⚠️ The item's own image is identified by its wrtSn MATCHING THE ITEM, not by
# its position or its CSS class: the page also carries a row of related items,
# and they use the same container class. Two markup shapes exist -- some pages
# expose the file plainly, others append thumbAt/thumbSe/wrtTy -- and on the
# items checked both return byte-identical files, so the small `t_thumb`
# variant is the only one that must be avoided.
def full_image(item_id, html):
    srcs = re.findall(r'<img src="(/gongu/wrt/cmmn/wrtFileImageView\.do\?'
                      r'wrtSn=%s&[^"]*)"' % re.escape(item_id), html)
    full = [u for u in srcs if 't_thumb' not in u]
    return BASE + full[0].replace('&amp;', '&') if full else None
OG_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]*)"')
CREATED_RE = re.compile(r'<dt>창작년도</dt>\s*<dd>\s*([\d-]+)\s*</dd>')
SIZE_RE = re.compile(r'<dt>원저작물 크기</dt>\s*<dd>\s*([\d*x ]+)\s*</dd>')


def fetch(url):
    """None on any transport failure. Never raises: a blip must not lose the
    items already harvested."""
    result = subprocess.run(
        ['curl', '-sS', '--max-time', '40', '-A', UA, url],
        capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def strip_marks(text):
    """The search endpoint wraps matched terms in <!HS>…<!HE> highlight
    markers, which are not part of the title and must never reach a post."""
    return re.sub(r'<!H[SE]>', '', text).strip()


def listing(pages, **params):
    """{id: (title, licence)} across the given listing pages.

    Returns None if any page could not be read: a partial listing would look
    exactly like a provider that had removed half its collection, and the
    caller must not write a shrunken pool on that evidence.
    """
    rows = {}
    for page in pages:
        query = urllib.parse.urlencode(
            {'menuNo': '200018', 'pageUnit': '100', 'pageIndex': page,
             **params})
        html = fetch(f'{LIST}?{query}')
        if html is None:
            return None
        for ident, title, licence in ROW_RE.findall(html):
            rows[ident] = (strip_marks(title), licence)
        time.sleep(DELAY)
    return rows


def seoul_ids(rows, searched):
    """The subset that is about Seoul, by either signal. See the module note."""
    return {i for i, (title, _) in rows.items()
            if i in searched or any(p in title for p in PLACES)}


def parse_item(item_id, html):
    """One record, or None where the page does not carry what a post needs."""
    title = OG_TITLE_RE.search(html)
    image = full_image(item_id, html)
    if not title or not image:
        return None
    created = CREATED_RE.search(html)
    date = created.group(1) if created else ''
    size = SIZE_RE.search(html)
    return {
        'id': item_id,
        'title': strip_marks(title.group(1)),
        # Year for the post header, full date kept because these records carry
        # an exact day and throwing it away cannot be undone later.
        'year': date[:4],
        'date': date,
        'image_url': image,
        'detail_url': f'{VIEW}?wrtSn={item_id}&menuNo=200018',
        'size': size.group(1).strip() if size else '',
        'licence': 'KOGL-1',
        'author': '한국정책방송원',
    }


def load_existing():
    if not OUTPUT.exists():
        return {}
    return {it['id']: it for it in json.loads(OUTPUT.read_text())}


def save(items_by_id):
    OUTPUT.write_text(json.dumps(list(items_by_id.values()),
                                 ensure_ascii=False, indent=1) + '\n')


def main():
    argv = sys.argv[1:]
    unknown = [a for a in argv if a not in ('--sample', '--force')
               and not a.isdigit()]
    if unknown:
        # A bare membership test let `--help` fall through to a live post in
        # seoul_post.py once. Refuse anything unrecognised here too.
        sys.exit(f'unknown argument: {unknown[0]}')
    force = '--force' in argv
    sample = None
    if '--sample' in argv:
        idx = argv.index('--sample')
        sample = int(argv[idx + 1]) if idx + 1 < len(argv) else 20

    existing = load_existing()
    print(f'{len(existing)} items already harvested')

    print(f'Listing provider {PROVIDER} (한국정책방송원)...')
    everything = listing(range(1, 11), searchSrcTrgetInttCd=PROVIDER)
    if everything is None:
        sys.exit('a listing page could not be read; nothing written')
    print(f'  {len(everything)} KTV images')

    print('Listing the same provider, searched for 서울...')
    searched = listing(range(1, 5), searchSrcTrgetInttCd=PROVIDER,
                       searchWrd='서울')
    if searched is None:
        sys.exit('the keyword listing could not be read; nothing written')
    print(f'  {len(searched)} matched the search')

    wanted = seoul_ids(everything, set(searched))
    print(f'{len(wanted)} are about Seoul by title or search')

    # ⚠️ Positive licence match. Anything that is not 공공누리 제1유형 is dropped
    # and NAMED, never silently.
    refused = {i for i in wanted if everything[i][1] != ALLOWED_LICENCE}
    for i in sorted(refused):
        print(f'  !! {i} refused: licence badge {everything[i][1]}, '
              f'not {ALLOWED_LICENCE} ({everything[i][0]})')
    wanted -= refused

    to_fetch = sorted(wanted - set(existing))
    if sample:
        to_fetch = to_fetch[:sample]
    print(f'{len(to_fetch)} to fetch')

    failed = []
    for n, item_id in enumerate(to_fetch, 1):
        html = fetch(f'{VIEW}?wrtSn={item_id}&menuNo=200018')
        item = parse_item(item_id, html) if html else None
        if not item:
            failed.append(item_id)
            print(f'  [{n}/{len(to_fetch)}] {item_id}: SKIPPED '
                  f'(no full-size image or title on the page)')
        else:
            existing[item_id] = item
            print(f'  [{n}/{len(to_fetch)}] {item_id}: {item["title"]} '
                  f'({item["date"] or "no date"}, {item["size"] or "?"})')
        if n % 25 == 0:
            save(existing)
        time.sleep(DELAY)

    # ⚠️ Anti-clobber, as everywhere else here except the CSV exporter: a run
    # that would SHRINK the pool refuses. A provider outage that returned half
    # its listing would otherwise quietly halve the bot's material, and the
    # only evidence would be a feed that got samey again.
    on_disk = len(load_existing())
    if len(existing) < on_disk and not force:
        sys.exit(f'refusing to write {len(existing)} items over {on_disk} '
                 f'already on disk; pass --force if this is deliberate')

    save(existing)
    print(f'\n{len(existing)} items in {OUTPUT.name}')
    if failed:
        print(f'{len(failed)} could not be parsed: {", ".join(failed)}')


if __name__ == '__main__':
    main()
