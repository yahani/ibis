import csv
from datetime import date, datetime
from decimal import Decimal

import pytest
from openpyxl import load_workbook

from ibis_docs import export
from ibis_docs.export import (
    _runs,
    build_sheets,
    format_requests,
    sheets_payload,
    spreadsheet_key,
    write_csv,
    write_excel,
    write_google_sheet,
)
from ibis_docs.models import Invoice, Reason, Status
from ibis_docs.results import Result

WHEN = datetime(2026, 10, 23, 9, 30)


def result(name: str, status: Status = Status.OK, **fields) -> Result:
    invoice = Invoice.model_validate(
        {
            "source_file": {"name": name, "sha256": "a" * 64},
            "doc_type": "invoice",
            "supplier_name": "Harbourline Packaging Ltd",
            "invoice_number": "INV-1",
            "invoice_date": "2026-08-18",
            "currency": "GBP",
            "subtotal": "1481.00",
            "tax": "296.20",
            "total": "1777.20",
            "model": "claude-haiku-5-5",
            "line_items": [
                {
                    "description": "Box",
                    "quantity": "20",
                    "unit_price": "1.70",
                    "line_total": "34.00",
                },
                {
                    "description": "Void fill",
                    "quantity": "50",
                    "unit_price": "28.945",
                    "line_total": "1447.25",
                },
            ],
            **fields,
        }
    )
    invoice.status = status
    if status != Status.OK:
        invoice.reasons = [Reason(code="total_mismatch", message="the total doesn't add up")]
    return Result(invoice=invoice, processed_at=WHEN, run_id="20261023-093000")


RESULTS = [
    result("001.pdf"),
    result("002.pdf", Status.REVIEW, invoice_number="INV-2", due_date="2026-09-17"),
]


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


def test_three_sheets():
    invoices, lines, review = build_sheets(RESULTS)
    assert [s.name for s in (invoices, lines, review)] == ["Invoices", "Line items", "Needs review"]
    assert len(invoices.rows) == 2 and len(lines.rows) == 4 and len(review.rows) == 1
    assert invoices.review_rows == {1} and lines.review_rows == {2, 3} and review.review_rows == {0}


def test_csv_keeps_amounts_exact(tmp_path):
    invoices, lines, _ = build_sheets(RESULTS)
    write_csv(tmp_path / "invoices.csv", invoices)
    write_csv(tmp_path / "lines.csv", lines)
    head, first, second = read_csv(tmp_path / "invoices.csv")
    row = dict(zip(head, first, strict=True))
    assert row["Total"] == "1777.20" and row["Invoice date"] == "2026-08-18"
    assert row["Due date"] == "" and row["Status"] == "ok" and row["Reasons"] == ""
    assert row["Processed"] == "2026-10-23T09:30:00"
    review = dict(zip(head, second, strict=True))
    assert review["Reasons"] == "the total doesn't add up"
    assert review["Reason codes"] == "total_mismatch"
    line = dict(zip(*read_csv(tmp_path / "lines.csv")[:3:2], strict=True))
    assert line["Unit price"] == "28.945" and line["Line"] == "2"
    # Excel needs the byte-order mark to show £ and € correctly.
    assert (tmp_path / "invoices.csv").read_bytes().startswith(b"\xef\xbb\xbf")


def test_excel_has_real_numbers_and_dates(tmp_path):
    write_excel(tmp_path / "ibis.xlsx", build_sheets(RESULTS))
    book = load_workbook(tmp_path / "ibis.xlsx")
    assert book.sheetnames == ["Invoices", "Line items", "Needs review"]
    ws = book["Invoices"]
    headers = [c.value for c in ws[1]]
    total = ws.cell(row=2, column=headers.index("Total") + 1)
    assert total.value == Decimal("1777.20") or total.value == pytest.approx(1777.20)
    assert total.number_format == "#,##0.00##"
    issued = ws.cell(row=2, column=headers.index("Invoice date") + 1)
    assert issued.value.date() == date(2026, 8, 18)
    assert ws.freeze_panes == "A2"
    assert ws.cell(row=1, column=1).fill.fgColor.rgb.endswith("23735F")
    # The review row is amber; the ok row isn't.
    assert ws.cell(row=3, column=1).fill.fgColor.rgb.endswith("F3E6CF")
    assert ws.cell(row=2, column=1).fill.fgColor.rgb != ws.cell(row=3, column=1).fill.fgColor.rgb


