# Architecture

How Ibis is put together. Updated at the end of every build session; parts not built yet are
marked **planned**.

| Session | Scope | Status |
|---|---|---|
| 1 | Skeleton, config (E1), models, CLI shell (E11), CI, demo invoices + answer key | ✅ done |
| 2 | Discovery (E2), extraction (E3), recorded fixtures | planned |
| 3 | Validation (E5), duplicates (E6), status and reasons (E7), escalation (E4) | planned |
| 4 | Exports (E8), file moves (E9), run report and cost (E12) | planned |
| 5 | Streamlit review page (E10), README, demo script | planned |

## The pipeline

Claude reads each document; Python checks the numbers. Only documents that pass every check
reach the sheet as `ok`.

```mermaid
flowchart LR
    cfg[config.yaml] --> load["Load config<br/>config.py ✅"]
    load --> find["Find PDFs, hash, skip seen<br/>discover.py (planned)"]
    find --> extract["Extract with Haiku 5.5<br/>extract.py (planned)"]
    extract --> check{"Validate<br/>validate.py (planned)"}
    check -- fails --> retry["Retry once on Sonnet 5.5<br/>extract.py (planned)"]
    retry --> check2{"Validate again"}
    check -- passes --> ok["status ok"]
    check2 -- passes --> ok
    check2 -- fails --> review["status review + reasons"]
    ok --> export["CSV / Excel / Sheets<br/>export.py (planned)"]
    review --> export
    export --> move["Move to processed/ or needs-review/<br/>files.py (planned)"]
    move --> report["Run report<br/>report.py (planned)"]
```

## Modules

```mermaid
flowchart TD
    cli["cli.py ✅<br/>Typer: check-config; run/show/cost stubs"] --> config["config.py ✅<br/>Config, load_config()"]
    config --> money["money.py ✅<br/>parse_amount(), normalise_currency(), ISO_4217"]
    models["models.py ✅<br/>LineItem, ExtractedInvoice, Invoice, Status"] --> money
    gen["demo/generate.py ✅<br/>fake PDFs + answers.json"] --> money
    tests["tests/test_demo_set.py ✅"] --> gen
    tests --> models
```

## Data model

`ExtractedInvoice` holds only what is printed, so it doubles as the schema Claude fills.
`Invoice` adds what the pipeline decides. Every `Invoice` starts as `review`; only validation
can make it `ok`.

```mermaid
classDiagram
    class ExtractedInvoice {
        doc_type: invoice|packing_slip|credit_note|other
        supplier_name, supplier_tax_id
        invoice_number, po_number
        invoice_date, due_date
        currency: ISO 4217
        subtotal, tax, discount, shipping, total: Decimal?
        line_items: LineItem[]
    }
    class LineItem {
        description, sku?
        quantity, unit_price, line_total: Decimal?
        tax_rate: Decimal? (percent)
    }
    class Invoice {
        source_file: SourceFile (name, sha256)
        status: ok|review|failed
        reasons: str[]
        model: str
    }
    ExtractedInvoice <|-- Invoice
    ExtractedInvoice *-- LineItem
```

## Currencies

Pluggable at three levels:

- **Config:** `validation.allowed_currencies` takes any ISO 4217 code; anything else on an
  invoice goes to review. `validation.currency_symbols` adds or remaps symbols per client.
- **Parsing:** `money.DEFAULT_SYMBOLS` (longest match first) plus the client overrides; an ISO
  code printed on the document beats a symbol.
- **Demo data:** `demo/generate.py --currencies` takes any ISO code; codes without a built-in
  style print as `XXX 1,234.00` with a generic 10% tax.

## Decisions

- Package `ibis_docs`, command `ibis` (the `ibis` import name belongs to ibis-framework).
- Demo-only dependencies (reportlab, pypdfium2, Pillow) live in a `demo` dependency group.
- Credit notes print negative quantities, so `qty × price = line total` holds for every
  document type without special cases.
- reportlab runs in invariant mode, so regenerating the demo set changes no bytes unless the
  generator changes; a test checks the committed PDFs match.
