#!/usr/bin/env python3
"""Harvest the Seoul glass-plate (유리건판) records from the National Museum of
Korea's dedicated 조선총독부박물관 유리건판 site into a single JSON file.

Probe script: this pulls metadata only, so the spread can be inspected before
anything is wired into the Old Seoul bot.

Search endpoint quirks, established by probing on 15 August 2026:
  - The paging parameter is `page`, NOT `currentPage` (which is silently ignored).
  - The full set of form fields must be posted or the response comes back empty.
  - `pageSize` is pinned at 10 server-side; passing anything else changes nothing.
  - Region 서울특별시 is IDR2=A09, under IDR1=A (한국).
"""

import html
import json
import re
import subprocess
import sys
import time
import urllib.parse

BASE = "https://www.museum.go.kr/dryplate"
SEARCH = f"{BASE}/search_detail_view.do"
IMG = f"{BASE}/imagePath.do?check=E&imgFile=pan{{relicnum}}.jpg"
# Shelling out to curl, not urllib: this Python's CA bundle is not wired up and
# urllib dies with CERTIFICATE_VERIFY_FAILED. Same approach as seoul_harvest.py.
UA = "Seoul-Archive-Bot/1.0 (personal project; contact: https://chris-stanford.com)"

# Every field the page's own pageForm submits. Omitting any of them yields an
# empty result set, so they are sent verbatim even where blank.
FORM = {
    "KEYWORD": "", "research": "",
    "IDS1": "", "IDS2": "", "IDS3": "", "IDS4": "",
    "IDR1": "A", "IDR2": "A09", "IDR3": "",
    "relicnum_start": "", "relicnum_end": "",
    "PHOTOGRAPHYEAR_start": "", "PHOTOGRAPHYEAR_end": "",
    "Sizecheck": "",
}

# Label in the markup -> key in the output JSON.
FIELDS = {
    "한자명칭": "name_hanja",
    "소장품 번호": "accession",
    "분야": "subject",
    "지역": "region",
    "건판 크기": "plate_size",
    "촬영 연도": "year",
    "조사자/촬영자": "photographer",
    "촬영 당시 기록자료": "contemporary_record",
    "참고자료": "reference",
}

DELAY = 1.0          # seconds between requests, matching seoul_harvest.py
RETRIES = 3


def clean(fragment):
    """Strip tags and entities from a markup fragment, collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def fetch(page):
    data = urllib.parse.urlencode({**FORM, "page": page})
    args = [
        "curl", "-s", "--max-time", "60", "-X", "POST", SEARCH,
        "-H", f"User-Agent: {UA}",
        "-H", "Content-Type: application/x-www-form-urlencoded; charset=UTF-8",
        "-H", f"Referer: {BASE}/search_detail.do",
        "--data", data,
    ]
    last = None
    for attempt in range(RETRIES):
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout:
            return result.stdout
        last = result.stderr or f"exit {result.returncode}, empty body"
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"page {page} failed after {RETRIES} tries: {last}")


def parse(page_html):
    records = []
    # Each result is a <div class="search_box">; split rather than regex the
    # whole block, since the markup carries stray unclosed tags.
    for block in page_html.split('<div class="search_box">')[1:]:
        block = block.split('<div class="file_box">')[0]

        relicnum = re.search(r"searchplate_view\.do\?relicnum=(\d+)", block)
        title = re.search(r"<h3>(.*?)</h3>", block, re.S)
        if not relicnum:
            continue
        relicnum = relicnum.group(1)

        rec = {
            "relicnum": relicnum,
            "title": clean(title.group(1)) if title else "",
            "image_url": IMG.format(relicnum=relicnum),
            "detail_url": f"{BASE}/searchplate_view.do?relicnum={relicnum}",
        }

        for li in re.findall(r'<li class="search_list">(.*?)</li>', block, re.S):
            text = clean(li)
            for label, key in FIELDS.items():
                if text.startswith(label):
                    value = text[len(label):].lstrip(" :").strip()
                    # "소장품 번호 : 건판  000003" -> keep the full accession
                    rec[key] = value if value != "-" else ""
                    break

        for key in FIELDS.values():
            rec.setdefault(key, "")
        records.append(rec)
    return records


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "seoul_dryplate.json"

    first = fetch(1)
    total = re.search(r"결과:(\d+)건", first)
    if not total:
        sys.exit("could not read the result count — the page markup has changed")
    total = int(total.group(1))
    pages = (total + 9) // 10
    print(f"{total} Seoul records across {pages} pages", flush=True)

    records, seen = [], set()
    for page in range(1, pages + 1):
        page_html = first if page == 1 else fetch(page)
        batch = parse(page_html)
        if not batch:
            print(f"  page {page}: EMPTY", flush=True)
        for rec in batch:
            if rec["relicnum"] not in seen:
                seen.add(rec["relicnum"])
                records.append(rec)
        if page % 20 == 0 or page == pages:
            print(f"  page {page}/{pages} — {len(records)} records", flush=True)
        if page < pages:
            time.sleep(DELAY)

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=1)

    print(f"\nwrote {len(records)} records to {out_path}")
    if len(records) != total:
        print(f"WARNING: expected {total}, got {len(records)}")


if __name__ == "__main__":
    main()
