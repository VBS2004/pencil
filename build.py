#!/usr/bin/env python3
"""Build a self-contained player.html (validator and sample lesson inlined).

    python3 build.py                      # uses samples/rainbow.json as the built-in sample
    python3 build.py --sample my.json --out dist/player.html
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def build(sample_path: Path, out_path: Path) -> None:
    template = (HERE / "player.template.html").read_text(encoding="utf-8")
    validator = (HERE / "validator.js").read_text(encoding="utf-8")
    sample = json.dumps(json.loads(sample_path.read_text(encoding="utf-8")), indent=2)
    for name, text in (("validator.js", validator), ("sample", sample)):
        if "</script" in text.lower():
            sys.exit(f"{name} contains a closing script tag and cannot be inlined safely")
    marker = '<script src="validator.js"></script>'
    if marker not in template or "__SAMPLE__" not in template:
        sys.exit("player.template.html is missing its placeholders")
    html = template.replace("__SAMPLE__", sample).replace(marker, "<script>\n" + validator + "\n</script>")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"wrote {out_path} ({len(html)} bytes)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", default=str(HERE / "samples" / "rainbow.json"))
    ap.add_argument("--out", default=str(HERE / "player.html"))
    a = ap.parse_args()
    build(Path(a.sample), Path(a.out))
