"""
Pure metric functions for the Aeterna evals (no API / DB access).

A "run" is one dict produced by evals.run_eval:
    {"file", "condition", "run", "ok", "score", "sub_scores",
     "missing_inputs", "usage", "error", ...}
where sub_scores maps term -> {"score": float|None, "evidence": str}.
"""

import statistics
from collections import Counter

TERMS = [
    "openness",
    "adoption",
    "non_supersession",
    "metadata_completeness",
    "age_lifecycle_health",
]

# Evidence text containing any of these is counted as citing the vector store.
VECTOR_CITATION_MARKERS = ("vector_results", "same_format_family", "comparable_formats", "related_formats")

# Claude Haiku 4.5 list prices, USD per million tokens.
PRICE_PER_MTOK = {
    "input": 1.00,
    "output": 5.00,
    "cache_write": 1.25,  # 1.25x input (5-minute TTL)
    "cache_read": 0.10,   # 0.1x input
}


def _std(values):
    return statistics.pstdev(values) if len(values) > 1 else 0.0


def _round(value, ndigits=3):
    return None if value is None else round(value, ndigits)


def _term_value(run, term):
    return ((run.get("sub_scores") or {}).get(term) or {}).get("score")


def _null_terms(run):
    return frozenset(t for t in TERMS if _term_value(run, t) is None)


def _ok(runs):
    return [r for r in runs if r.get("ok") and r.get("score") is not None]


# --------------------------------------------------------------------------
# Variance
# --------------------------------------------------------------------------

def variance_for_file(runs):
    """Repeatability of one file's scores across identical runs."""
    ok = _ok(runs)
    scores = [r["score"] for r in ok]

    term_std = {}
    for term in TERMS:
        values = [v for v in (_term_value(r, term) for r in ok) if v is not None]
        term_std[term] = _round(_std(values)) if values else None

    # How often runs agree on WHICH terms are null (i.e. what data
    # counts as missing) — a separate failure mode from score jitter.
    null_sets = Counter(_null_terms(r) for r in ok)
    null_agreement = (null_sets.most_common(1)[0][1] / len(ok)) if ok else None

    return {
        "runs": len(runs),
        "failures": len(runs) - len(ok),
        "scores": scores,
        "mean": _round(statistics.mean(scores), 2) if scores else None,
        "std": _round(_std(scores), 2) if scores else None,
        "range": (max(scores) - min(scores)) if scores else None,
        "sub_score_std": term_std,
        "null_pattern_agreement": _round(null_agreement),
    }


def variance_summary(per_file):
    """Aggregate variance_for_file results across files."""
    rows = [v for v in per_file.values() if v["std"] is not None]
    total_runs = sum(v["runs"] for v in per_file.values())
    total_fail = sum(v["failures"] for v in per_file.values())

    def mean_of(key):
        values = [r[key] for r in rows if r[key] is not None]
        return _round(statistics.mean(values)) if values else None

    term_std = {}
    for term in TERMS:
        values = [r["sub_score_std"][term] for r in rows if r["sub_score_std"][term] is not None]
        term_std[term] = _round(statistics.mean(values)) if values else None

    ranges = [r["range"] for r in rows]
    return {
        "files": len(per_file),
        "total_runs": total_runs,
        "parse_failure_rate": _round(total_fail / total_runs) if total_runs else None,
        "mean_score_std": mean_of("std"),
        "max_score_range": max(ranges) if ranges else None,
        "pct_files_range_le_5": _round(sum(r <= 5 for r in ranges) / len(ranges)) if ranges else None,
        "pct_files_identical": _round(sum(r == 0 for r in ranges) / len(ranges)) if ranges else None,
        "mean_null_pattern_agreement": mean_of("null_pattern_agreement"),
        "mean_sub_score_std": term_std,
    }


# --------------------------------------------------------------------------
# Ablation (full pipeline vs. lookup only)
# --------------------------------------------------------------------------

def _mean_or_none(values):
    values = [v for v in values if v is not None]
    return statistics.mean(values) if values else None


