#!/usr/bin/env python3
"""Harvest 서울시보, the Seoul city gazette (1982-1983), from the Seoul
Metropolitan Archives into a single JSON file.

The gazette is a municipal newspaper the archives have digitised article by
article: 64 issues, 256 pages, 2,573 articles as of 22 August 2026. They hold
about 500 issues and say 360 are queued, so the corpus grows.

Unlike the two photo pools, an item here is a REGION OF A PAGE, not a file of
its own. Each record therefore carries the page image URL plus the article's
own coordinates on it; whatever posts these has to crop. In exchange every
article comes with the archives' own Korean transcription, so there is real
text to translate instead of a catalog title alone.

Two requests per unit of work, and both are needed:
  - the listing at /newspaper gives 발행번호, date and TITLE, and is the only
    place the title exists;
  - the viewer at /newspaper/content gives the page image, its dimensions and
    every article's coordinates and transcription, and is the only place those
    exist.
They join on contentSeq. One viewer request covers a whole page, so the run is
~258 listing requests plus ~256 viewer ones: about nine minutes at one a second.

Established by probing on 22 August 2026, each the hard way:

  - `newsPaperSeq` is REQUIRED alongside `pageSeq`. Without it the viewer
    returns a 26,713-byte stub with `href: '/upload'`, no data and HTTP 200 —
    a perfectly healthy-looking empty page. Hence PAGE_EMPTY_HREF.

  - `<br/>` IS THE ONLY MARKUP IN A TRANSCRIPTION. Everything else between
    angle brackets is Korean editorial content: author affiliations
    (`<수필가>`, `<동작구 도시정비과>`), photo captions (`<사진설명>`),
    sub-headings. A generic `<[^>]+>` strip — the obvious cleaner, and what
    seoul_dryplate_harvest.py's clean() does — deleted 27 such tokens from a
    60-article sample, so roughly two articles in five would have lost text
    with nothing to show it had happened. Only <br> forms are touched here.
    There are no HTML entities in the corpus at all; html.unescape is applied
    anyway, being harmless on text that has none.

  - `contentSeq` IS NOT A FORMULA, whatever it looks like. Issue 32 article 43
    is 3243 and issue 64 article 49 is 6449, which reads as issue*100+n until
    issue 1 article 1 turns out to be 1 rather than 101. It is an opaque id and
    is only ever taken from the listing.

  - The archives' own bounding boxes are TIGHT, and sometimes short. The
    7 January 1982 comic strip's box clips the fourth panel; about 20px of
    padding recovers it. The raw box is stored unpadded, because that is what
    the source says, and page_width/page_height are stored with it so a
    consumer can pad and clamp. Pad before cropping.

  - A few articles have an EMPTY coords array in the archives' own data
    (`coords : [ , ]` — contentSeq 5421, one of the 2,573 as of 22 August
    2026). The transcription is fine and only the box is missing, so the record
    is kept with `box: null` and counted in the report. Anything that crops must
    skip those; treating them as a join failure would throw the article away and
    blame the parse for it.

  - A `poly` region is L-shaped around its neighbors, so its bounding box
    contains part of another article. Both `box` and the raw `coords` are kept
    so a consumer can mask rather than crop. 738 of the 2,573 are polys.

robots.txt allows /newspaper and /upload. It disallows /catalog/, which is
where the archives' document and drawing records live — this script never goes
there.

Usage:
    python3 seoul_gazette_harvest.py                  # full harvest
    python3 seoul_gazette_harvest.py --sample 5       # 5 spread listing pages
    python3 seoul_gazette_harvest.py --out other.json
"""

import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

BASE = "https://archives.seoul.go.kr"
LIST = f"{BASE}/newspaper"
VIEW = f"{BASE}/newspaper/content"
OUTPUT = Path(__file__).parent / "seoul_gazette.json"

# Shelling out to curl, not urllib: this Python's CA bundle is not wired up and
# urllib dies with CERTIFICATE_VERIFY_FAILED. Same approach as the other two
# harvesters here.
UA = "Seoul-Archive-Bot/1.0 (personal project; contact: https://chris-stanford.com)"

DELAY = 1.0          # seconds between requests, matching seoul_harvest.py
RETRIES = 3

# The viewer's answer when newsPaperSeq is missing: HTTP 200, no article data,
# and an image href that is the upload directory rather than a file.
PAGE_EMPTY_HREF = "/upload"

