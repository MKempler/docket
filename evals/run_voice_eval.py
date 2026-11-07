"""Voice eval: the same jobs, spoken aloud, transcribed the way Hermes transcribes voice notes, then quoted by Docket.

    python evals/run_voice_eval.py --jobs jobs_holdout20.yaml --whisper base

For each job:
  1. spoken(text) rewrites the written job the way a person would say it (ZIP digit by digit, "half inch",
     "Street", "times two"). One fixed rule set, applied to every job, never tuned on results.
  2. macOS `say` speaks it (three voices in rotation) into a Telegram-style Opus voice note.
  3. faster-whisper transcribes it with Hermes's own local settings (beam 5, VAD, confidence gate, English),
     twice: plain, and with Docket's pre_transcription vocabulary prompt.
  4. Docket quotes each transcript (model intake + pricing engine), scored exactly like run_eval.py.

Synthetic voices are cleaner than a real person in a basement, so this measures the pipeline, not field
accuracy. Audio goes to evals/voice_audio/ (not committed); results to evals/results/.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

from run_eval import BOOK_PATH, OpenAICompatLLM, score_total, truth_quote  # noqa: E402

from docket.intake import model_intake, parse_words  # noqa: E402
from docket.pricebook import load_pricebook  # noqa: E402
from docket.pricing import price, spec_from_args  # noqa: E402
from docket.voice import vocab_prompt  # noqa: E402

VOICES = ["Samantha", "Reed (English (US))", "Eddy (English (US))"]
DIGITS = "oh one two three four five six seven eight nine".split()
STREETS = {"St": "Street", "Ave": "Avenue", "Rd": "Road", "Ln": "Lane", "Dr": "Drive", "Blvd": "Boulevard",
           "Ct": "Court"}


def spoken(text: str) -> str:
    """How a person would say a written job note out loud."""
    t = re.sub(r"\b(\d{5})\b", lambda m: " ".join(DIGITS[int(d)] for d in m.group(1)), text)      # ZIPs
    t = re.sub(r"\b1-1/2\b", "one and a half", t)
    t = re.sub(r"\b1/2\b", "half", t)
    t = re.sub(r"\b3/4\b", "three quarter", t)
    t = re.sub(r"\b1/4\b", "quarter", t)
    t = re.sub(r"\bx(\d+)\b|\b(\d+)x\b", lambda m: f"times {m.group(1) or m.group(2)}", t)
    t = re.sub(r"\b(\d+(?:\.\d+)?)\s*hrs?\b", r"\1 hours", t)
    t = re.sub(r"\b(" + "|".join(STREETS) + r")\b\.?", lambda m: STREETS[m.group(1)], t)
    t = re.sub(r"\bPEX\b", "pecks", t, flags=re.I).replace(" - ", ", ").replace(":", ",")   # how `say` should pronounce PEX
    return t


def synthesize(text: str, voice: str, out: Path) -> Path:
    if out.exists():
        return out
    aiff = out.with_suffix(".aiff")
    subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(aiff), "-c:a", "libopus", "-b:a", "32k", str(out)],
                   check=True)
    aiff.unlink()
    return out


class Whisper:
    """faster-whisper with Hermes's local kwargs (tools/transcription_local.build_local_transcribe_kwargs)."""

    def __init__(self, model: str):
        from faster_whisper import WhisperModel
        self.model = WhisperModel(model, device="cpu", compute_type="int8")

    def __call__(self, path: Path, prompt: str | None = None) -> str:
        import numpy as np
        pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar",
                              "16000", "-"], capture_output=True, check=True).stdout
        audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
        kw = {"beam_size": 5, "condition_on_previous_text": False, "vad_filter": True,
              "vad_parameters": {"min_silence_duration_ms": 500}, "no_speech_threshold": 0.6,
              "log_prob_threshold": -1.0, "language": "en"}
        if prompt:
            kw["initial_prompt"] = prompt
        segs, _ = self.model.transcribe(audio, **kw)
        return " ".join(s.text.strip() for s in segs if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0)).strip()