def test_all_outputs_carry_the_same_data(tmp_path):
    """The spec's check: CSV, Excel and the Sheets payload match."""
    sheets = build_sheets(RESULTS)
    payload = sheets_payload(sheets)
    write_excel(tmp_path / "ibis.xlsx", sheets)
    book = load_workbook(tmp_path / "ibis.xlsx")
    for sheet in sheets:
        write_csv(tmp_path / "s.csv", sheet)
        from_csv = read_csv(tmp_path / "s.csv")
        from_excel = [[c.value for c in row] for row in book[sheet.name].iter_rows()]
        from_sheets = payload[sheet.name]
        assert len(from_csv) == len(from_excel) == len(from_sheets) == len(sheet.rows) + 1
        for csv_row, xl_row, gs_row in zip(from_csv, from_excel, from_sheets, strict=True):
            for text, xl, gs in zip(csv_row, xl_row, gs_row, strict=True):
                assert _same(text, xl) and _same(text, gs), (sheet.name, text, xl, gs)


def _same(text: str, value) -> bool:
    if value is None or value == "":
        return text == ""
    if isinstance(value, (int, float, Decimal)):
        return Decimal(text) == Decimal(str(value))
    if isinstance(value, datetime):
        return text.startswith(value.date().isoformat())
    return str(value).replace(" ", "T") == text or str(value) == text


@pytest.mark.parametrize("evil", ['=HYPERLINK("http://x")', "+1+1", "-2+3", "@SUM(A1)"])
def test_invoice_text_never_runs_as_a_formula(tmp_path, evil):
    r = result("001.pdf", supplier_name=evil)
    sheets = build_sheets([r])
    write_csv(tmp_path / "i.csv", sheets[0])
    write_excel(tmp_path / "i.xlsx", sheets)
    head, row = read_csv(tmp_path / "i.csv")
    assert row[head.index("Supplier")] == "'" + evil
    cell = load_workbook(tmp_path / "i.xlsx")["Invoices"].cell(
        row=2, column=head.index("Supplier") + 1
    )
    assert cell.data_type == "s" and cell.value == evil
    assert sheets_payload(sheets)["Invoices"][1][head.index("Supplier")] == "'" + evil


def test_negative_amounts_are_numbers_not_text(tmp_path):
    r = result("credit.pdf", doc_type="credit_note", subtotal="-1481.00", total="-1777.20")
    payload = sheets_payload(build_sheets([r]))["Invoices"]
    assert payload[1][payload[0].index("Total")] == -1777.20