# One row of the listing: 발행번호, 발행일, 제목, and the viewer link.
ROW_RE = re.compile(
    r"<td>(?P<number>\d{3}-\d{2,3})</td>\s*"
    r"<td>(?P<date>\d{4}-\d{2}-\d{2})</td>\s*"
    r'<td class="text-left">(?P<title>.*?)</td>\s*'
    r'<td><a href="(?P<href>/newspaper/content\?[^"]+)"',
    re.S,
)
PAGES_RE = re.compile(r'class="lb pageinfo">\s*/\s*(\d+)\s*<')

# The viewer's page image and its pixel dimensions, which the coordinates below
# are expressed in. Both pages checked matched the JPEG's real size, but it is
# read rather than assumed: a mismatch would silently mis-crop every article.
ORGIMG_RE = re.compile(
    r"const orgImg\s*=\s*\{\s*href:\s*'(?P<href>[^']*)'\s*,\s*"
    r"width:\s*(?P<width>\d+)\s*,\s*Height:\s*(?P<height>\d+)",
    re.S,
)

# One article region. Field order is fixed in the markup; coords hold only
# digits, commas and whitespace, so [^\]]* cannot run past the array.
REGION_RE = re.compile(
    r"\{\s*shape:\s*'(?P<shape>\w+)'\s*,\s*"
    r"coords\s*:\s*\[(?P<coords>[^\]]*)\]\s*,\s*"
    r"id:\s*'(?P<id>\d+)'\s*,\s*"
    r"description\s*:\s*`(?P<text>.*?)`\s*\}",
    re.S,
)

# `[서울만평] …` -> 서울만평. The gazette tags most of its articles this way,
# and the tag is what separates a cartoon from a public notice.
TAG_RE = re.compile(r"^\[([^\]]{1,30})\]")

BR_RE = re.compile(r"(?i)<br\s*/?>")


def fetch(url):
    args = ["curl", "-s", "--max-time", "60", "-H", f"User-Agent: {UA}", url]
    last = None
    for attempt in range(RETRIES):
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout:
            return result.stdout
        last = result.stderr or f"exit {result.returncode}, empty body"
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{url} failed after {RETRIES} tries: {last}")


def clean_line(fragment):
    """Collapse a listing cell to one line. Deliberately does NOT strip tags:
    see the <수필가> note at the top. Anything tag-shaped that turns up here is
    left visible rather than silently deleted."""
    text = html.unescape(fragment).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def clean_text(fragment):
    """A transcription: <br/> becomes a newline, everything else is left be."""
    text = BR_RE.sub("\n", fragment)
    text = html.unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def parse_listing(page_html):
    """Rows of the listing, each with the viewer coordinates it links to."""
    rows = []
    for m in ROW_RE.finditer(page_html):
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(html.unescape(m.group("href"))).query
        )
        try:
            rows.append({
                "number": m.group("number"),
                "date": m.group("date"),
                "title": clean_line(m.group("title")),
                "page_seq": int(query["pageSeq"][0]),
                "content_seq": query["contentSeq"][0],
                "paper_seq": int(query["newsPaperSeq"][0]),
            })
        except (KeyError, IndexError, ValueError):
            # A link missing one of the three parameters cannot be fetched, and
            # dropping it quietly would lose an article. Say so instead.
            print(f"  ! unusable viewer link on {m.group('number')}", flush=True)
    return rows


def bounding_box(coords):
    """Bounding box of a coords list, as [left, top, right, bottom].

    A rect is already its own box; a poly is not, and the difference is why the
    raw pairs are kept alongside this.
    """
    nums = [int(n) for n in re.findall(r"\d+", coords)]
    if len(nums) < 4 or len(nums) % 2:
        return None, []
    xs, ys = nums[0::2], nums[1::2]
    return [min(xs), min(ys), max(xs), max(ys)], nums


def parse_page(page_html):
    """(image href, width, height, {contentSeq: region}) for one gazette page.

    Returns None for the empty stub the viewer serves when newsPaperSeq is
    missing, which is HTTP 200 and looks entirely normal.
    """
    org = ORGIMG_RE.search(page_html)
    if not org or org.group("href").rstrip("/") == PAGE_EMPTY_HREF:
        return None

    regions = {}
    for m in REGION_RE.finditer(page_html):
        # box is None where the archives' own coords array is empty — a defect
        # in their data, not in this parse. The transcription is still good, so
        # the record is kept with a null box and counted separately; dropping it
        # would lose the article and read as a join failure.
        box, coords = bounding_box(m.group("coords"))
        regions[m.group("id")] = {
            "shape": m.group("shape"),
            "box": box,
            "coords": coords,
            "text": clean_text(m.group("text")),
        }
    return {
        "href": org.group("href"),
        "width": int(org.group("width")),
        "height": int(org.group("height")),
        "regions": regions,
    }


