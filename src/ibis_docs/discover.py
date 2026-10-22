"""Find the PDFs to process (E2).

Every file in the input folder is either found (ready to extract) or skipped with a plain reason.
Files are identified by content hash, so a renamed copy of a file processed in an earlier run is
skipped. Byte-identical files within one run are both found; the copy is marked `duplicate_of`
so duplicate detection (E6) can flag it without paying to extract it twice.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

# Subfolders that hold earlier runs' files (E9); never rescanned.
RESULT_FOLDERS = frozenset({"processed", "needs-review"})

# The Claude API's request size limit, which a base64-encoded PDF has to fit in.
MAX_BYTES = 32 * 1024 * 1024
# base64 grows a file by a third; leave room for the prompt.
MAX_PDF_BYTES = MAX_BYTES * 3 // 4 - 64 * 1024

SEEN_FILE = Path(".ibis") / "seen.json"


@dataclass(frozen=True)
class Found:
    path: Path
    sha256: str
    pages: int
    duplicate_of: str | None = None  # name of an identical file found earlier in this run

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class Skipped:
    path: Path
    reason: str

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class Discovery:
    found: list[Found]
    skipped: list[Skipped]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_pdf(path: Path, max_pages: int) -> tuple[int | None, str | None]:
    """Return (page count, None) for a PDF Claude can read, or (None, reason) for one it can't."""
    size = path.stat().st_size
    if size == 0:
        return None, "empty file"
    with path.open("rb") as f:
        head = f.read(1024)
    if b"%PDF-" not in head:
        return None, "not a PDF"
    if size > MAX_PDF_BYTES:
        return None, f"too large ({size / 1024 / 1024:.0f} MB; the limit is about 24 MB)"

    # pypdf logs every quirk it repairs; those are noise here, and could name document contents.
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            return None, "password-protected (remove the password and add it again)"
        pages = len(reader.pages)
    except (PdfReadError, OSError, ValueError, KeyError, TypeError) as exc:
        return None, f"unreadable PDF ({type(exc).__name__})"
    if pages == 0:
        return None, "PDF has no pages"
    if pages > max_pages:
        return None, f"too many pages ({pages}; max_pages is {max_pages})"
    return pages, None


def discover(
    input_dir: Path, max_pages: int, seen: set[str] | frozenset[str] = frozenset()
) -> Discovery:
    """Scan `input_dir` (top level only). `seen` holds hashes processed in earlier runs."""
    if not input_dir.is_dir():
        raise FileNotFoundError(f"input folder not found: {input_dir}")

    found: list[Found] = []
    skipped: list[Skipped] = []
    first_by_hash: dict[str, str] = {}

    for path in sorted(input_dir.iterdir(), key=lambda p: p.name.casefold()):
        # Hidden files (.DS_Store, macOS "._" resource forks) and our own folders aren't inputs.
        if path.name.startswith(".") or not path.is_file():
            continue
        pages, reason = check_pdf(path, max_pages)
        if reason:
            skipped.append(Skipped(path, reason))
            continue
        digest = sha256_of(path)
        if digest in seen:
            skipped.append(Skipped(path, "already processed in an earlier run (same contents)"))
            continue
        found.append(Found(path, digest, pages or 0, duplicate_of=first_by_hash.get(digest)))
        first_by_hash.setdefault(digest, path.name)

    return Discovery(found, skipped)


def load_seen(output_dir: Path) -> set[str]:
    """Hashes of files processed in earlier runs."""
    path = output_dir / SEEN_FILE
    if not path.is_file():
        return set()
    return set(json.loads(path.read_text(encoding="utf-8")).get("files", {}))


def record_seen(output_dir: Path, files: list[Found]) -> None:
    """Add files to the seen list. Stores names and hashes only, never contents."""
    path = output_dir / SEEN_FILE
    data = {"files": {}}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for f in files:
        data["files"].setdefault(f.sha256, {"name": f.name, "first_seen": now})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
