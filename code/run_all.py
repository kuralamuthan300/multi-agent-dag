#!/usr/bin/env python3
"""Batch runner for flow.py — executes 9 queries, captures logs,
saves them to a timestamped file, and generates README.md."""

import subprocess
import sys
import time
import os
from datetime import datetime
from pathlib import Path

# ── project layout ─────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
FLOW_PY = ROOT / "code" / "flow.py"
LOGS_DIR = ROOT / "run_logs"
README_PATH = ROOT / "README.md"

LOGS_DIR.mkdir(parents=True, exist_ok=True)

# ── queries ────────────────────────────────────────────────────────────────
QUERIES = [
    # ── from the attached file ──
    {
        "id": "arg1-hello",
        "purpose": "Basic hello-world test",
        "query": "Say hello."
    },
    {
        "id": "arg2-shannon",
        "purpose": "Fetch Wikipedia article (researcher web fetch)",
        "query": "Fetch https://en.wikipedia.org/wiki/Claude_Shannon and tell me his birth date, death date, and three key contributions to information theory."
    },
    {
        "id": "arg3-populations",
        "purpose": "Multi-city research (parallel researchers)",
        "query": "Find the populations of London, Paris, Berlin and tell me which two are closest in size."
    },
    {
        "id": "arg4-nonexistent",
        "purpose": "Error handling (read non-existent file)",
        "query": "Read /nonexistent/path.txt and tell me what's in it."
    },
    {
        "id": "arg5-africa",
        "purpose": "Multi-city Africa research (parallel researchers)",
        "query": "For Lagos, Cairo, and Kinshasa, find current populations and growth rates and tell me which is growing fastest."
    },
    # ── skill-specific ──
    {
        "id": "skill-parallel",
        "purpose": "Parallel processing — multiple financial/tech queries spawned by Planner",
        "query": "Find the current market capitalization of Apple, Microsoft, and Google (Alphabet), and tell me which company has the highest market cap and by how much it leads the second-place company."
    },
    {
        "id": "skill-critic",
        "purpose": "Critic skill — Researcher fetches → Critic fact-checks → Distiller formats structured output",
        "query": "Fetch the Wikipedia article on the Pyramids of Giza and extract: who built them, when were they built, and how long did it take to construct the Great Pyramid. Also investigate the claim that extraterrestrials built the pyramids — present any evidence for or against this, with sources."
    },
    {
        "id": "skill-coder",
        "purpose": "Coder skill + SandboxExecutor — Kadane's algorithm",
        "query": "Write a Python function implementing Kadane's algorithm to find the maximum sum subarray in an array that may contain negative numbers. Then test it on the input [-2, 1, -3, 4, -1, 2, 1, -5, 4] and run it in the sandbox to show the result."
    },
    {
        "id": "skill-token-miser",
        "purpose": "Token Miser — large Wikipedia article triggers auto-compression",
        "query": "Fetch the Wikipedia article on the Solar System and summarise its contents covering: the Sun, all eight planets, the asteroid belt, Kuiper belt, and Oort cloud. Provide key facts about each celestial body including size, distance from Sun, orbital period, and unique characteristics. Be thorough and use detailed descriptions."
    },
]


# ── helpers ────────────────────────────────────────────────────────────────