def test_a_failed_write_leaves_the_old_file(tmp_path, monkeypatch):
    target = tmp_path / "invoices.csv"
    target.write_text("old")
    sheet = build_sheets(RESULTS)[0]
    monkeypatch.setattr(export.csv, "writer", lambda f: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        write_csv(target, sheet)
    assert target.read_text() == "old"
    assert list(tmp_path.glob(".invoices.csv.*")) == []  # no temporary file left behind


def test_spreadsheet_key():
    url = "https://docs.google.com/spreadsheets/d/1AbC_d-9/edit#gid=0"
    assert spreadsheet_key(url) == "1AbC_d-9"
    assert spreadsheet_key(" 1AbC_d-9 ") == "1AbC_d-9"


class FakeWorksheet:
    def __init__(self, title, sheet_id):
        self.title, self.id, self.calls = title, sheet_id, []

    def clear(self):
        self.calls.append("clear")

    def resize(self, rows, cols):
        self.calls.append(("resize", rows, cols))

    def update(self, rows, start, value_input_option):
        self.calls.append(("update", start, value_input_option, rows))


class FakeBook:
    url = "https://docs.google.com/spreadsheets/d/KEY"

    def __init__(self):
        self.sheets = {
            "Sheet1": FakeWorksheet("Sheet1", 0),
            "Invoices": FakeWorksheet("Invoices", 1),
        }
        self.batches = []

    def worksheets(self):
        return list(self.sheets.values())

    def add_worksheet(self, title, rows, cols):
        self.sheets[title] = FakeWorksheet(title, len(self.sheets) + 10)
        return self.sheets[title]

    def batch_update(self, body):
        self.batches.append(body)


def test_google_sheet_writer(monkeypatch, tmp_path):
    import gspread

    book = FakeBook()
    opened = {}

    class Client:
        def open_by_key(self, key):
            opened["key"] = key
            return book

    def service_account(filename):
        opened["creds"] = filename
        return Client()

    monkeypatch.setattr(gspread, "service_account", service_account)
    sheets = build_sheets(RESULTS)
    payload = sheets_payload(sheets)
    url = write_google_sheet(
        "https://docs.google.com/spreadsheets/d/KEY/edit", sheets, tmp_path / "sa.json"
    )
    assert url == FakeBook.url
    assert opened == {"key": "KEY", "creds": str(tmp_path / "sa.json")}
    assert set(book.sheets) == {"Sheet1", "Invoices", "Line items", "Needs review"}
    assert book.sheets["Sheet1"].calls == []  # not Ibis's, left alone
    for name in payload:
        ws = book.sheets[name]
        rows = payload[name]
        assert ws.calls == [
            "clear",
            ("resize", max(len(rows), 2), len(rows[0])),
            ("update", "A1", "USER_ENTERED", rows),
        ]
    # One batch of formatting for the whole spreadsheet.
    (batch,) = book.batches
    sheet_ids = {
        r["repeatCell"]["range"]["sheetId"] for r in batch["requests"] if "repeatCell" in r
    }
    assert sheet_ids == {book.sheets[n].id for n in payload}


def test_sheet_formatting_matches_the_excel_file():
    invoices = build_sheets(RESULTS)[0]
    requests = format_requests(7, invoices)
    cells = [r["repeatCell"] for r in requests if "repeatCell" in r]

    # Old formatting is cleared first.
    assert cells[0] == {
        "range": {"sheetId": 7},
        "cell": {"userEnteredFormat": {}},
        "fields": "userEnteredFormat",
    }
    header = cells[1]
    assert header["range"] == {"sheetId": 7, "startRowIndex": 0, "endRowIndex": 1}
    fmt = header["cell"]["userEnteredFormat"]
    assert fmt["textFormat"]["bold"] is True
    assert round(fmt["backgroundColor"]["red"] * 255) == 0x23

    by_column = {
        c["range"]["startColumnIndex"]: c["cell"]["userEnteredFormat"]["numberFormat"]
        for c in cells
        if "numberFormat" in c["cell"]["userEnteredFormat"]
    }
    headers = invoices.headers
    assert by_column[headers.index("Total")] == {"type": "NUMBER", "pattern": "#,##0.00##"}
    assert by_column[headers.index("Invoice date")] == {"type": "DATE", "pattern": "yyyy-mm-dd"}
    assert by_column[headers.index("Processed")]["type"] == "DATE_TIME"
    assert headers.index("Supplier") not in by_column  # text stays text

    # The review row (the second document, sheet row 3) is amber.
    amber = [c for c in cells if c["fields"] == "userEnteredFormat.backgroundColor"]
    assert [(c["range"]["startRowIndex"], c["range"]["endRowIndex"]) for c in amber] == [(2, 3)]

    frozen = next(r for r in requests if "updateSheetProperties" in r)
    assert frozen["updateSheetProperties"]["properties"]["gridProperties"] == {"frozenRowCount": 1}
    assert "autoResizeDimensions" in requests[-1]


def test_an_empty_sheet_gets_only_its_header_formatted():
    review = build_sheets([result("001.pdf")])[2]
    assert review.rows == []
    requests = format_requests(3, review)
    assert not any("numberFormat" in str(r) for r in requests)


def test_consecutive_review_rows_are_painted_together():
    assert _runs({1, 2, 3, 7}) == [(1, 4), (7, 8)]
    assert _runs(set()) == []