def issue_and_page(href):
    """(64, 4) from '/upload/newspaper/064/04.jpg'. The path is authoritative:
    it is what the archives actually serve, so it is preferred over the issue
    implied by 발행번호, and the two are cross-checked by the caller."""
    m = re.search(r"/newspaper/(\d+)/(\d+)\.\w+$", href)
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def load_existing(path):
    """Existing records keyed by id, so a re-run keeps the `posted` flags.
    Without this every re-harvest would empty the bot's memory and it would
    start posting articles it has already published."""
    if not path.exists():
        return {}
    try:
        return {item["id"]: item for item in json.loads(path.read_text())}
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        sys.exit(f"{path} exists but could not be read ({exc}). "
                 f"Refusing to overwrite it — move it aside to start fresh.")


def write_json(path, records):
    """Atomic, because this file carries the posted flags and a half-written
    one would lose them."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(records, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def harvest(out_path, sample=None):
    existing = load_existing(out_path)
    print(f"{len(existing)} records already on disk", flush=True)

    first = fetch(LIST)
    pages = PAGES_RE.search(first)
    if not pages:
        sys.exit("could not read the listing's page count — the markup has changed")
    pages = int(pages.group(1))

    wanted = list(range(1, pages + 1))
    if sample:
        step = max(1, len(wanted) // sample)
        wanted = wanted[::step][:sample]
        print(f"sample mode: {len(wanted)} of {pages} listing pages", flush=True)
    else:
        print(f"{pages} listing pages", flush=True)

    rows = []
    for n, page in enumerate(wanted, 1):
        page_html = first if page == 1 else fetch(f"{LIST}?page={page}")
        batch = parse_listing(page_html)
        if not batch:
            print(f"  ! listing page {page} yielded no rows", flush=True)
        rows.extend(batch)
        if n % 50 == 0 or n == len(wanted):
            print(f"  listing {n}/{len(wanted)} — {len(rows)} articles", flush=True)
        if n < len(wanted):
            time.sleep(DELAY)

    # One viewer request per page, however many of its articles are listed.
    page_keys = list(dict.fromkeys((r["page_seq"], r["paper_seq"]) for r in rows))
    print(f"\n{len(page_keys)} gazette pages to fetch", flush=True)

    pages_by_key, failed = {}, []
    for n, (page_seq, paper_seq) in enumerate(page_keys, 1):
        url = f"{VIEW}?pageSeq={page_seq}&newsPaperSeq={paper_seq}"
        parsed = parse_page(fetch(url))
        if parsed is None:
            failed.append((page_seq, paper_seq))
            print(f"  ! pageSeq={page_seq} newsPaperSeq={paper_seq}: empty page",
                  flush=True)
        elif not parsed["regions"]:
            # A page that parses but holds no regions means the data block's
            # shape has changed. Never silently treated as "no articles here".
            failed.append((page_seq, paper_seq))
            print(f"  ! pageSeq={page_seq} newsPaperSeq={paper_seq}: "
                  f"image but no article regions — markup may have changed",
                  flush=True)
        else:
            pages_by_key[(page_seq, paper_seq)] = parsed
        if n % 25 == 0 or n == len(page_keys):
            print(f"  pages {n}/{len(page_keys)}", flush=True)
        if n < len(page_keys):
            time.sleep(DELAY)

    records, unjoined, duplicates = {}, [], []
    for row in rows:
        page = pages_by_key.get((row["page_seq"], row["paper_seq"]))
        if not page:
            unjoined.append(row["number"])
            continue
        region = page["regions"].get(row["content_seq"])
        if not region:
            # The listing names an article the page does not draw a box for.
            unjoined.append(row["number"])
            continue

        issue, page_no = issue_and_page(page["href"])
        if issue is not None and issue != int(row["number"].split("-")[0]):
            print(f"  ! {row['number']} sits on issue {issue}'s page — "
                  f"trusting the image path", flush=True)

        record = {
            "id": row["content_seq"],
            "number": row["number"],
            "issue": issue,
            "page": page_no,
            "date": row["date"],
            "year": row["date"][:4],
            "title": row["title"],
            "tag": (TAG_RE.match(row["title"]).group(1)
                    if TAG_RE.match(row["title"]) else ""),
            "text": region["text"],
            "page_image": f"{BASE}{page['href']}",
            "page_width": page["width"],
            "page_height": page["height"],
            "shape": region["shape"],
            "box": region["box"],
            "coords": region["coords"],
            "detail_url": (f"{VIEW}?pageSeq={row['page_seq']}"
                           f"&contentSeq={row['content_seq']}"
                           f"&newsPaperSeq={row['paper_seq']}"),
            # Preserved across re-runs: see load_existing.
            "posted": existing.get(row["content_seq"], {}).get("posted", False),
        }
        if record["id"] in records:
            duplicates.append(record["number"])
            continue
        records[record["id"]] = record

    # Sample runs cover part of the corpus, so anything already harvested and
    # not seen this time is kept rather than dropped.
    merged = dict(existing)
    merged.update(records)
    write_json(out_path, list(merged.values()))
    return merged, records, rows, unjoined, duplicates, failed


def report(merged, fresh, rows, unjoined, duplicates, failed, out_path):
    print(f"\nwrote {len(merged)} records to {out_path} "
          f"({len(fresh)} from this run)")

    if fresh:
        dates = sorted(r["date"] for r in fresh.values())
        polys = sum(1 for r in fresh.values() if r["shape"] != "rect")
        empty = sum(1 for r in fresh.values() if not r["text"])
        boxless = sum(1 for r in fresh.values() if not r["box"])
        tags = {}
        for r in fresh.values():
            tags[r["tag"] or "(untagged)"] = tags.get(r["tag"] or "(untagged)", 0) + 1
        print(f"  {dates[0]} to {dates[-1]}")
        print(f"  {polys} poly regions ({polys * 100 // len(fresh)}%) — "
              f"their boxes overlap neighbors, mask with coords")
        if empty:
            print(f"  {empty} with no transcription")
        if boxless:
            print(f"  {boxless} with box: null — the archives' own coords are "
                  f"empty, so these cannot be cropped")
        top = sorted(tags.items(), key=lambda kv: -kv[1])[:8]
        print("  tags: " + ", ".join(f"{k} {v}" for k, v in top))

    if unjoined:
        print(f"\n  {len(unjoined)} listed articles had no region on their page: "
              f"{', '.join(unjoined[:10])}{' …' if len(unjoined) > 10 else ''}")
    if duplicates:
        print(f"  {len(duplicates)} duplicate contentSeq, kept the first: "
              f"{', '.join(duplicates[:10])}")
    if failed:
        print(f"  {len(failed)} pages could not be read: "
              + ", ".join(f"pageSeq={p} newsPaperSeq={n}" for p, n in failed[:10]))

    # An incomplete harvest must not read as a clean one.
    return 1 if (unjoined or duplicates or failed) else 0


def main():
    out_path = OUTPUT
    if "--out" in sys.argv:
        out_path = Path(sys.argv[sys.argv.index("--out") + 1])

    sample = None
    if "--sample" in sys.argv:
        sample = int(sys.argv[sys.argv.index("--sample") + 1])

    sys.exit(report(*harvest(out_path, sample), out_path=out_path))


# Refuse anything unrecognized before the flags above are read. A bare
# membership test silently ignores what it does not recognize, so a typo
# (`--sampel`) or a reflex (`--help`) reads as no flag at all and takes the
# ordinary path — the trap that published a real seoul-index thread on
# 20 July 2026, and what harden_audit.sh check 10 looks for.
_KNOWN_ARGS = {"--sample", "--out"}

if __name__ == "__main__":
    # The value after each flag belongs to that flag, so it must not be read as
    # an unknown argument.
    _skip = set()
    for _flag in ("--sample", "--out"):
        if _flag in sys.argv:
            _i = sys.argv.index(_flag)
            if _i + 1 < len(sys.argv):
                _skip.add(_i + 1)
    _unknown = [a for j, a in enumerate(sys.argv[1:], 1)
                if a not in _KNOWN_ARGS and j not in _skip]
    if _unknown:
        sys.exit(f'Unknown argument(s): {" ".join(_unknown)}. '
                 f'Recognized: --sample N, --out PATH. Refusing to run.')
    if "--sample" in sys.argv:
        _i = sys.argv.index("--sample")
        if _i + 1 >= len(sys.argv) or not sys.argv[_i + 1].isdigit():
            sys.exit("--sample needs a positive integer.")
    if "--out" in sys.argv and sys.argv.index("--out") + 1 >= len(sys.argv):
        sys.exit("--out needs a path.")
    main()
