"""The review page (E10): the PDF on the left, its fields on the right; correct, approve, discard.

Start it with `uv run ibis review`, which binds it to this computer only (localhost) and turns
off Streamlit's usage statistics. The config file comes from IBIS_CONFIG (set by `ibis review`).
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path

import pypdfium2 as pdfium
import streamlit as st
from dotenv import load_dotenv

from ibis_docs import results
from ibis_docs.config import Config, ConfigError, load_config
from ibis_docs.models import DocType, Status
from ibis_docs.review import (
    LINE_FIELDS,
    EditError,
    apply_edits,
    approve,
    as_text,
    discard,
    form_values,
    line_values,
    recheck,
)
from ibis_docs.run import write_exports

CONFIG_ENV = "IBIS_CONFIG"
AMBER, GREEN = "#B7791F", "#23735F"
MARK = {Status.OK: "✓", Status.REVIEW: "!", Status.FAILED: "✗"}


@st.cache_data(show_spinner=False)
def page_images(path: str, modified: float) -> list:
    """Each page as an image, rendered locally (`modified` refreshes the cache if it changes)."""
    pdf = pdfium.PdfDocument(path)
    try:
        return [page.render(scale=1.6).to_pil() for page in pdf]
    finally:
        pdf.close()


def label(result: results.Result) -> str:
    return f"{MARK[result.invoice.status]} {result.name}"


def show_pdf(result: results.Result) -> None:
    path = Path(result.pdf_path or "")
    if not path.is_file():
        st.warning(
            as_text(f"The PDF isn't where it was left ({result.pdf_path}). Moved or deleted?")
        )
        return
    for i, image in enumerate(page_images(str(path), path.stat().st_mtime), start=1):
        st.image(image, caption=f"page {i}", width="stretch")


def edit_form(result: results.Result, key: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    values = form_values(result.invoice)
    header: dict[str, str] = {}
    types = [t.value for t in DocType]
    header["doc_type"] = st.selectbox(
        "Type", types, index=types.index(values["doc_type"]), key=f"{key}-doc_type"
    )
    left, right = st.columns(2)
    fields = [
        ("supplier_name", "Supplier", left),
        ("supplier_tax_id", "Supplier tax ID", right),
        ("invoice_number", "Invoice number", left),
        ("po_number", "PO number", right),
        ("invoice_date", "Invoice date (YYYY-MM-DD)", left),
        ("due_date", "Due date (YYYY-MM-DD)", right),
        ("currency", "Currency (e.g. GBP)", left),
        ("subtotal", "Subtotal", right),
        ("tax", "Tax", left),
        ("discount", "Discount (amount taken off)", right),
        ("shipping", "Shipping", left),
        ("total", "Total", right),
    ]
    for name, title, column in fields:
        header[name] = column.text_input(title, values[name], key=f"{key}-{name}")

    st.markdown("**Line items**")
    edited = st.data_editor(
        line_values(result.invoice) or [dict.fromkeys(LINE_FIELDS, "")],
        num_rows="dynamic",
        width="stretch",
        key=f"{key}-lines",
        column_config={f: st.column_config.TextColumn(f.replace("_", " ")) for f in LINE_FIELDS},
    )
    lines = edited if isinstance(edited, list) else edited.to_dict("records")
    return header, [
        {f: "" if row.get(f) is None else str(row.get(f)) for f in LINE_FIELDS} for row in lines
    ]


def main() -> None:
    st.set_page_config(page_title="ibis · review", page_icon="🪶", layout="wide")
    load_dotenv()
    try:
        config: Config = load_config(os.environ.get(CONFIG_ENV, "config.yaml"))
    except ConfigError as exc:
        st.error(as_text(str(exc)))
        return

    everything = results.load_all(config.output_path)
    live = [r for r in everything if not r.discarded]
    flagged = [r for r in live if r.invoice.status != Status.OK]

    with st.sidebar:
        st.markdown(f"<h2 style='color:{GREEN};margin-bottom:0'>ibis</h2>", unsafe_allow_html=True)
        st.caption("Every invoice, read and checked.")
        st.write(f"{len(live)} documents · **{len(flagged)} need a look**")
        show = st.radio("Show", ["Needs review", "All"], horizontal=True)
        choices = flagged if show == "Needs review" else live
        if not choices:
            st.success("Nothing needs a look." if show == "Needs review" else "No documents yet.")
            return
        picked = st.radio("Documents", choices, format_func=label, label_visibility="collapsed")

    result: results.Result = picked
    key = result.filename()
    left, right = st.columns([1.1, 1], gap="large")
    with left:
        show_pdf(result)
    with right:
        st.subheader(result.name)
        status = result.invoice.status
        colour = GREEN if status == Status.OK else AMBER
        st.markdown(
            f"<span style='color:{colour};font-weight:600'>{MARK[status]} {status.value}</span>"
            f" · read by {result.invoice.model or 'no model'}",
            unsafe_allow_html=True,
        )
        for reason in result.invoice.reasons:
            st.warning(as_text(reason.message))
        if review := result.review:
            st.info(as_text(f"{review.action} {review.at:%Y-%m-%d %H:%M}. {review.note}"))

        header, lines = edit_form(result, key)
        try:
            edited, changes = apply_edits(result.invoice, header, lines, config)
        except EditError as exc:
            for problem in exc.problems:
                st.error(as_text(problem))
            return

        open_reasons = recheck(edited, result, everything, config, date.today())
        st.markdown("**Checks with your edits**")
        if open_reasons:
            for reason in open_reasons:
                st.markdown(
                    f"<span style='color:{AMBER}'>! {as_text(reason.message, in_html=True)}</span>",
                    unsafe_allow_html=True,
                )
        else:
            st.markdown(
                f"<span style='color:{GREEN}'>✓ Everything checks out.</span>",
                unsafe_allow_html=True,
            )
        if changes:
            st.caption(
                as_text(
                    "Changed: "
                    + "; ".join(f"{c.field}: {c.before or '—'} → {c.after or '—'}" for c in changes)
                )
            )

        note = st.text_input(
            "Note (needed to approve with checks still failing)", key=f"{key}-note"
        )
        approve_col, discard_col = st.columns(2)
        if approve_col.button("Approve", type="primary", key=f"{key}-approve"):
            try:
                approve(
                    config,
                    result,
                    edited,
                    changes,
                    open_reasons=open_reasons,
                    note=note,
                    now=datetime.now(),
                )
            except EditError as exc:
                st.error(as_text(exc.problems[0]))
                return
            finish(config, f"Approved {result.name}.")
        confirm = discard_col.checkbox("Leave this out of the sheets", key=f"{key}-confirm")
        if discard_col.button("Discard", disabled=not confirm, key=f"{key}-discard"):
            discard(config, result, note=note, now=datetime.now())
            finish(config, f"Discarded {result.name}.")


def finish(config: Config, message: str) -> None:
    kept = write_exports(config)
    st.session_state["flash"] = (message, kept.outputs, kept.warnings)
    st.rerun()


def flash() -> None:
    if "flash" in st.session_state:
        message, outputs, warnings = st.session_state.pop("flash")
        st.toast(as_text(f"{message} Sheets updated: {', '.join(outputs)}"))
        for warning in warnings:
            st.warning(as_text(warning))


def run() -> None:
    flash()
    main()


if __name__ == "__main__":
    run()
