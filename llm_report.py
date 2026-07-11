#!/usr/bin/env python3
"""
LLM daily energy report generator.

Designed to run as a ONE-SHOT SUBPROCESS -- not imported or kept alive.
Loads the model, runs inference, prints the report to stdout, then exits,
fully releasing its ~1GB RAM footprint.

The parent process (scheduler.py) launches this via subprocess.run(),
feeds the daily summary JSON via stdin, reads the report from stdout.

Usage (normally called by scheduler.py, but testable manually):
    .venv/bin/python scheduler.py --run-llm-once

    # Or pipe directly to test prompt building without the scheduler:
    .venv/bin/python - << 'EOF'
    import json
    from client import build_client_from_env
    from daily_summary import build_daily_summary
    client = build_client_from_env()
    client.login()
    summary = build_daily_summary(client)
    print(json.dumps(summary))
    EOF | .venv/bin/python llm_report.py

Model: Qwen2.5-1.5B-Instruct Q4_K_M GGUF
    ~1.0 GB on disk, ~1.1-1.3 GB resident RAM during inference
    ~3-6 tokens/second on Pi4 ARM CPU (no GPU)
    150-250 token report = ~30-90 seconds -- fine for a once-a-day job

Installation (on the Pi):
    CMAKE_ARGS="-DGGML_NATIVE=on" .venv/bin/pip install llama-cpp-python --no-cache-dir

Model download:
    mkdir -p ~/models
    curl -L -o ~/models/qwen2.5-1.5b-instruct-q4_k_m.gguf \\
      https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf

If 2GB RAM is too tight alongside the running scheduler service,
fall back to the 0.5B model (~400MB):
    curl -L -o ~/models/qwen2.5-0.5b-instruct-q4_k_m.gguf \\
      https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf
    Then update MODEL_PATH below.
"""

import json
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    stream=sys.stderr,   # logs to stderr so stdout is clean for the report text
)
logger = logging.getLogger("llm_report")

# ── Configuration ─────────────────────────────────────────────────────
MODEL_PATH = os.environ.get(
    "FUSIONSOLAR_LLM_MODEL_PATH",
    "/home/translight-iot/models/qwen2.5-1.5b-instruct-q4_k_m.gguf",
)

N_CTX       = 2048    # context window -- 2048 handles fleets of up to ~20 plants
N_THREADS   = 4       # Pi 4 has 4 cores
MAX_TOKENS  = 300     # ~200 words, more than enough for the report
TEMPERATURE = 0.3     # low = more consistent/deterministic output

# Plants below this deviation get flagged as needing attention in the prompt
DEVIATION_FLAG_PCT = -15.0


def build_prompt(summary: dict) -> str:
    """
    Builds the LLM prompt from the pre-computed summary dict.

    IMPORTANT: The prompt explicitly tells the model to use ONLY the
    numbers provided. Small quantized models hallucinate plausible-looking
    figures if given any latitude to calculate -- all math is done in
    daily_summary.py before this function is ever called.
    """
    date   = summary.get("date", "today")
    fleet  = summary.get("fleet_total", {})
    plants = summary.get("plants", [])
    psh    = summary.get("psh", 4.5)

    # Compact per-plant lines -- keeps the prompt short to save tokens and RAM
    plant_lines = []
    for p in plants:
        gen   = p.get("generation_kwh")
        exp   = p.get("expected_kwh")
        dev   = p.get("deviation_pct")
        imp   = p.get("import_kwh")
        flags = p.get("flags") or []

        gen_str  = f"{gen:.1f} kWh" if gen is not None else "no data"
        exp_str  = f"{exp:.1f} kWh" if exp is not None else "unknown"
        dev_str  = f"{dev:+.1f}%" if dev is not None else "n/a"
        imp_str  = f"{imp:.1f} kWh import" if imp is not None else ""
        flag_str = f" [FLAGS: {', '.join(flags)}]" if flags else ""

        plant_lines.append(
            f"  - {p['name']}: {gen_str} generated / {exp_str} expected"
            f" ({dev_str})"
            + (f" | {imp_str}" if imp_str else "")
            + flag_str
        )

    fleet_gen = fleet.get("generation_kwh", 0)
    fleet_exp = fleet.get("expected_kwh", 0)
    attention = fleet.get("plants_needing_attention") or []
    attn_str  = ", ".join(attention) if attention else "none"

    return f"""You are writing a concise daily solar fleet performance report for a Telegram message.
You are reporting to a solar engineer. Keep it professional and brief.

RULES:
- Use ONLY the numbers provided below. Do not calculate or estimate any figures yourself.
- Flag any plant with a FLAG tag or deviation worse than {DEVIATION_FLAG_PCT:.0f}% as needing attention.
- Keep it under 200 words. Plain text with short line breaks. No markdown tables or bullet symbols.
- End with a one-line overall fleet assessment.

DATE: {date}
LOCATION: Ghana (PSH: {psh}h/day)
FLEET TOTAL: {fleet_gen:.1f} kWh generated / {fleet_exp:.1f} kWh expected
PLANTS NEEDING ATTENTION: {attn_str}

PER-PLANT BREAKDOWN:
{chr(10).join(plant_lines)}

Write the daily performance report now:"""


def generate_report(summary: dict) -> str:
    """Loads the model, runs inference, returns the report string."""
    try:
        from llama_cpp import Llama
    except ImportError:
        logger.error(
            "llama-cpp-python is not installed. Run:\n"
            "  CMAKE_ARGS=\"-DGGML_NATIVE=on\" .venv/bin/pip install llama-cpp-python --no-cache-dir"
        )
        sys.exit(2)

    if not os.path.exists(MODEL_PATH):
        logger.error(
            "Model file not found at: %s\n"
            "Download with:\n"
            "  mkdir -p ~/models\n"
            "  curl -L -o %s \\\n"
            "    https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/"
            "qwen2.5-1.5b-instruct-q4_k_m.gguf",
            MODEL_PATH, MODEL_PATH,
        )
        sys.exit(2)

    logger.info("Loading model from %s ...", MODEL_PATH)
    llm = Llama(
        model_path=MODEL_PATH,
        n_ctx=N_CTX,
        n_threads=N_THREADS,
        verbose=False,
    )

    prompt = build_prompt(summary)
    logger.info("Running inference (max_tokens=%d, temperature=%.1f)...", MAX_TOKENS, TEMPERATURE)

    output = llm.create_chat_completion(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        stop=["<|im_end|>", "<|endoftext|>"],
    )

    report = output["choices"][0]["message"]["content"].strip()
    tokens_used = output.get("usage", {}).get("completion_tokens", "?")
    logger.info("Inference complete. %s tokens generated.", tokens_used)

    return report


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        logger.error("No JSON received on stdin.")
        return 1

    try:
        summary = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse stdin as JSON: %s", exc)
        return 1

    report = generate_report(summary)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
