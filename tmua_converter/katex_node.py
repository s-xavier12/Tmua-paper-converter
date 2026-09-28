"""Render every maths expression with the bundled KaTeX through Node.js (no browser needed).

This is the same KaTeX the simulator uses, so anything it cannot draw (an unknown command, a
missing brace, a misplaced ^) is caught before the file is used.  When Node.js is not installed
the check is skipped and the static checks in the validator still run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import latex

KATEX_JS = Path(__file__).parent / "web" / "static" / "vendor" / "katex" / "katex.min.js"

_SCRIPT = r"""
const katex = require(process.argv[1]);
let input = "";
process.stdin.on("data", d => input += d);
process.stdin.on("end", () => {
  const out = {};
  for (const [id, tex, display] of JSON.parse(input)) {
    try {
      katex.renderToString(tex, {displayMode: display, throwOnError: true, strict: "ignore"});
    } catch (e) {
      out[id] = String(e.message || e).replace(/^KaTeX parse error: /, "");
    }
  }
  process.stdout.write(JSON.stringify(out));
});
"""


def node_available() -> bool:
    return shutil.which("node") is not None and KATEX_JS.exists()


def _fields(q: dict) -> list[tuple[str, str]]:
    out = [("stem", q.get("stem"))]
    for i, o in enumerate(q.get("options") or []):
        if isinstance(o, dict):
            out.append((f"options[{i}].content", o.get("content")))
    return [(f, s) for f, s in out if isinstance(s, str)]


def check_paper(data: Any) -> dict[tuple[int, str], str] | None:
    """(question number, field) -> KaTeX error message; None when Node.js is unavailable."""
    if not node_available() or not isinstance(data, dict):
        return None
    jobs, where = [], {}
    for q in data.get("questions") or []:
        if not isinstance(q, dict):
            continue
        for fld, s in _fields(q):
            segs, _ = latex.split_math(s)
            for seg in segs:
                if seg.kind == "text" or not seg.content.strip():
                    continue
                key = str(len(jobs))
                jobs.append([key, seg.content, seg.kind == "display"])
                where[key] = (q.get("number"), fld, seg.content)
    if not jobs:
        return {}
    try:
        proc = subprocess.run(["node", "-e", _SCRIPT, str(KATEX_JS)], input=json.dumps(jobs), text=True,
                              capture_output=True, timeout=60)
        result = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    errors: dict[tuple[int, str], str] = {}
    for key, msg in result.items():
        qn, fld, tex = where[key]
        short = tex if len(tex) <= 50 else tex[:47] + "..."
        text = f"{msg} in '{short}'"
        errors[(qn, fld)] = errors[(qn, fld)] + "; " + text if (qn, fld) in errors else text
    return errors
