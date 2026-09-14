"""
LLM inference step: gathers file context (services.data_pipeline),
builds the scoring system prompt (services.system_prompt), and calls
the Anthropic API to produce the raw "<json>\\n---\\n<summary>" text
that app.py's /score endpoint parses.
"""

import os
from dotenv import load_dotenv
from anthropic import Anthropic
from services.system_prompt import system_prompt
from services.data_pipeline import get_system_data

load_dotenv()
api_key = os.getenv("ANTHROPIC_API_KEY")


def run_inference(file_path):
    """
    Run the full survivability-scoring pipeline for one file.

    Args:
        file_path: Path to the uploaded file on disk.

    Returns:
        The raw text of the model's reply: a JSON scoring object,
        followed by a line containing only "---", followed by a
        markdown summary. (Parsing of this text happens in
        app.py's /score route, not here.)

    Raises:
        Propagates any exception from the data-gathering step
        (services.data_pipeline.get_system_data) or from the
        Anthropic API call itself (e.g. network errors, auth errors
        if ANTHROPIC_API_KEY is missing/invalid, rate limits).
        Callers are expected to handle/log this.
    """
    data = get_system_data(file_path)

    client = Anthropic(api_key=api_key)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        system=system_prompt(
            data["file_metadata"],
            data["file_format_details"],
            data["vector_results"]
        ),
        max_tokens=12000,
        messages=[{
            "role": "user",
            "content": "Analyze the retrieved data. Return the scoring JSON first, followed by the delimiter '---', and then write a comprehensive markdown summary."
            } 
        ],
    )

    text = response.content[0].text

    return text