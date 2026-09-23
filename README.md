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

Five pools, posted from as one. Each item's format, tags and credit follow the
pool it came from.

| Pool | Source | Period | Postable |
| --- | --- | --- | --- |
| `seoul_archive.json` | [Seoul Metropolitan Archives](https://archives.seoul.go.kr) | 1950s-90s municipal photography | ~9,500 |
| `seoul_dryplate.json` | [National Museum of Korea](https://www.museum.go.kr/dryplate/main.do), Government-General glass plates | 1909-1945 | 1,452 |
| `seoul_gazette.json` | [Seoul Metropolitan Archives](https://archives.seoul.go.kr/newspaper), 서울시보 cartoons and adverts | 1982-83 | 169 of 2,573 |
| `seoul_gongu.json` | [한국정책방송원 KTV](https://gongu.copyright.or.kr), via 공유마당 | 1950s-70s news photography | 333 |
| `seoul_loc.json` | [Library of Congress](https://www.loc.gov/pictures/), Prints & Photographs | 1895-1911 stereographs, Carpenter album, studio scenes | ~150 |

⚠️ **KTV and gazette shares are configured, not left to pool size.** Both pull
down the archive pool's officials-at-ceremonies bias, so an unweighted draw
would surface each too rarely to matter. `SOURCES['gongu']['share'] = 0.20`
puts KTV in one post in five (~2.5 days apart, lasting ~2 years);
`SOURCES['gazette']['share'] = 0.10` puts the gazette in one in ten (~5 days
apart, ~2.5 years), and `SOURCES['loc']['share'] = 0.10` does the same for
the Library of Congress. The two big photo pools split the remaining share in
proportion to their own sizes. See `draw_weights`, which also covers the
gazette being absent, being the only source (`--source gazette`), and shares
misconfigured past 1.

The archive records carry a year and a Korean description, so their posts lead
with the date. The glass plates are catalog entries: title, subject path and
place, with no description and a 촬영 연도 on only 284 of the 1,452. Leading with
a date alone would print "date unknown" on four posts in five, so those posts
lead with the district and no description is written for them — inventing one
from a title the model cannot check would be fabrication, not translation. The
date follows the district: the year where the catalog has one ("Jongno-gu,
1946"), and "date unknown" where it does not. The year used to be dropped from
the header while the alt text still stated it, so the post contradicted its own
description.

A third pool, `seoul_gazette.json`, holds 2,573 articles of the 1982-83 city
gazette. **Only its 169 cartoons, comic strips and advertisements are posted** —
`서울만평` and `주사 새서울씨`, both by 정운경, plus `광고` — because an item there is a
region of a page and the other 2,404 crop to a picture of Korean newsprint
rather than a picture.
Those posts lead with the exact publication date, drop `#photography` (a
pen-and-ink cartoon is not a photograph) and have their picture cut out of the
page scan on the way through. See sections 1c and 2.

## How it works

The bot runs in two stages.

### 1. Harvest (`seoul_harvest.py`)

A one-off crawler that walks the archive's photo catalog and builds a local
pool of postable items in `seoul_archive.json`. For each item it records the
Korean title, year, description, keywords and the full-resolution (2000px)
image URLs.

It is deliberately polite and conservative:

- **One request per second** (`DELAY = 1.0`).
- **Public, unrestricted items only** — it keeps an item only when the archive
  marks its access type as public (`공개`) and its usage type as unrestricted
  (`제한없음`), and skips everything else.
- **Resumable** — it skips item IDs already in `seoul_archive.json` and saves
  progress every 50 items, so an interrupted run can be restarted.

A full harvest takes a few hours. `--sample N` fetches N evenly-spaced items for
a quick test run.

### 1b. Harvest the glass plates (`seoul_dryplate_harvest.py`)

Pulls the Seoul subset of the National Museum of Korea's glass-plate catalog
into `seoul_dryplate.json`: 1,452 records, 146 pages, about three minutes at one
request per second. It records the Korean and hanja titles, accession number,
subject path, region, plate size, year and photographer where known.

The search endpoint has three quirks worth knowing before editing it: the paging
parameter is `page` and not `currentPage` (which is accepted and ignored), the
full set of form fields must be posted or the response comes back empty, and
`pageSize` is pinned at 10 server-side.

### 1c. Harvest the city gazette (`seoul_gazette_harvest.py`)

Pulls 서울시보, the Seoul city gazette, into `seoul_gazette.json`: 2,573 articles
across 64 issues and 256 pages, 7 January 1982 to 13 October 1983, about nine
minutes at one request per second. The archives hold roughly 500 issues and say
360 are queued, so re-running it picks up whatever they have released since.

**The cartoons and strips are wired into `seoul_post.py`; the other 2,466
articles are not.** An item here is a region of a page rather than a file:

- Records carry `page_image` plus `box`, `coords` and `page_width`/`page_height`.
  Whatever posts them has to crop. The archives' boxes are **tight, and sometimes
  short** — the 7 January 1982 comic strip's own box clips its fourth panel, and
  about 20px of padding recovers it — so pad and clamp rather than cropping the
  raw box.
- 738 of the 2,573 regions (28%) are `poly` rather than `rect`, wrapped in an L
  around their neighbors, so their bounding box contains part of another
  article. The raw `coords` are kept so those can be masked instead.
- Every article has the archives' own Korean transcription in `text`, which is
  real prose to translate rather than a title alone. Two exceptions to expect:
  the cartoons transcribe to the artist's name and the org charts to a literal
  `X`, so for those the title is the content.
- The bot posts three slots, all of which crop clean: 54 `서울만평` editorial
  cartoons, 53 `주사 새서울씨` four-panel strips (both by 정운경) and 62 `광고`
  advertisements. `GAZETTE_SLOTS` in `seoul_post.py` is both the roster and the
  filter — adding a key there starts posting that slot.
- ⚠️ **Five of the 67 advertisements are excluded** by `gazette_postable`,
  which requires a notice to have a transcription beyond its headline. Those
  five are two instalments of a 순화대상 행정용어 glossary and three lists of
  office codes: their whole content is the table printed in the image, so there
  is nothing to translate and the crop is a wall of unreadable type. A cartoon
  has no such rule, because for a cartoon the caption alone is the content.

Two requests per unit of work, and both are needed: the listing gives 발행번호,
date and **title**, the viewer gives the page image, its dimensions and every
article's coordinates and **transcription**. They join on `contentSeq`, which is
opaque and must be taken from the listing — it looks like issue×100+n until issue
1 article 1 turns out to be 1 rather than 101.

Endpoint quirks worth knowing before editing this:

- **`newsPaperSeq` is required alongside `pageSeq`.** Without it the viewer
  returns an empty stub — HTTP 200, no article data — that otherwise looks
  like a healthy response.
- **`<br/>` is the only markup to strip from a transcription.** Everything
  else between angle brackets (author affiliations, photo captions,
  sub-headings) is real Korean editorial content, so a generic `<[^>]+>` strip
  would delete it.
- **A few `coords` arrays are empty in the archives' own data.** Those items
  keep their transcription with `box: null` rather than being dropped.

`--sample N` harvests N evenly-spaced listing pages for a quick test run, and
`--out PATH` writes somewhere other than `seoul_gazette.json`. A re-run
**preserves the `posted` flags** of records already on disk and merges rather
than replaces, so a sample run against the real pool is safe. The script exits
non-zero if any article failed to join, any id was duplicated or any page could
not be read: an incomplete harvest must not read as a clean one.

`robots.txt` allows `/newspaper` and `/upload`. It disallows `/catalog/`, where
the archives' document and drawing records live, and this script never goes
there.

### 1d. Harvest the Library of Congress (`seoul_loc_harvest.py`)

Reads every pre-1945 record the Library's Prints & Photographs division
returns for "seoul", opens each item record, keeps the ones whose rights
advisory reads "No known restrictions on publication" and that serve a JPEG,
and writes `seoul_loc.json`. About 150 name Seoul in their own title,
description or notes; the Korea-wide stereographs the search also returns are
kept in the file, flagged `seoul_named: false`, and not posted.

⚠️ **It cannot run from Seoul.** loc.gov's catalogue answers every request
from a Korean address with a Cloudflare challenge that never clears, in curl
and in a real browser alike, while the same URLs answer plainly from a US
address. The image host `tile.loc.gov` is not walled, so posting works from
here; only the harvest needs a US egress. `.github/workflows/loc-harvest.yml`
runs it on a GitHub runner and returns the pool as an artifact rather than a
commit, since the pool files are runtime state:

```bash
gh workflow run loc-harvest.yml
gh run download <run-id> -n seoul_loc     # writes seoul_loc.json here
```

⚠️ **Item records are read six seconds apart.** At 1.2 s the catalogue
answered 429 after six and refused everything after; at 6 s, 114 in a row
passed. A full harvest is about 25 minutes.

### 2. Post (`seoul_post.py`)

Picks a random item that hasn't been posted yet from the combined pool,
translates its Korean title (and description, where the source has one) into
concise English, formats the bilingual caption with a topic emoji, uploads up to
four images (each with descriptive alt text) and publishes to Bluesky. The item
is then marked `posted` in its own pool so it is never repeated.

Three things vary by source rather than globally:

- **Tags.** `#photography` goes on a photograph. The gazette's drawings take
  the same set without it (`PHOTO_TAGS` / `DRAWING_TAGS`).
- **The picture.** A gazette record points at a whole broadsheet page, so
  `crop_article` cuts the article out of it, padded by 20px because the
  archives' boxes are tight. ⚠️ It checks the scan's real size against the size
  the record declares and refuses on a mismatch: the coordinates are expressed
  in the declared one, and a silent mis-crop would ship for months.
- **The date.** A gazette record states its exact publication day, so the
  header is that day and the model is never asked to find one. ⚠️ The Library
  of Congress pads a bare year to `1904-01-01`, so that pool declares
  `day_is_placeholder` and its header is the year alone; a record that only
  says "between 1910 and 1920" prints exactly that.
- **The language.** The Library of Congress captions are English, written at
  the time, and they are posted verbatim after the style pass: "The 'Hermit
  Kingdom' awakening" is a document of 1904 and the date above it says so.
  What the model writes for that pool is the *Korean* line, and
  `check_korean` reads it against the English, the mirror of the check every
  other pool gets (`korean_line_checked`). A Korean line flagged twice redraws
  the item; there is no English-only branch, because every post on this
  account is bilingual.
- **Stereographs.** A stereograph card carries two near-identical frames and a
  printed caption. `stereo_frame` finds the left frame from the pixels (the
  mount is flat, the picture is not, so the frame is the longest run of
  high-variance columns and rows in the left half) and posts that alone,
  falling back to the plain left half if it finds nothing plausible.

⚠️ **`translate_gazette` must never describe the artifact.** It is shown the
archives' transcription and nothing else. A caption reading `지하철 급진전` invites
"drawn as a star-shaped crater", which is a fine sentence about a picture the
model has not seen. The artifact is described by `image_alt.describe()`, which
is shown the actual pixels — and needs no special case for a notice, returning
"Newspaper advertisement in Korean, dense vertical columns of text under a bold
headline", which complements the caption instead of repeating it.

It has **two prompts, because they are different jobs**. A strip's
transcription IS its words and is short enough to render whole: translation. An
advertisement's runs to a median 593 characters, so a hundred characters of
English is a *summary*, which is where a model rounds a specific condition into
a tidier wrong one. `_gazette_notice_prompt` therefore asks for concrete
particulars and says to drop what will not fit rather than generalise it.

⚠️ **The notice description is capped at 100 characters, not the larger budget
it looks like it should have** — a gazette headline runs far longer than a
photo caption, so 100 characters is what actually fits most adverts.
`format_post` trims the rest at a word boundary.

The model's image description passes through `educate_quotes` on its way into
the alt text, so alt matches the caption: `reading “서울특별시”`, not
`reading '서울특별시'`. Applied to the description alone, not to the assembled
string — the provenance lead and the disclosure are ours and carry no marks.
Note this also promotes a matched pair of single quotes to double, which is
house style but is a behavior change and not only a character swap.

**A signed drawing credits its artist**: `✏️ 정운경` sits above the source
credit. ⚠️ The name comes from the record's OWN title, never from the slot.
Four of the 107 name nobody the source can confirm, and they get no credit
line: printing the slot's usual artist over a drawing the archives left
unsigned would be an attribution we invented. Adverts have no artist and get
no line.

⚠️ **Eight of the 107 are wordless** — title and transcription hold nothing but
the tag and the artist's name — and `gazette_has_words` skips the model
entirely for those, posting them under the slot name alone. Asked to translate
nothing, the model replied `{"gist": "Artist name"}` and then explained itself
in prose after the closing fence, which was invalid JSON and took the whole run
down. `_first_json_object` now recovers a trailing-prose reply rather than
spending a retry on it, for every prompt in the file.

`capitalize_after_colon` capitalises the first word after a colon, so a title
built as `<strip name>: <gist>` reads "Cartoon: The subway races ahead". House
style, enforced in code for the reason `promote_single_quotes` exists: an
instruction the model follows most of the time still ships the exception.

Translation is done by calling the [`claude` CLI](https://docs.claude.com/en/docs/claude-code/overview)
(`claude -p`, Haiku model), which returns a compact JSON object with the English
title and a one-sentence description in British date style. Every such call,
the translation check's included, runs `--restricted --tools ""` since
11 September 2026: no tools at all, for the reason given under `image_alt.py`
below.

Two rules in the prompt: a description **keeps a reason the Korean gives**
(dropping the cause behind an effect reads as a non sequitur), and it **does
not strengthen a verb** (단속 is a crackdown, not a seizure). The photograph is
not a license for either — the caption is made from the text.

### 2b. Describe the image (`image_alt.py`)

Alt text is generated separately from the caption, by a second model call
(`describe()`) shown only the image's pixels — never the caption, so it
cannot paraphrase provenance instead of describing what is actually visible.
Since 27 August 2026 the description is checked against the image before it
ships: a further call locates every concrete claim in the description and
flags anything it cannot find; on a failure the description is retried once,
naming what could not be verified, and a second failure drops it. Any
failure along the way — generation, verification, a spent quota — falls
back to a plain citation-only alt rather than holding the post, on the
principle that a missing description is not worth a missing post. A
generated description is prefixed `A.I.-generated description:`
(`image_alt.DISCLOSURE`); the citation fallback is not, since it is
catalog metadata rather than a model's claim.

Both calls run `claude -p --restricted --tools Read` (`image_alt.CONFINED`)
since 11 September 2026. Unconfined, `claude -p` is an agent with a shell,
not a vision endpoint: in a sibling bot it was found cropping images through
a dozen tool calls, and once running the project's own code and returning a
progress report as the answer. Confined, the model can read the one image in
its working directory and nothing else, and answers in about seven seconds a
call rather than ten.

### 3. Check the English against the Korean (`check_translation`)

Nothing used to. Accuracy rested on instructions inside the translation
prompts, and everything after them was style: quotes, thousands separators,
capitalisation. `check_translation` now reads the Korean and the English
together, on the stronger model, and reports only claims the Korean does not
support.

It runs **before** the post, because a published post cannot be corrected in
place: `putRecord` succeeds and the appview goes on serving the old text, so
fixing a live caption means a new record and a deleted one, at the cost of the
permalink. A check that runs afterwards can only ever report.

What counts as a problem is narrow, and what does not is spelled out at
length, because a checker that flags wording it would have chosen differently
costs good posts:

- a statement the Korean does not support, or contradicts
- a name, place, institution, number or date rendered wrongly
- a detail kept without the fact that explains it, so the English leaves a
  season or a figure standing there unexplained
- a verb that goes further than the Korean: a crackdown rendered as a seizure,
  an inspection as a raid. What is visible in the photograph does not license
  it, because the caption is made from the text
- **not** anything merely left out: both lines are capped at 60 and 100
  characters and dropping material is expected
- **not** style, length, romanization, or anything about the picture, which
  nobody in this chain has seen

The prompt also tells the checker what kind of English it is reading, because
otherwise it is wrong in both directions at once: a glass plate's description
is empty by design, and a municipal notice is a summary that is *supposed* to
leave things out.

On a flag: retranslate once, then drop the description, or drop the item
entirely if the title is what failed. A dropped item is not marked posted, so
it comes round again another day for another reading.

`stray_years` is the deterministic half and needs no model: a year in the
English that is nowhere in the record was invented. Years only — Korean counts
in units of 만 and 억, so a digit-for-digit test on quantities reads a correct
expansion of 300만원 as three million invented won.

Every verdict is logged to `translation_checks.jsonl`, **including the drafts
it rejected**. Those never reach the feed, so the log is the only place a
false alarm can ever be seen.

A verdict that changed what shipped is also reported to `~/Scripts/observe.py`
if that file exists, which is a shared log a weekly review reads, so a check
firing three weeks running is visible as one recurring condition rather than
three unrelated events. A dropped description and a rejected title are filed
separately, a retry that then passed is not a finding at all, and the case
worth having most is the check that could not *run*: posts keep going out when
it fails, so a checker broken for a week looks exactly like a quiet week.
Nothing here depends on it: with no `observe.py` the bot runs unchanged.

## Reliability

Four small modules support the run itself, separate from the posting logic:

- **`net_guard.py`** — `wait_for_network()` checks for a working network path
  before the run starts (a routeless-but-associated Wi-Fi state that ping and
  a cached DNS answer can both still look healthy through). No path out backs
  off and retries up to a budget; missing the budget exits 0 with one log
  line rather than a traceback mid-run.
- **`limit_guard.py`** — waits out a spent `claude -p` usage-limit reply
  instead of losing the run to it, since the refusal names the moment it
  clears. Anything that isn't a usage limit (an expired token, say) still
  raises, so a real fault is never mistaken for a quota wait.
- **`alt_log.py`** — appends the alt text each post shipped, and whether it
  was model-generated or fell back to a citation, to `alt_history.jsonl`.
  Best-effort: a logging failure is warned about, never raised.
- **`api_call_log.py`** — wraps `subprocess.run` (curl calls), `requests` and
  `httpx` (which is what the atproto client uses to post) so every outbound
  call this bot makes is logged with its target host to a shared
  `~/Scripts/api_calls.jsonl`, the same log every opted-in script across the
  estate writes to. Installed only in `seoul_post.py`'s real `__main__` block,
  so importing the module for tests never activates it. Observation only: it
  never alters a call's behavior or return value.

Both guards exit 0 on a skipped run rather than failing loudly, which is only
safe because something outside this repo (`bot_health_check.py`) alerts when
the bot itself has gone quiet.

## Requirements

- Python 3.9+
- The [`claude` CLI](https://docs.claude.com/en/docs/claude-code/overview),
  installed and authenticated — `seoul_post.py` shells out to it for
  translation.
- A Bluesky account and an [app password](https://bsky.app/settings/app-passwords)
- macOS — the Bluesky password is read from the macOS Keychain via the
  `security` command. On other platforms, adapt the `keychain_password`
  function to your own secret store.

Install the Python dependencies:

```bash
pip install -r requirements.txt
```

## Secrets

The bot reads one secret from the macOS Keychain. Add it once:

```bash
# Bluesky app password for your bot account
security add-generic-password -a "oldhanyang.bsky.social" -s "seoulbot-bluesky" -w
```

If you use a different Bluesky handle or Keychain entry name, edit the `HANDLE`
and `KEYCHAIN_SERVICE` constants near the top of `seoul_post.py`.

## Usage

Build the archive first (once), then post from it:

```bash
python3 seoul_harvest.py             # full harvest → seoul_archive.json
python3 seoul_harvest.py --sample 20 # fetch 20 items for a quick test
python3 seoul_dryplate_harvest.py    # glass plates → seoul_dryplate.json
python3 seoul_gazette_harvest.py     # city gazette → seoul_gazette.json (~9 min)
python3 seoul_gazette_harvest.py --sample 5   # 5 spread listing pages

python3 seoul_post.py                     # translate, format and post one item
python3 seoul_post.py --dry-run           # translate and format without posting
python3 seoul_post.py --source dryplate   # restrict the pool to one source
python3 seoul_post.py --tail 10           # show recent alt text, post nothing
python3 seoul_post.py --checks 20         # show recent translation checks
```

## Data files

All live alongside the scripts and are gitignored:

- `seoul_archive.json` — the Seoul Metropolitan Archives pool. Built by
  `seoul_harvest.py`; each item is flagged `posted` once used.
- `seoul_dryplate.json` — the glass-plate pool. Built by
  `seoul_dryplate_harvest.py`; flagged `posted` the same way.
- `seoul_gazette.json` — the 1982-83 서울시보 pool. Built by
  `seoul_gazette_harvest.py`; only its cartoons and adverts are postable.
- `seoul_gongu.json` — the KTV news-photography pool. Built by
  `seoul_gongu_harvest.py`; flagged `posted` the same way.
- `seoul_loc.json` — the Library of Congress pool. Built by
  `seoul_loc_harvest.py` on a US runner; flagged `posted` the same way.
- `seoul_state.json` — records `last_success_at`, the timestamp of the most
  recent successful post, and the recent topic emojis driving the cooldown.
- `alt_history.jsonl` — one line per posted item, recording the alt text that
  shipped and whether it was generated or fell back to a citation.
- `translation_checks.jsonl` — one line per translation checked, the rejected
  drafts included. Read it with `--checks`.

## Scheduling

Run `seoul_post.py` on a schedule (the live bot posts twice a day). With cron:

```cron
0 9,21 * * * cd /path/to/old-seoul && /usr/bin/python3 seoul_post.py >> seoul_post.log 2>&1
```

Re-run `seoul_harvest.py` occasionally to pick up items the archive has added
since the last crawl.

## Source material and attribution

Every post links back to its original item, and English captions are
AI-generated and labeled as such.

**[Seoul Metropolitan Archives](https://archives.seoul.go.kr)** — the harvester
keeps only items the archive marks as public (`공개`) and unrestricted
(`제한없음`), and skips everything else.

**[National Museum of Korea](https://www.museum.go.kr/dryplate/main.do)** — the
glass plates are published under
[공공누리 제1유형](https://www.kogl.or.kr) (KOGL Type 1: free use, including
commercial and derivative use, on the single condition of attribution). The
museum credit in the caption and in every image's alt text is that attribution,
so it is a license term rather than a courtesy and must not be dropped.

**[Library of Congress](https://www.loc.gov/pictures/)** — every item in the
pool carries the Prints & Photographs division's own advisory, "No known
restrictions on publication", read off its record at harvest and kept on the
record as `rights`. The Library asks for no credit; the credit line is the
reader's route to the catalogue, and the link rides on it.

A note on what these photographs are: the glass plates were made from 1909 to
about 1945 by the Japanese Government-General's survey of Korean antiquities,
and the photographer credits are the surveying officials. They are a colonial
record of Seoul, and worth reading as one.

## License

[MIT](LICENSE) — applies to this bot's code, not to the photographs, which
remain subject to their respective institutions' terms.
