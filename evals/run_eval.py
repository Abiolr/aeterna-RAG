"""
Cheap evals for the Aeterna scoring pipeline.

Measures two things:

  1. Variance — score the same file N times with identical CONTEXT.
     How repeatable are the final score, each sub-score, and the set of
     terms the LLM marks as missing?
  2. Ablation — score the same file N more times with vector_results
     emptied (standard PRONOM/LoC lookup only). How much does the vector
     store move the score, fill otherwise-null sub-scores, and get cited
     as evidence?

There is no ground truth here, so these are consistency/influence
metrics, not accuracy.

Cost controls:
  - CONTEXT (ExifTool + Postgres lookup + vector search) is gathered once
    per file and reused for every run.
  - Generation stops at the "---" delimiter, so the markdown summary is
    never generated — only the scoring JSON.
  - The system prompt is prompt-cached, so repeat runs of the same file
    and condition read it at 0.1x input price.
  - The variance runs double as the ablation's "full" condition.
  Default (10 files x 3 runs x 2 conditions = 60 Haiku 4.5 calls) costs
  roughly $0.25-0.50.

Requires ANTHROPIC_API_KEY and POSTGRES_URL (lookup tables built), same
as the API itself.

Usage (from the repo root):
    python -m evals.run_eval                       # test_files/*, 3 runs
    python -m evals.run_eval --runs 5 --files test_files/*.png
    python -m evals.run_eval --no-ablation         # variance only

Outputs go to evals/results/<timestamp>/:
    runs.jsonl    one line per LLM call
    summary.json  all metrics
    report.md     human-readable tables
"""

import argparse
import copy
import glob
import json
import os
import time
from datetime import datetime, timezone

import anthropic
from anthropic import Anthropic

from evals import metrics
from services.data_pipeline import get_system_data
from services.llm_inference import MODEL, api_key, score_context

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")

FULL = "full"
LOOKUP_ONLY = "lookup_only"


def _lookup_only(data):
    """Same CONTEXT with every vector_results list emptied."""
    ablated = copy.deepcopy(data)
    vector = ablated.get("vector_results") or {}
    ablated["vector_results"] = {
        "query": vector.get("query"),
        "same_format_family": [],
        "related_formats": [],
        "comparable_formats": [],
    }
    return ablated


def _usage(response):
    u = response.usage
    return {
        "input_tokens": u.input_tokens or 0,
        "output_tokens": u.output_tokens or 0,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
    }


def _score_once(client, data, file_name, condition, run, max_tokens):
    record = {"file": file_name, "condition": condition, "run": run, "ok": False}
    start = time.perf_counter()
    try:
        score_data, _summary, response = score_context(
            data,
            client=client,
            with_summary=False,
            max_tokens=max_tokens,
            cache_system=True,
        )
        record.update({
            "ok": True,
            "score": score_data.get("survivability_score"),
            "sub_scores": score_data.get("sub_scores"),
            "missing_inputs": score_data.get("missing_inputs"),
            "stop_reason": response.stop_reason,
            "usage": _usage(response),
        })
    except json.JSONDecodeError as exc:
        record["error"] = f"invalid_json: {exc}"
    except anthropic.APIError as exc:
        record["error"] = f"api_error: {exc}"
    record["latency_s"] = round(time.perf_counter() - start, 2)
    return record


def _fmt(value):
    return "–" if value is None else value


