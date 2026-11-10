"""Quote documents: a customer PDF (reportlab, with an accept QR code) and an owner PNG card (Pillow).

Both are drawn from the stored quote JSON, so they always show exactly the engine's numbers. Only
built-in fonts are used, so rendering works on any machine without font files.
"""

from __future__ import annotations

import io
import textwrap
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from .money import fmt_money, fmt_qty
from .pricebook import PriceBook
from .store import QuoteRecord

VALID_DAYS = 30                  # default; the owner's "Quote valid for" setting wins


def _valid_days() -> int:
    from .settings import setting
    return setting("quote_valid_days")


def _qty_label(line: dict[str, Any]) -> str:
    qty = Decimal(line["qty"]).normalize()
    unit = line["unit"] if line["unit"] and line["unit"] != "each" else ""
    if qty == 1 and not unit:
        return ""
    return f"{format(qty, 'f')} {unit}".strip()


def _quote_date(rec: QuoteRecord) -> date:
    try:
        return datetime.fromisoformat(rec.updated_at).date()
    except Exception:
        return date.today()


def _draw_document(c, book: PriceBook, doc: dict, *, title: str, ref_lines: list[str], party_label: str,
                   party_lines: list[str]) -> float:
    """Shared quote/invoice layout: header, party, line table and totals. Returns the y below the totals."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import inch

    cur, biz = doc.get("currency", book.currency), book.business
    width, height = LETTER
    left, right = 0.75 * inch, width - 0.75 * inch
    y = height - 0.8 * inch
    c.setFont("Helvetica-Bold", 18)
    c.drawString(left, y, biz.get("name", ""))
    c.setFont("Helvetica", 9)
    c.drawString(left, y - 14, " · ".join(x for x in (biz.get("phone"), biz.get("email"), biz.get("address")) if x))
    if biz.get("license"):
        c.drawString(left, y - 26, f"License {biz['license']}")
    c.setFont("Helvetica-Bold", 22)
    c.drawRightString(right, y, title)
    c.setFont("Helvetica", 10)
    for i, line in enumerate(ref_lines):
        c.drawRightString(right, y - 16 - 13 * i, line)

    y -= 62
    c.setFont("Helvetica-Bold", 10)
    c.drawString(left, y, party_label)
    c.setFont("Helvetica", 10)
    shown = [x for x in party_lines if x]
    for i, text in enumerate(shown):
        c.drawString(left, y - 14 * (i + 1), text)

    y -= 28 + 14 * max(len(shown), 3)
    c.setFillGray(0.93)
    c.rect(left, y - 4, right - left, 18, stroke=0, fill=1)
    c.setFillGray(0)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(left + 6, y + 1, "Description")
    c.drawRightString(right - 110, y + 1, "Qty")
    c.drawRightString(right - 6, y + 1, "Amount")
    y -= 20
    c.setFont("Helvetica", 10)
    for line in doc["lines"]:
        if y < 2.6 * inch:
            c.showPage()
            y = height - inch
            c.setFont("Helvetica", 10)
        amount = "Price to confirm" if line["kind"] == "needs_price" else fmt_money(Decimal(line["amount"]), cur)
        wrapped = textwrap.wrap(line["description"], 60) or [""]
        c.drawString(left + 6, y, wrapped[0])
        c.drawRightString(right - 110, y, _qty_label(line))
        c.drawRightString(right - 6, y, amount)
        for extra in wrapped[1:]:
            y -= 13
            c.drawString(left + 6, y, extra)
        y -= 18

    if doc.get("multiplier_flag"):
        c.setFont("Helvetica-Oblique", 9)
        c.setFillGray(0.35)
        c.drawString(left + 6, y + 4, f"Labor is charged at the {doc['multiplier_flag'].replace('_', '-')} rate "
                                      f"(x {fmt_qty(Decimal(doc['multiplier']))}).")
        c.setFillGray(0)
        y -= 14
    c.setLineWidth(0.5)
    c.line(right - 230, y + 8, right, y + 8)
    rows = [("Subtotal", fmt_money(Decimal(doc["subtotal"]), cur)),
            (f"{doc['tax_name']} ({fmt_qty(Decimal(doc['tax_rate']) * 100)}% on materials "
             f"{fmt_money(Decimal(doc['taxable']), cur)})" if doc.get("taxable") else doc["tax_name"],
             fmt_money(Decimal(doc["tax"]), cur))]
    for label, value in rows:
        y -= 6
        c.setFont("Helvetica", 10)
        c.drawRightString(right - 110, y, label)
        c.drawRightString(right - 6, y, value)
        y -= 12
    y -= 6
    c.setFont("Helvetica-Bold", 13)
    c.drawRightString(right - 110, y, "Total")
    c.drawRightString(right - 6, y, fmt_money(Decimal(doc["total"]), cur))
    return y


def render_pdf(book: PriceBook, rec: QuoteRecord, accept_url: str) -> bytes:
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode import qr
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER, pageCompression=1)
    c.setTitle(f"Quote {rec.number} from {book.business.get('name', '')}")
    c.setAuthor(book.business.get("name", ""))
    issued = _quote_date(rec)
    _draw_document(c, book, rec.quote, title="QUOTE",
                   ref_lines=[f"{rec.number} · version {rec.version}",
                              f"Issued {issued:%b %d, %Y} · valid until {issued + timedelta(days=_valid_days()):%b %d, %Y}"],
                   party_label="Prepared for",
                   party_lines=[rec.customer.get("name", ""), rec.site, rec.zip and f"ZIP {rec.zip}"])
    left = 0.75 * inch
    box = 1.3 * inch
    widget = qr.QrCodeWidget(accept_url)
    x0, y0, x1, y1 = widget.getBounds()
    drawing = Drawing(box, box, transform=[box / (x1 - x0), 0, 0, box / (y1 - y0), 0, 0])
    drawing.add(widget)
    renderPDF.draw(drawing, c, left, 0.9 * inch)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(left + box + 14, 0.9 * inch + box - 14, "Accept this quote")
    c.setFont("Helvetica", 9)
    c.drawString(left + box + 14, 0.9 * inch + box - 30, "Scan the code or open:")
    c.setFillColorRGB(0.1, 0.3, 0.7)
    for i, chunk in enumerate(textwrap.wrap(accept_url, 70)):
        c.drawString(left + box + 14, 0.9 * inch + box - 44 - 11 * i, chunk)
    c.setFillGray(0.35)
    c.drawString(left + box + 14, 0.9 * inch + 6,
                 f"Fixed price for the work listed. Valid {_valid_days()} days. Changes to the scope are quoted separately.")
    c.showPage()
    c.save()
    return buf.getvalue()


def render_invoice_pdf(book: PriceBook, invoice: dict) -> bytes:
    """Invoice PDF from a stored invoice (see invoices.py). Payment terms come from the price book."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER, pageCompression=1)
    c.setTitle(f"Invoice {invoice['number']} from {book.business.get('name', '')}")
    c.setAuthor(book.business.get("name", ""))
    issued = date.fromisoformat(invoice["issued"])
    due = date.fromisoformat(invoice["due"])
    _draw_document(c, book, invoice["doc"], title="INVOICE",
                   ref_lines=[f"{invoice['number']} · for quote {invoice['quote_number']}",
                              f"Issued {issued:%b %d, %Y} · due {due:%b %d, %Y}"],
                   party_label="Bill to",
                   party_lines=[invoice["customer"].get("name", ""), invoice.get("site", ""),
                                invoice.get("zip") and f"ZIP {invoice['zip']}", invoice["customer"].get("email", "")])
    left = 0.75 * inch
    c.setFont("Helvetica-Bold", 11)
    c.drawString(left, 1.6 * inch, f"Amount due: {fmt_money(Decimal(invoice['doc']['total']), invoice['doc'].get('currency', book.currency))}")
    c.setFont("Helvetica", 9)
    c.setFillGray(0.35)
    terms = book.business.get("payment_terms") or f"Payment due by {due:%b %d, %Y}. Thank you for your business."
    for i, chunk in enumerate(textwrap.wrap(terms, 100)):
        c.drawString(left, 1.35 * inch - 12 * i, chunk)
    c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------- owner card

