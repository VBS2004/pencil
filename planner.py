#!/usr/bin/env python3
"""Generate a hand-drawn lesson (JSON) for any topic, validate it, and repair it.

    export ANTHROPIC_API_KEY=...
    python3 planner.py --topic "How a rainbow forms" --out lesson.json
    python3 planner.py --topic - < topic.txt --minutes 3 --level beginner
    python3 planner.py --topic "Photosynthesis" --price-in 1.0 --price-out 5.0 --inr-per-usd 88

The lesson is checked by validator.js (the same code the player uses). Any issues are sent back
to the model for a bounded number of repair rounds. Token usage and an optional cost estimate
are printed at the end; prices are inputs (USD per million tokens), never hardcoded.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

HERE = Path(__file__).resolve().parent
VALIDATOR = HERE / "validator.js"
DEFAULT_MODEL = os.environ.get("LESSON_MODEL", "claude-haiku-5-5")

SYSTEM_PROMPT = """You write short, narrated whiteboard lessons as strict JSON. Output ONE minified JSON object and nothing else (no markdown fences, no commentary).

SCHEMA
{"version":1,
 "meta":{"title":str,"subject":str,"language":str,"mood":str},
 "canvas":{"logicalWidth":1000,"theme":"light"},
 "beats":[{"id":"b1","narration":str,"ops":[OP,...]}, ...],
 "checks":[{"question":str,"options":[str,...],"answer":int,"explanation":str}]}

OP is one of (every op needs a unique string "id"):
 {"id","type":"heading","text","level":1|2|3}
 {"id","type":"paragraph","text"}
 {"id","type":"callout","text","tone":"info"|"tip"|"warn"|"key"}
 {"id","type":"definition","term","definition"}
 {"id","type":"table","columns":[1-5 str],"rows":[1-8 arrays, each exactly as long as columns]}
 {"id","type":"figure","frame":{"width":100-1000,"height":60-700},"parts":[PART,...]}
PART = {"name":unique str,"cue":int,"marks":[MARK,...]}
 cue = the 0-based index of the word in THIS beat's narration at which the part is drawn.
 It must be less than the number of words in that narration.
MARK is one of (all coordinates are in the figure frame; every mark must lie fully inside it):
 {"kind":"circle","cx","cy","r"}  {"kind":"ellipse","cx","cy","rx","ry"}
 {"kind":"rect","x","y","w","h"}  {"kind":"line","x1","y1","x2","y2"}  (plain segment: axis, ground, divider, or a thin leader line from a label to an object)
 {"kind":"arrow","x1","y1","x2","y2"}  (from tail (x1,y1) to tip (x2,y2); the arrowhead is drawn FOR you, never draw heads by hand)
 {"kind":"path","d"}  -> ABSOLUTE commands only (M L H V C S Q T Z, uppercase). No relative commands, no arcs.
 {"kind":"label","x","y","text","size":int,"align":"start"|"middle"|"end"}
 Optional on non-label marks: "color","fill","width","dashed". color is one of ink,blue,red,green,orange,purple,gray.
 fill is a #rrggbb hex string. A label is about size*0.55 pixels wide per character: keep labels from overlapping each other.

RULES
- 4 to 8 beats. Each narration is 20 to 70 words, spoken aloud, plain and concrete. Start with a hook, end with a one-line recap.
- At least two figures. Draw simple pictures from basic shapes; cue each part to the narrated word that introduces it.
- FIGURE CRAFT. Before writing a figure, decide what it must show, then lay it out like a diagram on a grid:
  * 3 to 6 parts, each part one recognisable object or idea. Typical frame: 800x400. Keep a 30px margin inside the frame.
  * Draw each object so it looks like the thing it names (a sun = circle plus short rays, a leaf = closed curved path, a hill = path). Never use a shape for something it does not resemble.
  * Put every label in empty space next to its object, at least 20px away from any line or shape, never on top of them. Use a thin line from label to object if it must sit further away.
  * ARROWS: use the "arrow" mark. Its tail and its tip must each lie on the edge of an object, or right next to a label (within 10px). Compute the endpoints from the objects' coordinates (e.g. a circle at cx,cy,r: the point on its edge facing the other object is cx +/- r). An arrow whose end touches nothing is an error.
  * LEADER LINES: a "line" from a label to the object it names must start right at the label (within 10px) and end on the object's edge or inside it. Never draw a line that starts in empty space.
  * Every label must be within about 40px of the thing it names, and in the same part as that thing.
  * Spread objects out: no two objects closer than 40px unless one is meant to be inside the other. Order parts left to right or top to bottom in the order they are narrated.
