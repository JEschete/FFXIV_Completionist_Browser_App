"""Presentation helpers for import artifacts."""
from __future__ import annotations

import html
from typing import Any
from urllib.parse import quote


def render_unmatched_import_report(
    run_id: str,
    character_name: str,
    items: list[dict[str, Any]],
) -> str:
    rows: list[str] = []
    for item in items:
        bucket = html.escape(str(item.get("bucket") or "unknown"))
        label = html.escape(str(item.get("label") or ""))
        reason = html.escape(str(item.get("reason") or ""))
        rows.append(f"<tr><td>{bucket}</td><td>{label}</td><td>{reason}</td></tr>")

    body = "\n".join(rows) if rows else "<tr><td colspan='3'>No unmatched items.</td></tr>"
    escaped_character_name = html.escape(character_name)
    escaped_run_id = html.escape(run_id)
    return f"""
<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8' />
  <meta name='viewport' content='width=device-width, initial-scale=1.0' />
  <title>Unmatched Items - {escaped_character_name}</title>
  <style>
    body {{ font-family: Segoe UI, Arial, sans-serif; margin: 20px; background: #0f1218; color: #e7ecf5; }}
    a {{ color: #8ec2ff; }}
    table {{ border-collapse: collapse; width: 100%; max-width: 1100px; background: #171d28; }}
    th, td {{ border: 1px solid #2c3647; padding: 8px 10px; text-align: left; }}
    th {{ background: #202a3b; }}
    .meta {{ color: #a8b2c3; margin-bottom: 12px; }}
  </style>
</head>
<body>
  <h1>Unmatched Items</h1>
  <div class='meta'>Run ID: {escaped_run_id} | Character: {escaped_character_name} | Count: {len(rows)}</div>
  <p><a href='/characters/import-unmatched.json?run_id={quote(run_id)}'>Download JSON report</a></p>
  <table>
        <thead><tr><th>Category</th><th>Label</th><th>Reason</th></tr></thead>
    <tbody>{body}</tbody>
  </table>
</body>
</html>
"""
