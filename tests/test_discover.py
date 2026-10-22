import json
import shutil
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from ibis_docs import discover as discover_module
from ibis_docs.discover import check_pdf, discover, load_seen, record_seen, sha256_of

DEMO = Path(__file__).resolve().parent.parent / "demo" / "invoices"
ONE_PAGE = DEMO / "001-inv-classic.pdf"
TWO_PAGES = DEMO / "006-inv-euro.pdf"


@pytest.fixture
def inbox(tmp_path) -> Path:
    folder = tmp_path / "inbox"
    folder.mkdir()
    return folder


def add(folder: Path, source: Path, name: str | None = None) -> Path:
    target = folder / (name or source.name)
    shutil.copy(source, target)
    return target


def encrypted_copy(source: Path, target: Path) -> Path:
    writer = PdfWriter(clone_from=PdfReader(source))
    writer.encrypt(user_password="secret", owner_password="owner")
    with target.open("wb") as f:
        writer.write(f)
    return target


def reasons(result) -> dict[str, str]:
    return {s.name: s.reason for s in result.skipped}


def test_finds_pdfs_with_pages_and_hash(inbox):
    add(inbox, ONE_PAGE)
    add(inbox, TWO_PAGES)
    result = discover(inbox, max_pages=20)
    assert [f.name for f in result.found] == ["001-inv-classic.pdf", "006-inv-euro.pdf"]
    assert [f.pages for f in result.found] == [1, 2]
    assert result.found[0].sha256 == sha256_of(ONE_PAGE)
    assert len(result.found[0].sha256) == 64
    assert result.skipped == []


def test_bad_files_are_skipped_with_a_reason_and_dont_stop_the_run(inbox):
    add(inbox, ONE_PAGE)
    (inbox / "empty.pdf").write_bytes(b"")
    (inbox / "notes.txt").write_text("call the supplier")
    (inbox / "renamed.pdf").write_bytes(b"\x89PNG\r\n\x1a\n not really a pdf")
    (inbox / "broken.pdf").write_bytes(b"%PDF-1.7\n this is not a real PDF body \n%%EOF")
    encrypted_copy(ONE_PAGE, inbox / "locked.pdf")

    result = discover(inbox, max_pages=20)

    assert [f.name for f in result.found] == ["001-inv-classic.pdf"]
    assert reasons(result) == {
        "broken.pdf": "unreadable PDF (PdfReadError)",
        "empty.pdf": "empty file",
        "locked.pdf": "password-protected (remove the password and add it again)",
        "notes.txt": "not a PDF",
        "renamed.pdf": "not a PDF",
    }


def test_pdf_is_recognised_by_content_not_extension(inbox):
    add(inbox, ONE_PAGE, "SCAN0001.PDF")
    add(inbox, ONE_PAGE, "download")  # no extension at all
    result = discover(inbox, max_pages=20)
    assert [f.name for f in result.found] == ["download", "SCAN0001.PDF"]  # case-insensitive


def test_too_many_pages(inbox):
    add(inbox, TWO_PAGES)
    result = discover(inbox, max_pages=1)
    assert reasons(result) == {"006-inv-euro.pdf": "too many pages (2; max_pages is 1)"}


def test_too_large(inbox, monkeypatch):
    add(inbox, ONE_PAGE)
    monkeypatch.setattr(discover_module, "MAX_PDF_BYTES", 100)
    result = discover(inbox, max_pages=20)
    assert reasons(result)["001-inv-classic.pdf"].startswith("too large")


def test_pdf_with_no_pages(inbox):
    path = inbox / "blank.pdf"
    with path.open("wb") as f:
        PdfWriter().write(f)
    assert reasons(discover(inbox, max_pages=20)) == {"blank.pdf": "PDF has no pages"}


def test_hidden_files_and_folders_are_ignored(inbox):
    add(inbox, ONE_PAGE)
    add(inbox, ONE_PAGE, ".hidden.pdf")
    (inbox / "._001-inv-classic.pdf").write_bytes(b"\x00\x05\x16\x07 resource fork")
    (inbox / ".DS_Store").write_bytes(b"\x00")
    for sub in ("processed", "needs-review", "other"):
        (inbox / sub).mkdir()
        add(inbox / sub, TWO_PAGES)
    result = discover(inbox, max_pages=20)
    assert [f.name for f in result.found] == ["001-inv-classic.pdf"]
    assert result.skipped == []


def test_identical_files_in_one_run_are_both_found_and_linked(inbox):
    add(inbox, ONE_PAGE)
    add(inbox, ONE_PAGE, "045-copy.pdf")
    result = discover(inbox, max_pages=20)
    first, copy = result.found
    assert first.name == "001-inv-classic.pdf" and first.duplicate_of is None
    assert copy.name == "045-copy.pdf" and copy.duplicate_of == "001-inv-classic.pdf"


def test_files_seen_in_an_earlier_run_are_skipped_even_when_renamed(inbox, tmp_path):
    output = tmp_path / "output"
    add(inbox, ONE_PAGE)
    record_seen(output, discover(inbox, max_pages=20).found)

    add(inbox, ONE_PAGE, "renamed-resend.pdf")
    add(inbox, TWO_PAGES)
    result = discover(inbox, max_pages=20, seen=load_seen(output))

    assert [f.name for f in result.found] == ["006-inv-euro.pdf"]
    assert set(reasons(result).values()) == {"already processed in an earlier run (same contents)"}


def test_seen_file_keeps_names_and_hashes_only(inbox, tmp_path):
    output = tmp_path / "output"
    assert load_seen(output) == set()
    add(inbox, ONE_PAGE)
    found = discover(inbox, max_pages=20).found
    record_seen(output, found)
    record_seen(output, found)  # recording twice keeps the first entry

    data = json.loads((output / ".ibis" / "seen.json").read_text())
    entry = data["files"][sha256_of(ONE_PAGE)]
    assert set(entry) == {"name", "first_seen"}
    assert entry["name"] == "001-inv-classic.pdf"
    assert load_seen(output) == {sha256_of(ONE_PAGE)}


def test_missing_input_folder(tmp_path):
    with pytest.raises(FileNotFoundError, match="input folder not found"):
        discover(tmp_path / "nope", max_pages=20)


def test_check_pdf_on_a_good_file():
    assert check_pdf(TWO_PAGES, max_pages=20) == (2, None)