- Use one worked example. Use at most one table.
- Add 2 or 3 "checks" (multiple choice) that test understanding, with a valid answer index.
- Write everything in the requested language. Keep the JSON compact.
"""


class LLM(Protocol):
    def complete(self, system: str, messages: list[dict], max_tokens: int) -> tuple[str, dict]:
        """Return (text, usage) where usage has input_tokens and output_tokens."""


class AnthropicLLM:
    def __init__(self, model: str):
        try:
            import anthropic  # imported lazily so tests and --dry-run need no SDK
        except ImportError as exc:  # pragma: no cover
            raise SystemExit("Install the SDK first: pip install anthropic") from exc
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("Set ANTHROPIC_API_KEY (or use --dry-run to see the prompt).")
        self.client = anthropic.Anthropic()
        self.model = model

    def complete(self, system, messages, max_tokens):
        resp = self.client.messages.create(
            model=self.model, max_tokens=max_tokens, system=system, messages=messages
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return text, {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}


class DeepSeekLLM:
    """DeepSeek via its OpenAI-compatible chat completions endpoint (stdlib only)."""

    def __init__(self, model: str, base_url: str | None = None):
        self.key = os.environ.get("DEEPSEEK_API_KEY")
        if not self.key:
            raise SystemExit("Set DEEPSEEK_API_KEY (or use --dry-run to see the prompt).")
        self.model = model
        self.url = (base_url or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/") + "/chat/completions"

    def complete(self, system, messages, max_tokens):
        import urllib.error
        import urllib.request

        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, *messages],
            "response_format": {"type": "json_object"},
            # Thinking is on by default for deepseek-flash; it only adds output tokens here.
            "thinking": {"type": os.environ.get("DEEPSEEK_THINKING", "disabled")},
        }
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"DeepSeek API error {exc.code}: {exc.read().decode('utf-8', 'replace')[:500]}") from exc
        usage = data.get("usage", {})
        return data["choices"][0]["message"]["content"] or "", {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        }


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, u: dict) -> None:
        self.calls += 1
        self.input_tokens += int(u.get("input_tokens", 0))
        self.output_tokens += int(u.get("output_tokens", 0))


@dataclass
class Result:
    lesson: dict | None
    issues: list[dict]
    usage: Usage = field(default_factory=Usage)
    repairs: int = 0
    first_issues: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)  # soft issues accepted at the end
    fixes: int = 0  # layout changes made by the automatic fixer (arrow snaps, label moves)


def build_user_prompt(topic: str, language: str, minutes: int, level: str, research: str) -> str:
    parts = [
        f"Topic: {topic.strip()}",
        f"Language: {language}",
        f"Learner level: {level}",
        f"Target length: about {minutes} minutes when narrated (roughly {int(minutes * 150)} spoken words in total).",
    ]
    if research.strip():
        parts.append("Reference notes (use for accuracy, do not copy):\n" + research.strip()[:6000])
    return "\n".join(parts)


BRACKET_HINT = (
    "Your JSON was missing closing brackets, so they were appended at the very end and the structure may be wrong "
    "(typically later beats ended up nested inside an earlier beat's ops array). Close every op object, every "
    "ops array and every beat object before the next beat starts."
)

# Cosmetic geometry problems: the model gets repair rounds for them, but a lesson that only has these
# left at the end is accepted with warnings instead of being thrown away.
SOFT_CODES = {"label_on_mark", "label_overlap", "dangling_line", "orphan_label"}


def close_open_brackets(s: str) -> str:
    """Append the closing brackets a reply left open (a common model slip on long JSON).

    Walks the text ignoring string contents; if the reply ends inside open objects/arrays, the
    missing closers are appended. The validator still checks the result, so a wrong guess is caught.
    """
    stack: list[str] = []
    in_str = esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()
    return s + "".join(reversed(stack)) if stack and not in_str else s


def extract_json(text: str, notes: list[str] | None = None):
    """Pull a JSON object out of a model reply, tolerating fences and stray prose.

    If missing closing brackets had to be appended, a note is added to `notes` (when given).
    """
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.IGNORECASE)
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object found in the reply")
    body = s[start : end + 1]
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        fixed = close_open_brackets(body)
        if fixed != body:
            try:
                obj = json.loads(fixed)
                if notes is not None:
                    notes.append(BRACKET_HINT)
                return obj
            except json.JSONDecodeError:
                pass
        lo, hi = max(0, exc.pos - 60), min(len(body), exc.pos + 60)
        raise ValueError(
            f"{exc.msg} at char {exc.pos}; around it: ...{body[lo:exc.pos]}<<HERE>>{body[exc.pos:hi]}..."
        ) from exc


def validate_with_node(lesson) -> list[dict]:
    """Run validator.js on the lesson; returns its issue list."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(lesson, fh)
        path = fh.name
    try:
        proc = subprocess.run(
            ["node", str(VALIDATOR), path], capture_output=True, text=True, timeout=30
        )
    except FileNotFoundError as exc:
        raise SystemExit("Node.js is required for validation (validator.js).") from exc
    finally:
        os.unlink(path)
    try:
        return json.loads(proc.stdout)["issues"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise RuntimeError(f"validator failed: {proc.stderr or proc.stdout}") from exc


def hoist_nested_beats(lesson: dict) -> int:
    """Move beats that the model nested inside a previous beat's ops (a forgotten `]`) back into `beats`.

    A beat-shaped item (has `narration` and `ops`, no `type`) found inside ops is lifted out and placed right
    after the beat it was found in, keeping order. Returns how many were moved.
    """
    beats = lesson.get("beats") if isinstance(lesson, dict) else None
    if not isinstance(beats, list):
        return 0
    moved, i = 0, 0
    while i < len(beats):
        beat = beats[i]
        ops = beat.get("ops") if isinstance(beat, dict) else None
        if isinstance(ops, list):
            at = next((k for k, o in enumerate(ops) if isinstance(o, dict) and "type" not in o and "narration" in o and "ops" in o), None)
            if at is not None:
                lifted = ops[at:]  # the beat and anything after it were swallowed too
                del ops[at:]
                beats[i + 1 : i + 1] = [o for o in lifted if isinstance(o, dict)]
                moved += len(lifted)
        i += 1
    return moved


def autofix_with_node(lesson) -> tuple[dict, list[str]]:
    """Run validator.js --fix: snap loose arrow ends, move labels off lines. Returns (lesson, changes)."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(lesson, fh)
        path = fh.name
    try:
        proc = subprocess.run(["node", str(VALIDATOR), "--fix", path], capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise SystemExit("Node.js is required for validation (validator.js).") from exc
    finally:
        os.unlink(path)
    try:
        out = json.loads(proc.stdout)
        return out["lesson"], out["changes"]
    except (json.JSONDecodeError, KeyError):
        return lesson, []  # never lose a lesson because the fixer failed; the validator still checks it


def format_issues(issues: list[dict], limit: int = 30) -> str:
    lines = [f"- {i['path']}: {i['message']}" for i in issues[:limit]]
    if len(issues) > limit:
        lines.append(f"- ...and {len(issues) - limit} more")
    return "\n".join(lines)


def generate_lesson(
    llm: LLM,
    topic: str,
    language: str = "en",
    minutes: int = 4,
    level: str = "beginner",
    research: str = "",
    max_repairs: int = 2,
    max_tokens: int = 6000,
    validate: Callable[[object], list[dict]] = validate_with_node,
    autofix: Callable[[object], tuple[dict, list[str]]] | None = autofix_with_node,
) -> Result:
    result = Result(lesson=None, issues=[])
    messages = [{"role": "user", "content": build_user_prompt(topic, language, minutes, level, research)}]

    for attempt in range(max_repairs + 1):
        text, usage = llm.complete(SYSTEM_PROMPT, messages, max_tokens)
        result.usage.add(usage)
        notes: list[str] = []
        try:
            lesson = extract_json(text, notes)
            if autofix is not None and isinstance(lesson, dict):
                result.fixes += hoist_nested_beats(lesson)
                lesson, changes = autofix(lesson)
                result.fixes += len(changes)
            issues = validate(lesson)
        except (ValueError, json.JSONDecodeError) as exc:
            lesson, issues = None, [{"path": "$", "code": "invalid_json", "message": f"reply was not valid JSON: {exc}. Return the complete lesson again as ONE syntactically valid JSON object"}]
        result.lesson, result.issues, result.repairs = lesson, issues, attempt
        if attempt == 0:
            result.first_issues = issues
        if not issues:
            return result
        if attempt == max_repairs:
            if lesson is not None and all(i["code"] in SOFT_CODES for i in issues):
                result.warnings, result.issues = issues, []
            break
        messages += [
            {"role": "assistant", "content": text},
            {
                "role": "user",
                "content": "The lesson failed validation. Fix every issue below and return the complete corrected "
                "JSON object only, changing as little else as possible.\n"
                + ("".join(n + "\n" for n in notes))
                + format_issues(issues),
            },
        ]
    return result


def cost_report(usage: Usage, price_in: float | None, price_out: float | None, inr_per_usd: float | None) -> str:
    line = f"calls={usage.calls} input_tokens={usage.input_tokens} output_tokens={usage.output_tokens}"
    if price_in is None or price_out is None:
        return line + "  (pass --price-in and --price-out, USD per million tokens, for a cost estimate)"
    usd = usage.input_tokens / 1e6 * price_in + usage.output_tokens / 1e6 * price_out
    line += f"  cost=${usd:.5f}"
    if inr_per_usd:
        line += f" = INR {usd * inr_per_usd:.3f}"
    return line


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--topic", required=True, help='topic text, or "-" to read it from stdin')
    p.add_argument("--language", default="en")
    p.add_argument("--minutes", type=int, default=4)
    p.add_argument("--level", default="beginner")
    p.add_argument("--research-file", help="optional text file with reference notes")
    p.add_argument("--provider", choices=["anthropic", "deepseek"], default=os.environ.get("LESSON_PROVIDER", "deepseek"))
    p.add_argument("--model", default=None, help="model id (env LESSON_MODEL; defaults: deepseek-flash / claude-haiku-5-5)")
    p.add_argument("--max-repairs", type=int, default=2)
    p.add_argument("--max-tokens", type=int, default=6000)
    p.add_argument("--out", default="lesson.json")
    p.add_argument("--price-in", type=float, help="USD per million input tokens")
    p.add_argument("--price-out", type=float, help="USD per million output tokens")
    p.add_argument("--inr-per-usd", type=float, help="exchange rate for an INR estimate")
    p.add_argument("--dry-run", action="store_true", help="print the prompts and exit")
    args = p.parse_args(argv)
    if args.minutes < 1 or args.minutes > 20:
        p.error("--minutes must be 1..20")
    if args.max_repairs < 0 or args.max_repairs > 5:
        p.error("--max-repairs must be 0..5")
    return args


def main(argv=None, llm: LLM | None = None) -> int:
    args = parse_args(argv)
    topic = sys.stdin.read() if args.topic == "-" else args.topic
    if not topic.strip():
        print("Topic is empty.", file=sys.stderr)
        return 2
    research = Path(args.research_file).read_text(encoding="utf-8") if args.research_file else ""
    if args.dry_run:
        print("--- SYSTEM ---\n" + SYSTEM_PROMPT)
        print("--- USER ---\n" + build_user_prompt(topic, args.language, args.minutes, args.level, research))
        return 0
    if llm is None:
        if args.provider == "deepseek":
            llm = DeepSeekLLM(args.model or os.environ.get("LESSON_MODEL", "deepseek-flash"))
        else:
            llm = AnthropicLLM(args.model or DEFAULT_MODEL)
    res = generate_lesson(
        llm, topic, args.language, args.minutes, args.level, research, args.max_repairs, args.max_tokens
    )
    print(cost_report(res.usage, args.price_in, args.price_out, args.inr_per_usd), file=sys.stderr)
    if res.lesson is None or res.issues:
        print(f"Lesson still has {len(res.issues)} issue(s) after {res.repairs} repair round(s):", file=sys.stderr)
        print(format_issues(res.issues), file=sys.stderr)
        if res.lesson is not None:
            Path(args.out).write_text(json.dumps(res.lesson, indent=2), encoding="utf-8")
            print(f"Wrote the best attempt to {args.out}", file=sys.stderr)
        return 1
    Path(args.out).write_text(json.dumps(res.lesson, indent=2, ensure_ascii=False), encoding="utf-8")
    if res.warnings:
        print(f"Accepted with {len(res.warnings)} cosmetic warning(s):", file=sys.stderr)
        print(format_issues(res.warnings), file=sys.stderr)
    print(f"Wrote {args.out} (valid, {res.repairs} repair round(s))", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
