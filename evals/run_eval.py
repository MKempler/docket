"""Run the Docket evaluation: bare model vs Docket (same model) vs Docket's built-in parser (no model).

    python evals/run_eval.py --model qwen3.5-hermes:9b --base-url http://localhost:11434/v1

A job scores only if the final total is exact to the cent, or, for jobs the price book cannot fully price,
if the answer flags that instead of producing a total. Results go to evals/results/.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from docket.intake import model_intake, parse_words  # noqa: E402
from docket.pricebook import load_pricebook  # noqa: E402
from docket.pricing import JobSpec, SpecItem, price, spec_from_args  # noqa: E402

BOOK_PATH = HERE.parent / "demo_data" / "pricebook.yaml"
CENT = Decimal("0.01")

BARE_RULES = """You are pricing a plumbing job from the business's price book below. Follow these rules exactly:
1. Price only from the price book. If any requested work is not in the price book, or the ZIP is in no travel
   zone, set needs_price to true and do not invent a price for it.
2. Always add the service call fee once.
3. Travel: add the fee of the travel zone that contains the job's ZIP (zone A is $0).
4. Assemblies: labor = labor_hours per unit x quantity x labor rate x multiplier; materials = each component's
   qty per unit x quantity (round up to a whole number for 'each' units) x material price.
5. Material price = cost x (1 + markup), rounded to the cent.
6. Multipliers apply to labor only. If several apply (after_hours, weekend, emergency), use only the highest.
7. If the job's total labor hours are under labor.minimum_hours, add the difference as extra labor.
8. Member discount: 10% of all labor (including minimum top-up), as a negative line.
9. Fees (service call, travel, permit, haul-away, camera) are flat and not taxed. LABOR is billed per hour.
10. Sales tax applies to materials only (including materials inside assemblies and materials sold directly).
11. Round each line to the cent (half up). Subtotal = sum of lines. Tax = taxable materials x tax rate,
    rounded to the cent. Total = subtotal + tax.
