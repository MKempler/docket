"""Local outbox: what would be emailed to the customer, written to disk. SMTP is a later opt-in."""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path


def write_outbox(root: Path, *, number: str, version: int, business: dict, customer: dict, subject: str,
                 body: str, attachments: dict[str, bytes] | None = None) -> Path:
    folder = Path(root) / f"{number}-v{version}"
    folder.mkdir(parents=True, exist_ok=True)
    msg = EmailMessage()
    msg["From"] = f"{business.get('name', 'Docket')} <{business.get('email') or 'quotes@docket.invalid'}>"
    msg["To"] = f"{customer.get('name', '')} <{customer.get('email') or 'customer@docket.invalid'}>"
    msg["Subject"] = subject
    msg.set_content(body)
    for name, data in (attachments or {}).items():
        maintype, subtype = ("application", "pdf") if name.endswith(".pdf") else ("application", "octet-stream")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
        (folder / name).write_bytes(data)
    (folder / "message.eml").write_bytes(bytes(msg))
    (folder / "message.txt").write_text(body, encoding="utf-8")
    return folder
