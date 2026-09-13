#!/usr/bin/env python3
"""Shared load/save helpers for the resumable, dict-keyed-by-id harvest pools.

seoul_harvest.py, seoul_gongu_harvest.py and seoul_loc_harvest.py each keep
their pool on disk as a JSON array and in memory as a dict keyed by each
item's own id, so a re-run can skip whatever it already fetched. The three
copies were identical in shape and differed only in incidental formatting
(indent width, trailing newline), which this keeps per-caller rather than
unifying, so no existing pool file's on-disk format changes.

seoul_gazette_harvest.py's load/save are a different shape on purpose (an
atomic write plus a corrupt-file refusal, protecting the `posted` flags) and
are not this. seoul_dryplate_harvest.py is a non-resumable probe with no
load/save pair at all.
"""

import json
from pathlib import Path


def load_keyed_json(path: Path, key: str = 'id') -> dict:
    """{item[key]: item} from a JSON array on disk, or {} if none exists yet."""
    if not path.exists():
        return {}
    return {item[key]: item for item in json.loads(path.read_text())}


def save_keyed_json(path: Path, items_by_id: dict, indent: int = 1,
                     trailing_newline: bool = True) -> None:
    """Write items_by_id.values() back out as a JSON array."""
    text = json.dumps(list(items_by_id.values()), ensure_ascii=False, indent=indent)
    if trailing_newline:
        text += '\n'
    path.write_text(text)
