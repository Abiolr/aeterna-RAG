"""
LLM inference step: gathers file context (services.data_pipeline),
builds the scoring system prompt (services.system_prompt), and calls
the Anthropic API to produce the raw "<json>\\n---\\n<summary>" text
that app.py's /score endpoint parses.

The LLM only produces sub-scores + evidence + missing_inputs +
explanation + a markdown summary. The final survivability_score is
computed deterministically in Python (compute_survivability_score) and
injected into the JSON before it is returned, so the wire format seen
by app.py is unchanged.
"""

import json
import os

from dotenv import load_dotenv
from anthropic import Anthropic

from services.system_prompt import system_prompt
from services.data_pipeline import get_system_data

load_dotenv(override=True)
api_key = os.getenv("ANTHROPIC_API_KEY")


# Weights for the survivability score. Must sum to 1.0.
WEIGHTS = {
    "openness": 0.25,
    "adoption": 0.25,
    "non_supersession": 0.20,
    "metadata_completeness": 0.15,
    "age_lifecycle_health": 0.15,
}


def compute_survivability_score(sub_scores: dict):
    """
    Compute the final survivability score (0-100 int) from the LLM's
    sub-scores, using weight renormalization over non-null terms.

    Args:
        sub_scores: dict mapping term name -> {"score": float|None, ...}.
            Terms with a null/None score are treated as missing.

    Returns:
        (score, active_weight)
            score: int 0-100, or None if every term is null.
            active_weight: float sum of weights for non-null terms
                (0.0 if all are null). Useful for logging/debugging.
    """
    weighted_sum = 0.0
    active_weight = 0.0

    for term, weight in WEIGHTS.items():
        entry = sub_scores.get(term) or {}
        value = entry.get("score")

        if value is None:
            continue

        weighted_sum += weight * float(value)
        active_weight += weight

    if active_weight == 0.0:
        return None, 0.0

    final_fraction = weighted_sum / active_weight
    return round(100 * final_fraction), active_weight


def run_inference(file_path):
    """
    Run the full survivability-scoring pipeline for one file.

    Args:
        file_path: Path to the uploaded file on disk.

    Returns:
        The raw text of the model's reply, reconstructed so it keeps
        the same shape the /score route expects: a JSON scoring object
        (now including a code-computed `survivability_score`), followed
        by a line containing only "---", followed by the markdown
        summary.

    Raises:
        Propagates any exception from the data-gathering step
        (services.data_pipeline.get_system_data), the Anthropic API
        call, or JSON parsing of the model's reply. Callers are
        expected to handle/log this.
    """
    data = get_system_data(file_path)

    client = Anthropic(api_key=api_key)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        system=system_prompt(
            data["file_metadata"],
            data["file_format_details"],
            data["vector_results"],
        ),
        max_tokens=12000,
        messages=[{
            "role": "user",
            "content": (
                "Analyze the retrieved data. Return the scoring JSON first, "
                "followed by the delimiter '---', and then write a "
                "comprehensive markdown summary."
            ),
        }],
    )

    text = response.content[0].text

    # Split off the summary the same way app.py will, so we can inject
    # the code-computed score into the JSON half.
    parts = text.split("---", 1)
    raw_json = (
        parts[0]
        .strip()
        .removeprefix("```json")
        .removesuffix("```")
        .strip()
    )
    summary = parts[1].strip() if len(parts) > 1 else ""

    score_data = json.loads(raw_json)

    # Defensive: if the model ignored instructions and emitted a score,
    # drop it so the code-computed value is the single source of truth.
    score_data.pop("survivability_score", None)

    sub_scores = score_data.get("sub_scores", {})
    score, _active_weight = compute_survivability_score(sub_scores)
    score_data["survivability_score"] = score

    # Re-emit in the exact "<json>\n---\n<summary>" shape app.py parses.
    return f"{json.dumps(score_data)}\n---\n{summary}"