#!/usr/bin/env python3
"""Run the planner over a set of topics and record validity, repairs, tokens and time.

    python3 eval_planner.py --provider deepseek --runs 1 --out eval_results.json
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import planner

TOPICS = [
    "How tides work",
    "Photosynthesis",
    "How a bill becomes a law",
    "What is a prime number",
    "How vaccines train the immune system",
    "The water cycle",
    "How does a binary search work",
    "Why the seasons change",
]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--provider", choices=["anthropic", "deepseek"], default="deepseek")
    p.add_argument("--model", default=None)
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--topics", help="comma-separated topics to use instead of the built-in set")
    p.add_argument("--minutes", type=int, default=3)
    p.add_argument("--price-in", type=float, help="USD per million input tokens (cache miss)")
    p.add_argument("--price-out", type=float, help="USD per million output tokens")
    p.add_argument("--inr-per-usd", type=float)
    p.add_argument("--out", default="eval_results.json")
    p.add_argument("--lessons-dir", default="eval_lessons")
    args = p.parse_args()

    if args.provider == "deepseek":
        llm = planner.DeepSeekLLM(args.model or os.environ.get("LESSON_MODEL", "deepseek-flash"))
    else:
        llm = planner.AnthropicLLM(args.model or planner.DEFAULT_MODEL)

    Path(args.lessons_dir).mkdir(exist_ok=True)
    rows = []
    topics = [t.strip() for t in args.topics.split(",")] if args.topics else TOPICS
    for topic in topics:
        for run in range(args.runs):
            t0 = time.time()
            res = planner.generate_lesson(llm, topic, minutes=args.minutes)
            row = {
                "topic": topic,
                "run": run,
                "valid": res.lesson is not None and not res.issues,
                "repairs": res.repairs,
                "calls": res.usage.calls,
                "input_tokens": res.usage.input_tokens,
                "output_tokens": res.usage.output_tokens,
                "seconds": round(time.time() - t0, 1),
                "warnings": len(res.warnings),
                "autofixes": res.fixes,
                "first_try_codes": sorted({i["code"] for i in res.first_issues}),
                "issues": [f"{i['path']}: {i['message']}" for i in res.issues][:10],
            }
            if res.lesson is not None:
                slug = "".join(c if c.isalnum() else "_" for c in topic.lower())
                (Path(args.lessons_dir) / f"{slug}_{run}{'' if row['valid'] else '_INVALID'}.json").write_text(
                    json.dumps(res.lesson, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            rows.append(row)
            print(json.dumps({k: v for k, v in row.items() if k != "issues"}), flush=True)
            Path(args.out).write_text(json.dumps({"rows": rows}, indent=2), encoding="utf-8")

    n = len(rows)
    summary = {
        "n": n,
        "valid_rate": sum(r["valid"] for r in rows) / n,
        "first_try_valid_rate": sum(r["valid"] and r["repairs"] == 0 for r in rows) / n,
        "avg_repairs": sum(r["repairs"] for r in rows) / n,
        "avg_input_tokens": sum(r["input_tokens"] for r in rows) / n,
        "avg_output_tokens": sum(r["output_tokens"] for r in rows) / n,
        "avg_seconds": sum(r["seconds"] for r in rows) / n,
    }
    Path(args.out).write_text(json.dumps({"summary": summary, "rows": rows}, indent=2), encoding="utf-8")
    if args.price_in is not None and args.price_out is not None:
        usd = sum(r["input_tokens"] * args.price_in + r["output_tokens"] * args.price_out for r in rows) / 1e6 / n
        summary["avg_cost_usd"] = round(usd, 6)
        if args.inr_per_usd:
            summary["avg_cost_inr"] = round(usd * args.inr_per_usd, 4)
    Path(args.out).write_text(json.dumps({"summary": summary, "rows": rows}, indent=2), encoding="utf-8")
    print("SUMMARY " + json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