def quote_arm(llm, book, text: str, expected, expected_needs: bool) -> dict:
    p = parse_words(book, text)
    pq = price(book, spec_from_args(p.args))
    out = {"parser_total": str(pq.total), "parser_ok": score_total(expected, expected_needs, pq.total, not pq.complete)}
    try:
        d = model_intake(llm, book, text)
        dq = price(book, spec_from_args(d.args))
        out.update({"total": str(dq.total), "needs_price": not dq.complete, "items": d.args["items"],
                    "ok": score_total(expected, expected_needs, dq.total, not dq.complete)})
    except Exception as exc:
        out.update({"error": f"{type(exc).__name__}: {exc}", "ok": False})
    return out


def run(args) -> dict:
    from ruamel.yaml import YAML
    book, errors = load_pricebook(BOOK_PATH)
    assert not errors, errors
    jobs = YAML(typ="safe").load((HERE / args.jobs).read_text())["jobs"]
    if args.limit:
        jobs = jobs[: args.limit]
    audio_dir = HERE / "voice_audio"
    audio_dir.mkdir(exist_ok=True)
    llm = OpenAICompatLLM(args.base_url, args.model, "", "none")
    whisper = Whisper(args.whisper)
    prompt = vocab_prompt(book)
    rows = []
    for i, job in enumerate(jobs):
        truth = truth_quote(book, job)
        expected, expected_needs = truth.total, bool(job.get("needs_price"))
        voice = VOICES[i % len(VOICES)]
        said = spoken(job["text"])
        audio = synthesize(said, voice, audio_dir / f"{Path(args.jobs).stem}-{job['id']}.ogg")
        t0 = time.time()
        plain = whisper(audio)
        t1 = time.time()
        hinted = whisper(audio, prompt)
        row = {"id": job["id"], "written": job["text"], "spoken": said, "voice": voice,
               "expected_total": str(expected), "needs_price": expected_needs,
               "stt_s": round(t1 - t0, 2), "plain": {"transcript": plain}, "vocab": {"transcript": hinted}}
        row["plain"].update(quote_arm(llm, book, plain, expected, expected_needs))
        row["vocab"].update(quote_arm(llm, book, hinted, expected, expected_needs))
        rows.append(row)
        print(f"#{job['id']:>3} {'NEEDS' if expected_needs else expected:>9}  plain {'✓' if row['plain']['ok'] else '✗'}"
              f"  vocab {'✓' if row['vocab']['ok'] else '✗'}  | {hinted[:90]}", flush=True)
    score = {arm: sum(1 for r in rows if r[arm]["ok"]) for arm in ("plain", "vocab")}
    score.update({f"{arm}_parser": sum(1 for r in rows if r[arm]["parser_ok"]) for arm in ("plain", "vocab")})
    result = {"jobs_file": args.jobs, "model": args.model, "whisper": args.whisper, "voices": VOICES,
              "vocab_prompt": prompt, "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "jobs": len(rows), "score": score, "model_call_failures": llm.failures, "valid": llm.failures == 0,
              "rows": rows}
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    name = f"{datetime.now():%Y%m%d-%H%M}-voice-{Path(args.jobs).stem}-whisper_{args.whisper}.json"
    (out_dir / name).write_text(json.dumps(result, indent=2))
    if llm.failures:
        print(f"\nINVALID RUN: {llm.failures} model call(s) failed.")
    n = len(rows)
    print(f"\nVoice, whisper {args.whisper}: Docket {score['plain']}/{n} plain · {score['vocab']}/{n} with vocabulary "
          f"(parser {score['plain_parser']}/{n} · {score['vocab_parser']}/{n})  -> evals/results/{name}")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", default="jobs_holdout20.yaml")
    ap.add_argument("--whisper", default="base")
    ap.add_argument("--model", default="qwen3.5-hermes:9b")
    ap.add_argument("--base-url", default="http://localhost:11434/v1")
    ap.add_argument("--limit", type=int, default=0)
    run(ap.parse_args())