INK, MUTED, RULE, BG, ACCENT, WARN = (24, 28, 34), (110, 118, 128), (226, 229, 233), (255, 255, 255), (22, 101, 52), (180, 83, 9)
STATUS_COLORS = {"draft": (100, 116, 139), "sent": (37, 99, 235), "viewed": (124, 58, 237), "accepted": (22, 163, 74),
                 "scheduled": (13, 148, 136), "declined": (220, 38, 38)}


def render_card_png(book: PriceBook, rec: QuoteRecord, *, width: int = 1080) -> bytes:
    """Owner-facing card: every line with the rule that priced it, totals, status. For Telegram and Desktop."""
    from PIL import Image, ImageDraw, ImageFont

    def font(size: int, bold: bool = False):
        # Bitstream Vera ships with reportlab (a Docket dependency) and, unlike Pillow's built-in font, has
        # ×, dashes, € and £, so quantities and non-US prices never render as empty boxes.
        import os
        import reportlab
        path = os.path.join(os.path.dirname(reportlab.__file__), "fonts", "VeraBd.ttf" if bold else "Vera.ttf")
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            try:
                return ImageFont.load_default(size=size)
            except TypeError:  # very old Pillow without FreeType default
                return ImageFont.load_default()

    q, cur = rec.quote, rec.quote.get("currency", book.currency)
    f_title, f_big, f_body, f_small = font(42, bold=True), font(38, bold=True), font(28), font(22)
    pad, inner = 56, width - 112
    rows: list[tuple[str, Any]] = []
    for line in q["lines"]:
        rows.append(("line", line))
    measure = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    def wrap(text: str, fnt, max_w: int) -> list[str]:
        words, out, cur_line = text.split(), [], ""
        for w in words:
            trial = f"{cur_line} {w}".strip()
            if measure.textlength(trial, font=fnt) <= max_w:
                cur_line = trial
            else:
                if cur_line:
                    out.append(cur_line)
                cur_line = w
        if cur_line:
            out.append(cur_line)
        return out or [""]

    blocks = []
    for _, line in rows:
        desc = line["description"] + (f"  × {_qty_label(line)}" if _qty_label(line) else "")
        rule = (line["rule"] if line["kind"] != "needs_price" or line.get("code") == "TRAVEL"
                else "Not in your price book. Add a price before sending.")
        blocks.append((wrap(desc, f_body, inner - 230), wrap(rule, f_small, inner), line))
    height = pad + 60 + 50 + 30 + sum(len(d) * 40 + len(r) * 30 + 34 for d, r, _ in blocks) + 210 + pad
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    _draw_text = d.text
    d.text = lambda xy, text, *a, **k: _draw_text(xy, str(text).replace("→", "->").replace("✓", "").replace("\u2713", ""), *a, **k)  # noqa: E731

    y = pad
    d.text((pad, y), f"{rec.number}  v{rec.version}", font=f_title, fill=INK)
    status = rec.status.replace("_", " ")
    sw = d.textlength(status.upper(), font=f_small) + 36
    d.rounded_rectangle((width - pad - sw, y + 6, width - pad, y + 50), radius=22,
                        fill=STATUS_COLORS.get(rec.status, MUTED))
    d.text((width - pad - sw + 18, y + 15), status.upper(), font=f_small, fill=BG)
    y += 62
    who = " · ".join(x for x in (rec.customer.get("name"), rec.site, rec.zip) if x)
    d.text((pad, y), who, font=f_body, fill=MUTED)
    y += 56
    d.line((pad, y, width - pad, y), fill=RULE, width=2)
    y += 26
    for desc_lines, rule_lines, line in blocks:
        needs = line["kind"] == "needs_price"
        amount = "NEEDS PRICE" if needs else fmt_money(Decimal(line["amount"]), cur)
        d.text((pad, y), desc_lines[0], font=f_body, fill=WARN if needs else INK)
        d.text((width - pad - d.textlength(amount, font=f_body), y), amount, font=f_body, fill=WARN if needs else INK)
        for extra in desc_lines[1:]:
            y += 40
            d.text((pad, y), extra, font=f_body, fill=INK)
        y += 42
        for r in rule_lines:
            d.text((pad + 4, y), r, font=f_small, fill=MUTED)
            y += 30
        y += 34
    d.line((pad, y - 14, width - pad, y - 14), fill=RULE, width=2)
    y += 6
    for label, value in (("Subtotal", fmt_money(Decimal(q["subtotal"]), cur)),
                         (f"{q['tax_name']} {fmt_qty(Decimal(q['tax_rate']) * 100)}%", fmt_money(Decimal(q["tax"]), cur))):
        d.text((width - pad - 420, y), label, font=f_body, fill=MUTED)
        d.text((width - pad - d.textlength(value, font=f_body), y), value, font=f_body, fill=INK)
        y += 44
    total = fmt_money(Decimal(q["total"]), cur)
    d.text((width - pad - 420, y + 6), "Total", font=f_big, fill=INK)
    d.text((width - pad - d.textlength(total, font=f_big), y + 6), total, font=f_big, fill=ACCENT)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