def _coverage(runs):
    """Mean number of non-null sub-scores per run."""
    ok = _ok(runs)
    return statistics.mean(len(TERMS) - len(_null_terms(r)) for r in ok) if ok else None


def _vector_citation_rate(runs):
    """Fraction of runs where at least one evidence string cites vector_results."""
    ok = _ok(runs)
    if not ok:
        return None
    cited = 0
    for run in ok:
        evidence = " ".join(
            str(((run.get("sub_scores") or {}).get(t) or {}).get("evidence") or "") for t in TERMS
        )
        cited += any(marker in evidence for marker in VECTOR_CITATION_MARKERS)
    return cited / len(ok)


def ablation_for_file(full_runs, lookup_runs):
    """Effect of removing vector_results for one file."""
    full_ok, lookup_ok = _ok(full_runs), _ok(lookup_runs)
    full_mean = _mean_or_none([r["score"] for r in full_ok])
    lookup_mean = _mean_or_none([r["score"] for r in lookup_ok])

    delta = None if full_mean is None or lookup_mean is None else full_mean - lookup_mean

    # Treat a delta as real only if it clears the run-to-run noise of
    # both conditions (2x the larger std, with a 2-point floor).
    noise = 2 * max(
        _std([r["score"] for r in full_ok]),
        _std([r["score"] for r in lookup_ok]),
        1.0,
    )

    term_delta = {}
    for term in TERMS:
        f = _mean_or_none([_term_value(r, term) for r in full_ok])
        l = _mean_or_none([_term_value(r, term) for r in lookup_ok])
        term_delta[term] = _round(f - l) if f is not None and l is not None else None

    full_cov, lookup_cov = _coverage(full_runs), _coverage(lookup_runs)
    return {
        "full_mean": _round(full_mean, 2),
        "lookup_only_mean": _round(lookup_mean, 2),
        "delta": _round(delta, 2),
        "noise_threshold": _round(noise, 2),
        "beyond_noise": None if delta is None else abs(delta) > noise,
        "sub_score_delta": term_delta,
        "full_coverage": _round(full_cov, 2),
        "lookup_only_coverage": _round(lookup_cov, 2),
        "coverage_gain": _round(full_cov - lookup_cov, 2) if full_cov is not None and lookup_cov is not None else None,
        "vector_citation_rate": _round(_vector_citation_rate(full_runs)),
    }


def ablation_summary(per_file):
    rows = [v for v in per_file.values() if v["delta"] is not None]

    def mean_of(key, fn=lambda x: x):
        values = [fn(r[key]) for r in rows if r[key] is not None]
        return _round(statistics.mean(values)) if values else None

    term_abs = {}
    for term in TERMS:
        values = [abs(r["sub_score_delta"][term]) for r in rows if r["sub_score_delta"][term] is not None]
        term_abs[term] = _round(statistics.mean(values)) if values else None

    return {
        "files": len(per_file),
        "mean_abs_delta": mean_of("delta", abs),
        "mean_signed_delta": mean_of("delta"),
        "files_changed_beyond_noise": sum(bool(r["beyond_noise"]) for r in rows),
        "mean_coverage_gain": mean_of("coverage_gain"),
        "mean_vector_citation_rate": mean_of("vector_citation_rate"),
        "mean_abs_sub_score_delta": term_abs,
    }


# --------------------------------------------------------------------------
# Cost
# --------------------------------------------------------------------------

def cost_summary(runs):
    totals = Counter()
    for run in runs:
        for key, value in (run.get("usage") or {}).items():
            totals[key] += value or 0
    usd = (
        totals["input_tokens"] * PRICE_PER_MTOK["input"]
        + totals["output_tokens"] * PRICE_PER_MTOK["output"]
        + totals["cache_creation_input_tokens"] * PRICE_PER_MTOK["cache_write"]
        + totals["cache_read_input_tokens"] * PRICE_PER_MTOK["cache_read"]
    ) / 1_000_000
    return {"calls": len(runs), **dict(totals), "estimated_usd": round(usd, 4)}