def run_flow(query: str, label: str) -> dict:
    """Execute flow.py with the given query, capture output."""
    # uv needs to run from the code/ directory where pyproject.toml lives
    cmd = ["uv", "run", "python3", "flow.py", query]
    print(f"\n{'#' * 78}")
    print(f"# [{label}] Running: uv run python3 flow.py <query>")
    print(f"{'#' * 78}\n")

    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 min per query; adjust if needed
            cwd=str(ROOT / "code"),  # run from code/ so uv finds pyproject.toml
        )
        elapsed = time.time() - start
        print(result.stdout)
        if result.stderr:
            print(f"[stderr]\n{result.stderr}")
        return {
            "label": label,
            "query": query,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "elapsed": elapsed,
            "success": result.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        print(f"[{label}] TIMED OUT after {elapsed:.1f}s")
        return {
            "label": label,
            "query": query,
            "returncode": -1,
            "stdout": "(timed out)",
            "stderr": "",
            "elapsed": elapsed,
            "success": False,
        }
    except Exception as e:
        elapsed = time.time() - start
        print(f"[{label}] EXCEPTION: {e}")
        return {
            "label": label,
            "query": query,
            "returncode": -2,
            "stdout": f"(exception: {e})",
            "stderr": "",
            "elapsed": elapsed,
            "success": False,
        }


def build_log_text(results: list[dict]) -> str:
    """Build a single text block with all logs."""
    lines = []
    lines.append("=" * 78)
    lines.append("MULTI-AGENT DAG — BATCH RUN LOG")
    lines.append(f"Started:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 78)
    lines.append("")

    for i, r in enumerate(results, 1):
        status = "✓" if r["success"] else "✗"
        lines.append(f"{'─' * 78}")
        lines.append(f"QUERY #{i}  [{r['label']}]  {status}")
        lines.append(f"Query:    {r['query']}")
        lines.append(f"Elapsed:  {r['elapsed']:.1f}s")
        lines.append(f"Exit:     {r['returncode']}")
        lines.append(f"{'─' * 78}")
        lines.append("")
        lines.append(r["stdout"].strip())
        if r["stderr"].strip():
            lines.append("")
            lines.append("── stderr ──")
            lines.append(r["stderr"].strip())
        lines.append("")

    lines.append("=" * 78)
    lines.append("END OF LOG")
    lines.append("=" * 78)
    return "\n".join(lines)


def build_readme(results: list[dict], log_path: str) -> str:
    """Build the README.md content."""
    lines = []
    lines.append("# Multi-Agent DAG — Batch Run Results")
    lines.append("")
    lines.append(
        f"Batch executed on **{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}**"
    )
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| # | Label | Purpose | Status | Time |")
    lines.append("|---|-------|---------|--------|------|")

    for i, r in enumerate(results, 1):
        status = "✅" if r["success"] else "❌"
        elapsed_s = f"{r['elapsed']:.1f}s"
        lines.append(
            f"| {i} | `{r['label']}` | {r['purpose']} | {status} | {elapsed_s} |"
        )
    lines.append("")

    success_count = sum(1 for r in results if r["success"])
    lines.append(
        f"**{success_count}/{len(results)} queries succeeded.**"
    )
    lines.append("")

    lines.append("## Full Logs")
    lines.append("")
    lines.append(f"Full raw logs saved to: [`{log_path}`]({log_path})")
    lines.append("")

    for i, r in enumerate(results, 1):
        status = "✅" if r["success"] else "❌"
        lines.append("---")
        lines.append("")
        lines.append(f"### Query #{i}: {r['label']} {status}")
        lines.append("")
        lines.append(f"**Purpose:** {r['purpose']}")
        lines.append("")
        lines.append(f"**Query:** `{r['query']}`")
        lines.append("")
        lines.append(f"**Elapsed:** {r['elapsed']:.1f}s  |  **Exit code:** {r['returncode']}")
        lines.append("")
        lines.append("```")
        # truncate stdout to 500 lines to keep README manageable
        out_lines = r["stdout"].strip().splitlines()
        if len(out_lines) > 500:
            out_lines = out_lines[:500] + [
                f"... ({len(out_lines) - 500} more lines truncated)"
            ]
        lines.append("\n".join(out_lines))
        if r["stderr"].strip():
            lines.append("```")
            lines.append("")
            lines.append("**stderr:**")
            lines.append("```")
            lines.append(r["stderr"].strip())
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


# ── main ───────────────────────────────────────────────────────────────────

def main():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_filename = f"run_{timestamp}.log"
    log_path = LOGS_DIR / log_filename

    results = []
    for i, entry in enumerate(QUERIES):
        r = run_flow(entry["query"], entry["id"])
        r["purpose"] = entry["purpose"]
        results.append(r)
        # flush after each run so partial results survive crashes
        log_text = build_log_text(results)
        log_path.write_text(log_text)
        print(f"\n  ✓ Log updated: {log_path}")

    # Final write of log file
    log_text = build_log_text(results)
    log_path.write_text(log_text)

    # Write README.md
    readme_text = build_readme(results, str(log_path))
    README_PATH.write_text(readme_text)

    print(f"\n{'=' * 78}")
    print(f"ALL DONE. Results:")
    for r in results:
        icon = "✓" if r["success"] else "✗"
        print(f"  {icon} [{r['label']:22s}] {r['elapsed']:6.1f}s")
    print(f"\nLog file:  {log_path}")
    print(f"README.md: {README_PATH}")
    print(f"{'=' * 78}")


if __name__ == "__main__":
    main()