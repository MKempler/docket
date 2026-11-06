"""Read-only owner reports used by the Automation Blueprints. They never send anything to customers."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from .money import fmt_money
from .store import Store

NOTHING = "Nothing to report."


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def followups(store: Store, *, stale_days: int = 5, accept_url=None, now: datetime | None = None) -> dict:
    """Quotes sent or viewed but not answered for stale_days+, oldest first, each with a ready-to-send nudge."""
    now = now or datetime.now(timezone.utc)
    rows = []
    for rec in store.list({"sent", "viewed"}):
        waited = (now - _parse(store.last_status_change(rec.id))).days
        if waited >= stale_days:
            rows.append((waited, rec))
    rows.sort(key=lambda r: -r[0])
    if not rows:
        return {"count": 0, "text": f"{NOTHING} No quotes have waited {stale_days}+ days."}
    total = sum((Decimal(r.total) for _, r in rows), Decimal(0))
    lines = [f"{len(rows)} quote{'s' if len(rows) != 1 else ''} waiting {stale_days}+ days ({fmt_money(total)} total):"]
    for waited, rec in rows:
        first = rec.customer["name"].split()[0]
        seen = "opened it" if rec.status == "viewed" else "not opened yet"
        lines.append(f"• {rec.number} {rec.customer['name']} · {fmt_money(Decimal(rec.total))} · {waited} days · {seen}")
        link = f" {accept_url(rec)}" if accept_url else ""
        lines.append(f"  Suggested nudge: \"Hi {first}, just checking you got our quote {rec.number}. "
                     f"Happy to answer any questions or adjust it.{link}\"")
    lines.append("Nothing was sent. Reply to the customer yourself, or revise with /quote revise <number> <change>.")
    return {"count": len(rows), "quotes": [r.number for _, r in rows], "text": "\n".join(lines)}


def run_sheet(store: Store, *, day: date | None = None) -> dict:
    """Jobs scheduled for a day (default tomorrow) with address and materials to load on the truck."""
    day = day or (datetime.now(timezone.utc).date() + timedelta(days=1))
    jobs = [r for r in store.list({"scheduled"}) if r.scheduled_for == day.isoformat()]
    if not jobs:
        return {"count": 0, "text": f"{NOTHING} No jobs scheduled for {day:%a %b %d}."}
    lines = [f"Run sheet for {day:%a %b %d}: {len(jobs)} job{'s' if len(jobs) != 1 else ''}"]
    load: dict[str, Decimal] = {}
    for rec in sorted(jobs, key=lambda r: r.id):
        where = " · ".join(x for x in (rec.site, rec.zip) if x)
        lines.append(f"• {rec.number} {rec.customer['name']} · {where}")
        for line in rec.quote["lines"]:
            if line["kind"] == "assembly":
                lines.append(f"    {line['description']}")
            for comp in line.get("components", []):
                key = f"{comp['name']} ({comp['unit']})"
                load[key] = load.get(key, Decimal(0)) + Decimal(comp["qty"])
            if line["kind"] == "material":
                key = f"{line['description']} ({line['unit']})"
                load[key] = load.get(key, Decimal(0)) + Decimal(line["qty"])
    if load:
        lines.append("Load on the truck:")
        lines += [f"  {format(q.normalize(), 'f')} × {name}" for name, q in sorted(load.items())]
    return {"count": len(jobs), "text": "\n".join(lines)}


def pipeline(store: Store, *, now: datetime | None = None, days: int = 7) -> dict:
    """Open quote value, what moved in the last `days`, and win rate."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    recs = store.list()
    open_ = [r for r in recs if r.status in {"draft", "sent", "viewed"}]
    won = [r for r in recs if r.status in {"accepted", "scheduled", "in_progress", "done", "invoiced", "paid"}]
    lost = [r for r in recs if r.status in {"declined", "expired"}]
    recent = {e["quote_id"]: e for e in store.events(limit=1000) if _parse(e["at"]) >= since}
    won_recent = [r for r in won if any(e["quote_id"] == r.id and e["to_status"] == "accepted"
                                        for e in store.events(r.id) if _parse(e["at"]) >= since)]

    def total(rs):
        return fmt_money(sum((Decimal(r.total) for r in rs), Decimal(0)))

    decided = len(won) + len(lost)
    rate = f"{round(100 * len(won) / decided)}%" if decided else "n/a"
    text = "\n".join([
        f"Pipeline, last {days} days",
        f"• Open quotes: {len(open_)} worth {total(open_)}",
        f"• Accepted this week: {len(won_recent)} worth {total(won_recent)}",
        f"• All-time win rate: {rate} ({len(won)} won, {len(lost)} lost)",
        f"• Quotes with activity this week: {len(recent)}",
    ])
    return {"open": len(open_), "won_week": len(won_recent), "win_rate": rate, "text": text}