def _report(summary):
    v, a, c = summary["variance"], summary.get("ablation"), summary["cost"]
    lines = [
        f"# Aeterna eval — {summary['timestamp']}",
        "",
        f"{summary['config']['files']} files × {summary['config']['runs']} runs per condition. "
        f"Estimated cost: ${c['estimated_usd']} over {c['calls']} calls "
        f"({c.get('cache_read_input_tokens', 0):,} cached input tokens).",
        "",
        "## Variance (same file, same CONTEXT, repeated)",
        "",
        f"- Mean score std: **{_fmt(v['summary']['mean_score_std'])}** points (0–100 scale)",
        f"- Max score range: **{_fmt(v['summary']['max_score_range'])}** points",
        f"- Files with range ≤ 5: **{_fmt(v['summary']['pct_files_range_le_5'])}**; "
        f"identical every run: **{_fmt(v['summary']['pct_files_identical'])}**",
        f"- Null-pattern agreement (runs agree on which terms are missing): "
        f"**{_fmt(v['summary']['mean_null_pattern_agreement'])}**",
        f"- Parse failure rate: **{_fmt(v['summary']['parse_failure_rate'])}**",
        "",
        "Mean sub-score std (0–1 scale): "
        + ", ".join(f"{t} {_fmt(s)}" for t, s in v["summary"]["mean_sub_score_std"].items()),
        "",
        "| File | Scores | Mean | Std | Range | Null agreement |",
        "|---|---|---|---|---|---|",
    ]
    for name, row in v["per_file"].items():
        lines.append(
            f"| {name} | {row['scores']} | {_fmt(row['mean'])} | {_fmt(row['std'])} | "
            f"{_fmt(row['range'])} | {_fmt(row['null_pattern_agreement'])} |"
        )

    if a:
        s = a["summary"]
        lines += [
            "",
            "## Ablation (full pipeline vs. standard lookup only)",
            "",
            f"- Mean |score delta|: **{_fmt(s['mean_abs_delta'])}** points "
            f"(signed mean {_fmt(s['mean_signed_delta'])}; + means vector context raises the score)",
            f"- Files whose score moved beyond run-to-run noise: "
            f"**{s['files_changed_beyond_noise']}/{s['files']}**",
            f"- Mean extra non-null sub-scores with vector context: **{_fmt(s['mean_coverage_gain'])}**",
            f"- Runs citing vector_results as evidence: **{_fmt(s['mean_vector_citation_rate'])}**",
            "",
            "Mean |sub-score delta| (0–1 scale): "
            + ", ".join(f"{t} {_fmt(d)}" for t, d in s["mean_abs_sub_score_delta"].items()),
            "",
            "| File | Full | Lookup only | Delta | Noise | Beyond noise | Coverage (full / lookup) | Vector cited |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for name, row in a["per_file"].items():
            lines.append(
                f"| {name} | {_fmt(row['full_mean'])} | {_fmt(row['lookup_only_mean'])} | "
                f"{_fmt(row['delta'])} | ±{_fmt(row['noise_threshold'])} | {_fmt(row['beyond_noise'])} | "
                f"{_fmt(row['full_coverage'])} / {_fmt(row['lookup_only_coverage'])} | "
                f"{_fmt(row['vector_citation_rate'])} |"
            )

    lines += [
        "",
        "_No ground truth: these measure consistency and the vector store's influence, not accuracy._",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--files", nargs="*", default=None, help="Files to score (default: test_files/*)")
    parser.add_argument("--runs", type=int, default=3, help="Runs per file per condition (default 3)")
    parser.add_argument("--no-ablation", action="store_true", help="Skip the lookup-only condition")
    parser.add_argument("--max-tokens", type=int, default=2000, help="Output cap per call (JSON only)")
    args = parser.parse_args()

    files = sorted(args.files or glob.glob("test_files/*"))
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        parser.error("no files to score")

    conditions = [FULL] if args.no_ablation else [FULL, LOOKUP_ONLY]
    print(f"Scoring {len(files)} files × {args.runs} runs × {len(conditions)} conditions "
          f"= {len(files) * args.runs * len(conditions)} calls")

    client = Anthropic(api_key=api_key)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = os.path.join(RESULTS_DIR, timestamp)
    os.makedirs(out_dir, exist_ok=True)

    runs = []
    with open(os.path.join(out_dir, "runs.jsonl"), "w", encoding="utf-8") as log:
        for path in files:
            name = os.path.basename(path)
            try:
                data = get_system_data(path)
            except Exception as exc:  # e.g. unknown extension, ExifTool error
                print(f"  {name}: skipped, context gathering failed ({exc})")
                continue

            for condition in conditions:
                context = data if condition == FULL else _lookup_only(data)
                for run in range(args.runs):
                    record = _score_once(client, context, name, condition, run, args.max_tokens)
                    runs.append(record)
                    log.write(json.dumps(record, default=str) + "\n")
                    log.flush()
                    status = record["score"] if record["ok"] else record["error"][:60]
                    print(f"  {name} [{condition} #{run + 1}] -> {status}")

    by_file = {}
    for record in runs:
        by_file.setdefault(record["file"], {}).setdefault(record["condition"], []).append(record)

    variance = {name: metrics.variance_for_file(c.get(FULL, [])) for name, c in by_file.items()}
    summary = {
        "timestamp": timestamp,
        "config": {"files": len(by_file), "runs": args.runs, "conditions": conditions,
                   "model": MODEL},
        "variance": {"summary": metrics.variance_summary(variance), "per_file": variance},
        "cost": metrics.cost_summary(runs),
    }
    if LOOKUP_ONLY in conditions:
        ablation = {
            name: metrics.ablation_for_file(c.get(FULL, []), c.get(LOOKUP_ONLY, []))
            for name, c in by_file.items()
        }
        summary["ablation"] = {"summary": metrics.ablation_summary(ablation), "per_file": ablation}

    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
    report = _report(summary)
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(report)

    print("\n" + report)
    print(f"Results written to {out_dir}")


if __name__ == "__main__":
    main()
