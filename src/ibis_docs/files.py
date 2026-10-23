"""Move processed PDFs out of the inbox (E9): `ok` to processed/, anything else to needs-review/.

The moves happen in `run.keep`, after the exports are written. A name already taken in the target
folder gets a suffix ("invoice-2.pdf"); nothing is overwritten.
"""

from __future__ import annotations

from pathlib import Path

from ibis_docs.models import Status

PROCESSED = "processed"
NEEDS_REVIEW = "needs-review"


def folder_for(status: Status) -> str:
    return PROCESSED if status == Status.OK else NEEDS_REVIEW


def free_name(folder: Path, name: str, taken: set[Path] | frozenset[Path] = frozenset()) -> Path:
    """`folder/name`, or `folder/stem-2.ext`, `-3`, ... if that exists or is already `taken`."""
    target = folder / name
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 2
    while target.exists() or target in taken:
        target = folder / f"{stem}-{n}{suffix}"
        n += 1
    return target
