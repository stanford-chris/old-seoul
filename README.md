# Old Seoul

A Bluesky bot that posts historical images of Seoul from five collections:
Library of Congress photographs from 1895 to 1911, colonial-era glass plates,
municipal photographs, mid-century news photography and the cartoons from the
city's own 1980s newspaper. It runs as
[@oldhanyang.bsky.social](https://bsky.app/profile/oldhanyang.bsky.social).

Each post pairs the image with a bilingual caption, topic hashtags and a link
back to the original item. One language is the source's own title and the
other is a translation written by an AI model: for the four Korean collections
the English is the translation, and for the Library of Congress, whose
catalogue captions are English, the Korean is. The bot's bio says the
translations are AI-generated.

## Sources

Five pools, posted from as one (`SOURCES` in `seoul_post.py`). Each item's
format, tags and credit follow the pool it came from.

| Pool | Source | Period | Postable |
| --- | --- | --- | --- |
| `seoul_archive.json` | [Seoul Metropolitan Archives](https://archives.seoul.go.kr) | 1950s-90s municipal photography | ~9,500 |
| `seoul_dryplate.json` | [National Museum of Korea](https://www.museum.go.kr/dryplate/main.do), Government-General glass plates | 1909-1945 | 1,452 |
| `seoul_gazette.json` | [Seoul Metropolitan Archives](https://archives.seoul.go.kr/newspaper), 서울시보 cartoons and adverts | 1982-83 | 169 of 2,573 |
| `seoul_gongu.json` | [한국정책방송원 KTV](https://gongu.copyright.or.kr), via 공유마당 | 1950s-70s news photography | 333 |
| `seoul_loc.json` | [Library of Congress](https://www.loc.gov/pictures/), Prints & Photographs | 1895-1911 stereographs, Carpenter album, studio scenes | ~160 |

**The three small pools have configured shares.** Left to pool size they
would surface too rarely to matter, so `share` in `SOURCES` fixes them: KTV
at 0.20 (one post in five), the gazette and the Library of Congress at 0.10
each (one in ten). The two big photo pools split the rest in proportion to
their sizes. See `draw_weights`, which also handles a shared pool being
absent, being the only source (`--source gazette`) and shares summing past 1.

The archive records carry a year and a Korean description, so their posts
lead with the date. The glass plates are catalog entries (title, subject path
and place) with no description and a year on only 284 of the 1,452, so those
posts lead with the district, followed by the year or "date unknown"
("Jongno-gu, 1946"). No description is written for them: inventing one from a
title the model cannot check would be fabrication, not translation.

Only the gazette's cartoons, comic strips and advertisements are posted
(`서울만평` and `주사 새서울씨`, both by 정운경, plus `광고`), because an item there is
a region of a page and the other 2,404 articles crop to a picture of Korean
newsprint. Those posts lead with the exact publication date, drop
`#photography` and have their picture cut out of the page scan.

## How it works

### 1. Harvest

Each pool has its own one-off harvester. All but the glass-plate one accept
`--sample N` for a quick test.

- **`seoul_harvest.py`** walks the Seoul Metropolitan Archives photo catalog
  into `seoul_archive.json`: Korean title, year, description, keywords and
  2000px image URLs. One request per second; it keeps only items the archive
  marks public (`공개`) and unrestricted (`제한없음`), and saves progress every 50
  items. A full harvest takes a few hours.
- **`seoul_dryplate_harvest.py`** pulls the Seoul subset of the National
  Museum of Korea's glass-plate catalog into `seoul_dryplate.json` (1,452
  records, about three minutes). Endpoint quirks: the paging parameter is
  `page`, not `currentPage`; the full set of form fields must be posted or the
  response comes back empty; `pageSize` is pinned at 10 server-side.
- **`seoul_gazette_harvest.py`** pulls 서울시보 into `seoul_gazette.json`: 2,573
  articles across 64 issues, 7 January 1982 to 13 October 1983, about nine
  minutes. Re-running picks up issues the archives have released since and
  preserves `posted` flags. It exits non-zero if any article failed to join,
  any id was duplicated or any page could not be read. See below.
- **`seoul_gongu_harvest.py`** pulls KTV news photographs from 공유마당 into
  `seoul_gongu.json`, keeping only items licensed 공공누리 제1유형 (출처표시),
  checked per item.
- **`seoul_loc_harvest.py`** reads every pre-1945 Prints & Photographs record
  for "seoul", keeps those whose rights advisory reads "No known restrictions
  on publication" and that serve a JPEG, and writes `seoul_loc.json`. Records
  that do not name Seoul in their own title, description or notes are kept,
  flagged `seoul_named: false`, and not posted. Item records are read six
  seconds apart; faster draws 429s. A full harvest is about 25 minutes.

**The Library of Congress harvest cannot run from Korea.** loc.gov answers
Korean addresses with a Cloudflare challenge that never clears, so the
harvest runs on a GitHub runner and returns the pool as an artifact rather
than a commit (the pool files are runtime state). The image host
`tile.loc.gov` is not walled, so posting works from anywhere.

```bash
gh workflow run loc-harvest.yml
gh run download <run-id> -n seoul_loc     # writes seoul_loc.json here
```

#### The gazette data

An item is a region of a page rather than a file. Records carry `page_image`
plus `box`, `coords` and `page_width`/`page_height`, and 738 of the 2,573
regions are polygons rather than rectangles, so the raw `coords` are kept.
Every article carries the archives' own Korean transcription in `text`; the
cartoons transcribe to the artist's name and the org charts to a literal `X`.

The bot posts three slots: 54 `서울만평` editorial cartoons, 53 `주사 새서울씨`
four-panel strips and 62 `광고` advertisements. `GAZETTE_SLOTS` in
`seoul_post.py` is both the roster and the filter. Five of the 67
advertisements are excluded by `gazette_postable`, which requires a notice to
have a transcription beyond its headline: their whole content is a table
printed in the image.

The harvester makes two requests per unit of work: the listing gives
발행번호, date and title, the viewer gives the page image, its dimensions and
every article's coordinates and transcription. They join on `contentSeq`,
which is opaque and must be taken from the listing. Quirks:

- `newsPaperSeq` is required alongside `pageSeq`, or the viewer returns an
  empty HTTP 200 stub.
- `<br/>` is the only markup to strip from a transcription. Everything else
  between angle brackets is real editorial content.
- A few `coords` arrays are empty in the archives' own data; those items keep
  their transcription with `box: null`.

`robots.txt` allows `/newspaper` and `/upload` and disallows `/catalog/`,
which this script never visits.

### 2. Post (`seoul_post.py`)

Picks a random unposted item from the combined pool, translates its title
(and description, where the source has one), formats the bilingual caption
with a topic emoji, uploads up to four images with alt text and publishes to
Bluesky. The item is then marked `posted` in its own pool.

What varies by source:

- **Tags.** `#photography` goes on photographs only (`PHOTO_TAGS` /
  `DRAWING_TAGS`).
- **The picture.** `crop_article` cuts a gazette article out of its page,
  padded by 20px because the archives' boxes are tight. It refuses if the
  scan's real size does not match the size the record declares, since the
  coordinates are expressed in the declared one.
- **Stereographs.** `stereo_frame` finds the left frame of a Library of
  Congress stereograph card from the pixels and posts that alone, falling
  back to the plain left half.
- **The date.** A gazette record states its exact publication day, so the
  header is that day. The Library of Congress pads a bare year to
  `1904-01-01`, so that pool declares `day_is_placeholder` and its header is
  the year alone.
- **The language.** Library of Congress captions are English, written at the
  time, and are posted verbatim after the style pass. The model writes the
  Korean line, and `check_korean` reads it against the English. A Korean line
  flagged twice redraws the item; every post is bilingual.
- **Artist credit.** A signed gazette drawing credits its artist (`✏️ 정운경`)
  above the source credit, taken from the record's own title, never assumed
  from the slot.

Translation calls the [`claude` CLI](https://docs.claude.com/en/docs/claude-code/overview)
(`claude -p`, Haiku model), which returns a compact JSON object with the
English title and a one-sentence description, dates in day-first form. These
calls run `--restricted --tools ""`: no tools at all. Two rules in the prompt:
a description keeps a reason the Korean gives, and it does not strengthen a
verb (단속 is a crackdown, not a seizure).

`translate_gazette` has two prompts: a strip's transcription is short enough
to translate whole, while an advertisement's must be summarized, so its
prompt asks for concrete particulars and says to drop what will not fit
rather than generalize it. The description is capped at 100 characters.
Wordless strips skip the model and post under the slot name alone
(`gazette_has_words`). `translate_gazette` never describes the artifact: that
is `image_alt.describe()`'s job.

House style is enforced in code rather than left to the prompt:
`capitalize_after_colon` ("Cartoon: The subway races ahead") and
`educate_quotes` on the alt text, so it matches the caption's curly quotes.

### 3. Describe the image (`image_alt.py`)

Alt text is generated by a second model call (`describe()`) shown only the
image's pixels, never the caption, so it describes what is visible rather
than paraphrasing provenance. The description is then checked against the
image: a further call locates every concrete claim and flags anything it
cannot find. A failed description is regenerated up to twice
(`MAX_REDESCRIBE`), each retry naming every claim rejected so far and banning
it outright, in any wording; if it still fails it is dropped. Any failure
(generation, verification, a spent quota) falls back to a plain citation-only
alt rather than holding the post. A generated description is prefixed
`A.I.-generated description:` (`image_alt.DISCLOSURE`); the citation fallback
is not, since it is catalog metadata.

Both calls run `claude -p --restricted --tools Read` (`image_alt.CONFINED`):
unconfined, `claude -p` is an agent with a shell, not a vision endpoint.
Confined, the model can read the one image in its working directory and
nothing else.

### 4. Check the translation (`check_translation`)

Before posting, `check_translation` reads the Korean and the English together
on the stronger model and reports only claims the Korean does not support. It
runs before the post because a published post cannot be corrected in place.

It flags:

- a statement the Korean does not support or contradicts
- a name, place, institution, number or date rendered wrongly
- a detail kept without the fact that explains it
- a verb that goes further than the Korean

It does not flag omissions (both lines are capped at 60 and 100 characters),
style, length, romanization or anything about the picture.

On a flag: retranslate once, then drop the description, or drop the item
entirely if the title failed. A dropped item is not marked posted, so it
comes round again. `stray_years` is the deterministic half: a year in the
English that is nowhere in the record was invented.

Every verdict, rejected drafts included, is logged to
`translation_checks.jsonl`. A verdict that changed what shipped is also
reported to `~/Scripts/observe.py` if that file exists; with no `observe.py`
the bot runs unchanged.

## Reliability

Four small modules support the run itself:

- **`net_guard.py`**: `wait_for_network()` checks for a working network path
  before the run, backs off and retries up to a budget, and exits 0 with one
  log line if the budget runs out.
- **`limit_guard.py`**: waits out a spent `claude -p` usage-limit reply
  instead of losing the run. Anything that isn't a usage limit still raises.
- **`alt_log.py`**: appends the alt text each post shipped, and whether it
  was generated or fell back to a citation, to `alt_history.jsonl`.
- **`api_call_log.py`**: logs every outbound call (curl, `requests`, `httpx`)
  with its target host to a shared `~/Scripts/api_calls.jsonl`. Installed only
  in `seoul_post.py`'s `__main__` block, and observation only.

Both guards exit 0 on a skipped run, which is safe only because something
outside this repo alerts when the bot has gone quiet.

## Requirements

- Python 3.9+
- The [`claude` CLI](https://docs.claude.com/en/docs/claude-code/overview),
  installed and authenticated
- A Bluesky account and an [app password](https://bsky.app/settings/app-passwords)
- macOS: the Bluesky password is read from the Keychain via `security`. On
  other platforms, adapt `keychain_password` to your own secret store.

```bash
pip install -r requirements.txt
```

## Secrets

The bot reads one secret from the macOS Keychain. Add it once:

```bash
# Bluesky app password for your bot account
security add-generic-password -a "oldhanyang.bsky.social" -s "seoulbot-bluesky" -w
```

If you use a different handle or Keychain entry name, edit the `HANDLE` and
`KEYCHAIN_SERVICE` constants near the top of `seoul_post.py`.

## Usage

Build the pools first (once), then post from them:

```bash
python3 seoul_harvest.py             # full harvest → seoul_archive.json
python3 seoul_harvest.py --sample 20 # fetch 20 items for a quick test
python3 seoul_dryplate_harvest.py    # glass plates → seoul_dryplate.json
python3 seoul_gazette_harvest.py     # city gazette → seoul_gazette.json (~9 min)
python3 seoul_gongu_harvest.py       # KTV → seoul_gongu.json
gh workflow run loc-harvest.yml      # Library of Congress, on a US runner

python3 seoul_post.py                     # translate, format and post one item
python3 seoul_post.py --dry-run           # translate and format without posting
python3 seoul_post.py --source dryplate   # restrict the pool to one source
python3 seoul_post.py --tail 10           # show recent alt text, post nothing
python3 seoul_post.py --checks 20         # show recent translation checks
```

## Data files

All live alongside the scripts and are gitignored:

- `seoul_archive.json`, `seoul_dryplate.json`, `seoul_gazette.json`,
  `seoul_gongu.json`, `seoul_loc.json`: the five pools, each built by its
  harvester. Every item is flagged `posted` once used.
- `seoul_state.json`: `last_success_at` and the recent topic emojis driving
  the cooldown.
- `alt_history.jsonl`: the alt text each post shipped and whether it was
  generated. Read it with `--tail`.
- `translation_checks.jsonl`: one line per translation checked, rejected
  drafts included. Read it with `--checks`.

## Scheduling

Run `seoul_post.py` on a schedule (the live bot posts twice a day). With cron:

```cron
0 9,21 * * * cd /path/to/old-seoul && /usr/bin/python3 seoul_post.py >> seoul_post.log 2>&1
```

Re-run the harvesters occasionally to pick up items the sources have added.

## Source material and attribution

Every post links back to its original item. The translated line is
AI-generated and labeled as such: English for four collections, Korean
for the Library of Congress, whose catalogue captions are English (see
"Sources" above).

**[Seoul Metropolitan Archives](https://archives.seoul.go.kr)**: the
photo harvester keeps only items the archive marks public (`공개`) and unrestricted
(`제한없음`).

**[National Museum of Korea](https://www.museum.go.kr/dryplate/main.do)** and
**[한국정책방송원 KTV](https://gongu.copyright.or.kr)**: published under
[공공누리 제1유형](https://www.kogl.or.kr) (KOGL Type 1: free use, including
commercial and derivative use, on the single condition of attribution). The
credit in the caption and in every image's alt text is that attribution, so
it is a license term and must not be dropped.

**[Library of Congress](https://www.loc.gov/pictures/)**: every item carries
the Prints & Photographs division's advisory "No known restrictions on
publication", read off its record at harvest and kept as `rights`. The
Library asks for no credit; the credit line is the reader's route to the
catalogue.

A note on what these photographs are: the glass plates were made from 1909 to
about 1945 by the Japanese Government-General's survey of Korean antiquities,
and the photographer credits are the surveying officials. They are a colonial
record of Seoul, and worth reading as one.

## License

[MIT](LICENSE): applies to this bot's code, not to the images, which remain
subject to their respective institutions' terms.
