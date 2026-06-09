#!/usr/bin/env python3
"""AST-based block mover for the app.py → routers refactor.

Moves named top-level definitions (functions / classes / simple assignments) VERBATIM out of
mgmt/app.py — including their decorators and contiguous leading comment block — into a target
module, then deletes them from app.py. Optionally rewrites ``@app.`` → ``@router.`` in the moved
text (for router modules). Reliable because it uses the AST to find exact line spans; verbatim
because it copies the source lines unchanged.

Usage:
  refactor_extract.py --dest mgmt/common.py --header HEADER.txt --names a,b,c [--router]
The header file is prepended to the dest (imports + e.g. `router = APIRouter(...)`); the moved
blocks are appended (joined by blank lines). app.py is rewritten in place with the blocks removed.
"""
import argparse
import ast
import sys

APP = "mgmt/app.py"  # default source


def find_spans(src, names):
    tree = ast.parse(src)
    lines = src.split("\n")
    spans = {}
    for node in tree.body:
        nm = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nm = node.name
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            nm = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            nm = node.target.id
        if nm not in names:
            continue
        start = node.lineno
        if getattr(node, "decorator_list", None):
            start = node.decorator_list[0].lineno
        # absorb a contiguous block of leading comments / blank lines directly above
        i = start - 2  # 0-based index of the line above `start`
        while i >= 0 and (lines[i].lstrip().startswith("#") or lines[i].strip() == ""):
            i -= 1
        start = i + 2
        spans[nm] = (start, node.end_lineno)
    return spans, lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", required=True)
    ap.add_argument("--header", required=True)
    ap.add_argument("--names", required=True)
    ap.add_argument("--router", action="store_true")
    ap.add_argument("--src", default=APP)
    a = ap.parse_args()
    names = [n.strip() for n in a.names.split(",") if n.strip()]

    SRC = a.src
    src = open(SRC).read()
    spans, lines = find_spans(src, names)
    missing = [n for n in names if n not in spans]
    if missing:
        print(f"ERROR: names not found as top-level defs: {missing}", file=sys.stderr)
        sys.exit(2)

    # extract verbatim (preserve given order via line position)
    ordered = sorted(spans.items(), key=lambda kv: kv[1][0])
    blocks = []
    for nm, (s, e) in ordered:
        text = "\n".join(lines[s - 1:e])
        if a.router:
            text = text.replace("@app.", "@router.")
        blocks.append(text)

    header = open(a.header).read().rstrip("\n") + "\n"
    open(a.dest, "w").write(header + "\n\n" + "\n\n\n".join(blocks) + "\n")

    # remove from app.py (reverse line order so offsets stay valid)
    for nm, (s, e) in sorted(spans.items(), key=lambda kv: kv[1][0], reverse=True):
        del lines[s - 1:e]
    open(SRC, "w").write("\n".join(lines))

    print(f"moved {len(names)} blocks → {a.dest}: {[n for n,_ in ordered]}")


if __name__ == "__main__":
    main()
