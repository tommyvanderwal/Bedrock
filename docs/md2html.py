#!/usr/bin/env python3
"""Convert markdown files to styled HTML pages."""
import markdown
import sys
import os
import glob

TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TITLE_PLACEHOLDER</title>
<style>
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    max-width: 960px; margin: 40px auto; padding: 0 20px;
    color: #24292f; background: #fff; line-height: 1.6;
  }
  h1 { border-bottom: 1px solid #d0d7de; padding-bottom: 8px; color: #1f2328; }
  h2 { border-bottom: 1px solid #d0d7de; padding-bottom: 6px; margin-top: 32px; }
  h3 { margin-top: 24px; }
  pre {
    background: #161b22; color: #e6edf3; padding: 16px; border-radius: 6px;
    overflow-x: auto; font-size: 13px; line-height: 1.5;
  }
  code {
    background: #eff1f3; padding: 2px 6px; border-radius: 3px;
    font-size: 85%;
  }
  pre code { background: none; padding: 0; font-size: 100%; }
  table { border-collapse: collapse; width: 100%; margin: 16px 0; }
  th, td { border: 1px solid #d0d7de; padding: 8px 12px; text-align: left; }
  th { background: #f6f8fa; }
  a { color: #0969da; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .nav { background: #f6f8fa; padding: 12px 20px; border-radius: 6px; margin-bottom: 24px; }
  .nav a { margin-right: 20px; font-weight: 500; }
</style>
</head>
<body>
<div class="nav">
  <a href="/docs/">Index</a>
  <a href="/docs/01-storage-stack.html">Storage Stack</a>
  <a href="/docs/02-drbd-replication.html">DRBD Replication</a>
  <a href="/docs/03-witness-and-orchestrator.html">Witness &amp; Orchestrator</a>
</div>
CONTENT_PLACEHOLDER
</body>
</html>"""


def convert(md_path, html_path):
    with open(md_path) as f:
        md_text = f.read()
    extensions = ["fenced_code", "tables", "toc"]
    html = markdown.markdown(md_text, extensions=extensions)
    title = md_text.split("\n")[0].replace("#", "").strip()
    page = TEMPLATE.replace("TITLE_PLACEHOLDER", title).replace(
        "CONTENT_PLACEHOLDER", html
    )
    with open(html_path, "w") as f:
        f.write(page)


if __name__ == "__main__":
    src_dir = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "/var/www/html/docs"
    os.makedirs(out_dir, exist_ok=True)

    files = sorted(glob.glob(os.path.join(src_dir, "*.md")))
    index_links = []
    for md_file in files:
        basename = os.path.splitext(os.path.basename(md_file))[0]
        html_file = os.path.join(out_dir, basename + ".html")
        convert(md_file, html_file)
        with open(md_file) as f:
            title = f.readline().replace("#", "").strip()
        index_links.append(f'<li><a href="{basename}.html">{title}</a></li>')
        print(f"  {md_file} -> {html_file}")

    # Create index page
    index_html = TEMPLATE.replace("TITLE_PLACEHOLDER", "Bedrock Documentation").replace(
        "CONTENT_PLACEHOLDER",
        "<h1>Bedrock Documentation</h1><ul>" + "".join(index_links) + "</ul>",
    )
    with open(os.path.join(out_dir, "index.html"), "w") as f:
        f.write(index_html)
    print("  index.html created")
