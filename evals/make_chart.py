"""Draw assets/eval-chart.png from two run_eval.py result files, so the README chart always matches the data.

    python evals/make_chart.py evals/results/<...jobs40...>.json evals/results/<...holdout20...>.json \
        [evals/results/<...jobs40-parser-only>.json evals/results/<...holdout20-parser-only>.json]

The optional parser-only files (run_eval.py --parser-only) replace the parser bars, for when the built-in parser
changed after the full run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ARMS = [("bare", "Bare model, full price book + rules", "#EB6834"),
        ("parser", "Docket parser, no model", "#1BB07A"),
        ("docket", "Docket (same model)", "#2979D6")]
GROUPS = ["Original 40 jobs", "Held-out 20 jobs\n(never tuned on)"]


def main(original: str, held_out: str, parser_original: str = "", parser_held_out: str = "",
         out: str = "assets/eval-chart.png") -> None:
    runs = [json.loads(Path(p).read_text()) for p in (original, held_out)]
    for run, parser_file in zip(runs, (parser_original, parser_held_out)):
        if parser_file:
            parser = json.loads(Path(parser_file).read_text())
            assert parser["jobs_file"] == run["jobs_file"], "parser-only file is for a different job set"
            run["score"]["parser"] = parser["score"]["parser"]
    for run in runs:
        assert run.get("valid", True), f"{run['jobs_file']}: invalid run (model calls failed)"
    fig, ax = plt.subplots(figsize=(8.6, 4.4), dpi=200)
    fig.patch.set_facecolor("#FCFCFB")
    ax.set_facecolor("#FCFCFB")
    bar_h, gap = 0.26, 0.04
    for g, run in enumerate(runs):
        n = run["jobs"]
        for i, (key, _, color) in enumerate(ARMS):
            score = run["score"][key]
            y = g * 1.3 + (i - 1) * (bar_h + gap)
            pct = score / n * 100
            ax.barh(y, pct, height=bar_h, color=color, zorder=3)
            label = f"{score}/{n}  ·  {pct:.1f}".rstrip("0").rstrip(".") + "%"
            strong = key == "docket"
            ax.text(pct + 1.2, y, label, va="center", fontsize=10.5 if strong else 10,
                    fontweight="bold" if strong else "normal", color="#1F1F1F" if strong else "#555555")
    ax.set_yticks([g * 1.3 for g in range(len(runs))], GROUPS, fontsize=11, color="#222222")
    ax.invert_yaxis()
    ax.set_xlim(0, 112)
    ax.set_xticks([0, 25, 50, 75, 100], ["0%", "25%", "50%", "75%", "100%"], fontsize=9, color="#888888")
    ax.grid(axis="x", color="#E3E3E3", zorder=0)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0)
    model = runs[0].get("model", "")
    fig.text(0.02, 0.925, "Exact quotes, same local model, with and without Docket", fontsize=15, fontweight="bold",
             color="#1F1F1F")
    fig.text(0.02, 0.865, f"{'Qwen 3.5 9B' if 'qwen3.5' in model else model} (local, thinking off). A job counts only if "
             "the total is exact to the cent, or work the price book can't price is flagged.", fontsize=8.6, color="#555555")
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for _, _, c in ARMS]
    fig.legend(handles, [label for _, label, _ in ARMS], loc="upper center", bbox_to_anchor=(0.55, 0.83), ncol=3,
               frameon=False, fontsize=9.5, labelcolor="#444444", handlelength=1.1)
    fig.subplots_adjust(left=0.21, right=0.97, top=0.70, bottom=0.08)
    fig.savefig(out, facecolor=fig.get_facecolor())
    print("wrote", out)


if __name__ == "__main__":
    main(*sys.argv[1:])