Return JSON only."""

BARE_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {"type": "array", "items": {"type": "object", "properties": {
            "description": {"type": "string"}, "amount": {"type": "number"}}, "required": ["description", "amount"]}},
        "subtotal": {"type": "number"},
        "tax": {"type": "number"},
        "total": {"type": "number"},
        "needs_price": {"type": "boolean"},
        "needs_price_reason": {"type": "string"},
    },
    "required": ["lines", "total", "needs_price"],
}


class OpenAICompatLLM:
    """The same call shape Docket makes through ctx.llm, sent straight to an OpenAI-compatible endpoint."""

    def __init__(self, base_url: str, model: str, api_key: str = "", reasoning_effort: str = "none"):
        self.base_url, self.model, self.api_key, self.effort = base_url.rstrip("/"), model, api_key, reasoning_effort
        self.calls: list[float] = []

    def complete_structured(self, *, instructions, input, json_schema=None, schema_name=None, temperature=0,
                            timeout=180.0, **_):
        text = "\n\n".join(block["text"] for block in input if block.get("type") == "text")
        body = {"model": self.model, "temperature": temperature,
                "messages": [{"role": "system", "content": instructions}, {"role": "user", "content": text}],
                "response_format": {"type": "json_schema", "json_schema": {"name": schema_name or "out",
                                                                             "schema": json_schema or {}}}}
        if self.effort:
            body["reasoning_effort"] = self.effort
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        t = time.time()
        req = urllib.request.Request(f"{self.base_url}/chat/completions", json.dumps(body).encode(), headers)
        data = json.load(urllib.request.urlopen(req, timeout=timeout))
        self.calls.append(time.time() - t)
        content = data["choices"][0]["message"].get("content") or ""
        try:
            parsed = json.loads(content.strip().removeprefix("```json").removesuffix("```"))
        except json.JSONDecodeError:
            parsed = None

        class R:
            pass
        r = R()
        r.parsed, r.text = parsed, content
        return r


def truth_quote(book, job):
    spec = JobSpec([SpecItem(c, Decimal(str(q))) for c, q in (job.get("items") or {}).items()],
                   zip_code=job.get("zip", ""), flags=tuple(job.get("flags") or ()),
                   customer_tier=job.get("tier", ""),
                   unpriced=["work the price book cannot price"] if job.get("needs_price") else [])
    return price(book, spec)


def score_total(expected_total: Decimal, needs_price: bool, got_total, got_needs: bool) -> bool:
    if needs_price:
        return bool(got_needs)
    if got_needs or got_total is None:
        return False
    try:
        return Decimal(str(got_total)).quantize(CENT) == expected_total
    except (InvalidOperation, ValueError):
        return False


def run(args) -> dict:
    from ruamel.yaml import YAML
    book, errors = load_pricebook(BOOK_PATH)
    assert not errors, errors
    jobs = YAML(typ="safe").load((HERE / args.jobs).read_text())["jobs"]
    if args.limit:
        jobs = jobs[: args.limit]
    llm = OpenAICompatLLM(args.base_url, args.model, args.api_key, args.reasoning_effort)
    book_yaml = BOOK_PATH.read_text()
    rows = []
    for job in jobs:
        truth = truth_quote(book, job)
        expected_needs = bool(job.get("needs_price"))
        expected = truth.total
        row = {"id": job["id"], "text": job["text"], "expected_total": str(expected), "needs_price": expected_needs}

        # Docket, built-in parser only (no model)
        p = parse_words(book, job["text"])
        pq = price(book, spec_from_args(p.args))
        row["parser"] = {"total": str(pq.total), "needs_price": not pq.complete,
                         "ok": score_total(expected, expected_needs, pq.total, not pq.complete)}

        # Docket with the model (intake only; engine prices)
        try:
            d = model_intake(llm, book, job["text"])
            dq = price(book, spec_from_args(d.args))
            row["docket"] = {"total": str(dq.total), "needs_price": not dq.complete, "items": d.args["items"],
                             "flags": d.args["flags"], "tier": d.args["customer_tier"],
                             "ok": score_total(expected, expected_needs, dq.total, not dq.complete)}
        except Exception as exc:
            row["docket"] = {"error": f"{type(exc).__name__}: {exc}", "ok": False}

        # Bare model: full price book + every rule, asked for the total
        try:
            prompt = f"PRICE BOOK (YAML):\n{book_yaml}\n\nJOB: {job['text']}"
            r = llm.complete_structured(instructions=BARE_RULES, input=[{"type": "text", "text": prompt}],
                                        json_schema=BARE_SCHEMA, schema_name="quote")
            out = r.parsed or {}
            row["bare"] = {"total": out.get("total"), "needs_price": bool(out.get("needs_price")),
                           "ok": score_total(expected, expected_needs, out.get("total"), bool(out.get("needs_price")))}
        except Exception as exc:
            row["bare"] = {"error": f"{type(exc).__name__}: {exc}", "ok": False}

        rows.append(row)
        print(f"#{job['id']:>2} expected {'NEEDS PRICE' if expected_needs else expected:>10}  "
              f"bare {'✓' if row['bare']['ok'] else '✗'} {str(row['bare'].get('total')):>10}  "
              f"docket {'✓' if row['docket']['ok'] else '✗'} {row['docket'].get('total', 'ERR'):>10}  "
              f"parser {'✓' if row['parser']['ok'] else '✗'}", flush=True)

    summary = {arm: sum(1 for r in rows if r[arm]["ok"]) for arm in ("bare", "docket", "parser")}
    result = {"jobs_file": args.jobs, "model": args.model, "base_url": args.base_url, "reasoning_effort": args.reasoning_effort,
              "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "jobs": len(rows),
              "score": summary, "median_call_s": sorted(llm.calls)[len(llm.calls) // 2] if llm.calls else None,
              "rows": rows}
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    name = (f"{datetime.now():%Y%m%d-%H%M}-{Path(args.jobs).stem}-"
            f"{args.model.replace(':', '_').replace('/', '_')}.json")
    (out_dir / name).write_text(json.dumps(result, indent=2))
    print(f"\n{args.model}: bare model {summary['bare']}/{len(rows)} · Docket {summary['docket']}/{len(rows)} · "
          f"Docket parser (no model) {summary['parser']}/{len(rows)}  -> evals/results/{name}")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3.5-hermes:9b")
    ap.add_argument("--base-url", default="http://localhost:11434/v1")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--reasoning-effort", default="none")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--jobs", default="jobs40.yaml")
    run(ap.parse_args())
