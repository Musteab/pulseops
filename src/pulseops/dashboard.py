"""build a self-contained dashboard out of the warehouse.

one command, one html file, no server and no javascript. every chart is svg
generated here from real query results, which means the whole thing opens from
disk, survives being emailed, and can be diffed in a pull request.

it reads through the copilot's guarded tools rather than talking to bigquery
directly. that is not a shortcut, it is the point: the dashboard physically
cannot show you anything the agent is not allowed to see, so there is one
allowlist to reason about rather than two.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .copilot.tools import CopilotTools

# flat, bold, high contrast. white ground, one blue, one red, black type.
INK = "#101014"
PAPER = "#FFFFFF"
BLUE = "#1B6DF0"
RED = "#F03A2E"
MUTED = "#6B7280"
RULE = "#101014"
WASH = "#F2F4F7"


@dataclass
class Panel:
    """one query and what it turned into."""

    title: str
    rows: list[dict[str, Any]]
    sources: list[str]


def _fetch(tools: CopilotTools) -> dict[str, Panel]:
    """everything the page needs, in four queries."""
    project = tools.project_id

    daily = tools.run_sql(
        f"""
        select order_date,
               round(sum(captured_revenue_myr), 2) as revenue,
               count(distinct event_id) as orders
        from `{project}.pulseops_mart.fct_order_line`
        group by 1 order by 1
        """
    )
    outlets = tools.run_sql(
        f"""
        select outlet_id,
               round(sum(captured_revenue_myr), 2) as revenue,
               count(distinct event_id) as orders
        from `{project}.pulseops_mart.fct_order_line`
        group by 1 order by revenue desc
        """
    )
    quarantine = tools.quarantine_summary(days=3650)
    faults = tools.warehouse_faults()
    replay = tools.replay_history(days=3650)

    for name, result in (
        ("daily", daily), ("outlets", outlets), ("quarantine", quarantine),
        ("faults", faults), ("replay", replay),
    ):
        if not result.ok:
            raise RuntimeError(f"{name} query refused: {result.error}")

    return {
        "daily": Panel("revenue by day", daily.rows, daily.sources),
        "outlets": Panel("revenue by outlet", outlets.rows, outlets.sources),
        "quarantine": Panel("why records were rejected", quarantine.rows, quarantine.sources),
        "faults": Panel("what ingest could not see", faults.rows, faults.sources),
        "replay": Panel("quarantine replay", replay.rows, replay.sources),
    }


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _money(value: float) -> str:
    return f"{value:,.0f}"


def _bar_chart(rows: list[dict[str, Any]], label_key: str, value_key: str, colour: str) -> str:
    """horizontal bars. svg because a chart library would be three hundred
    kilobytes to draw eleven rectangles."""
    if not rows:
        return '<p class="empty">nothing to show</p>'

    biggest = max(_num(r[value_key]) for r in rows) or 1.0
    row_h, gap, label_w, value_w = 26, 8, 190, 78
    width = 720
    track = width - label_w - value_w
    height = len(rows) * (row_h + gap)

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" class="chart">']
    for i, row in enumerate(rows):
        y = i * (row_h + gap)
        value = _num(row[value_key])
        bar = max(2, int(track * (value / biggest)))
        label = html.escape(str(row[label_key]))
        parts.append(
            f'<text x="0" y="{y + 17}" class="bar-label">{label}</text>'
            f'<rect x="{label_w}" y="{y}" width="{track}" height="{row_h}" fill="{WASH}"/>'
            f'<rect x="{label_w}" y="{y}" width="{bar}" height="{row_h}" fill="{colour}"/>'
            f'<text x="{width}" y="{y + 17}" class="bar-value">{_money(value)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _line_chart(rows: list[dict[str, Any]], x_key: str, y_key: str) -> str:
    """revenue over time. a filled area under a hard 3px line, no smoothing,
    no gradient, no tooltips."""
    if len(rows) < 2:
        return '<p class="empty">not enough days yet</p>'

    width, height, pad_b, pad_t = 720, 220, 26, 12
    values = [_num(r[y_key]) for r in rows]
    top = max(values) * 1.12 or 1.0
    plot_h = height - pad_b - pad_t
    step = width / (len(rows) - 1)

    points = [
        (i * step, pad_t + plot_h - (v / top) * plot_h) for i, v in enumerate(values)
    ]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    area = f"0,{pad_t + plot_h} {line} {width},{pad_t + plot_h}"

    peak_i = values.index(max(values))
    peak_x, peak_y = points[peak_i]

    first, last = str(rows[0][x_key])[5:], str(rows[-1][x_key])[5:]

    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" class="chart">'
        f'<polygon points="{area}" fill="{BLUE}" opacity="0.12"/>'
        f'<polyline points="{line}" fill="none" stroke="{BLUE}" stroke-width="3"/>'
        f'<circle cx="{peak_x:.1f}" cy="{peak_y:.1f}" r="5" fill="{RED}"/>'
        f'<text x="{peak_x:.1f}" y="{peak_y - 12:.1f}" class="peak">'
        f"{_money(max(values))}</text>"
        f'<text x="0" y="{height - 6}" class="axis">{first}</text>'
        f'<text x="{width}" y="{height - 6}" class="axis end">{last}</text>'
        "</svg>"
    )


def _stat(value: str, label: str, tone: str = "") -> str:
    return (
        f'<div class="stat {tone}"><span class="stat-value">{value}</span>'
        f'<span class="stat-label">{label}</span></div>'
    )


def _short_ts(value: Any) -> str:
    """2026-08-01T21:46:51.458471+00:00 is a machine's idea of a timestamp."""
    text = str(value or "")
    if "T" in text:
        return text.split(".")[0].replace("T", " ")
    return text


def _table(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    if not rows:
        return '<p class="empty">nothing to show</p>'
    head = "".join(f"<th>{html.escape(title)}</th>" for _, title in columns)
    body = []
    for row in rows:
        cells = "".join(
            f"<td>{html.escape(str(row.get(key, '')))}</td>" for key, _ in columns
        )
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def build_html(panels: dict[str, Panel], project: str) -> str:
    daily = panels["daily"].rows
    outlets = panels["outlets"].rows
    quarantine = panels["quarantine"].rows
    faults = panels["faults"].rows
    replay = [
        dict(row, last_attempt=_short_ts(row.get("last_attempt")))
        for row in panels["replay"].rows
    ]

    total_revenue = sum(_num(r["revenue"]) for r in daily)
    total_orders = sum(_num(r["orders"]) for r in daily)
    quarantined = _num(quarantine[0]["total_quarantined_records"]) if quarantine else 0
    repaired = sum(_num(r["records"]) for r in replay if r.get("status") == "repaired")
    hidden = sum(_num(r["events_affected"]) for r in faults)

    sources = sorted({s for panel in panels.values() for s in panel.sources})
    built = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pulseops</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 0 24px 80px;
    background: {PAPER}; color: {INK};
    font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
    font-size: 15px; line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{ max-width: 780px; margin: 0 auto; }}

  header {{ padding: 56px 0 28px; border-bottom: 4px solid {RULE}; }}
  h1 {{
    margin: 0; font-size: 46px; font-weight: 800;
    letter-spacing: -0.035em; line-height: 1;
  }}
  .tag {{ margin: 10px 0 0; font-size: 16px; color: {MUTED}; }}
  .built {{ margin: 4px 0 0; font-size: 13px; color: {MUTED}; }}

  section {{ padding: 34px 0 0; }}
  h2 {{
    margin: 0 0 16px; font-size: 13px; font-weight: 800;
    letter-spacing: 0.1em; text-transform: uppercase;
  }}

  .stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 0; margin-top: 26px; }}
  .stat {{ border-left: 4px solid {INK}; padding: 2px 0 2px 12px; }}
  .stat.blue {{ border-left-color: {BLUE}; }}
  .stat.red  {{ border-left-color: {RED}; }}
  .stat-value {{ display: block; font-size: 30px; font-weight: 800; letter-spacing: -0.03em; }}
  .stat-label {{ display: block; font-size: 12px; color: {MUTED}; }}

  .chart {{ width: 100%; height: auto; display: block; overflow: visible; }}
  .bar-label {{ font-size: 12px; font-weight: 700; fill: {INK}; }}
  .bar-value {{ font-size: 12px; font-weight: 700; fill: {INK}; text-anchor: end; }}
  .axis {{ font-size: 11px; fill: {MUTED}; }}
  .axis.end {{ text-anchor: end; }}
  .peak {{ font-size: 12px; font-weight: 800; fill: {RED}; text-anchor: middle; }}

  table {{ width: 100%; border-collapse: collapse; }}
  th {{
    text-align: left; font-size: 11px; font-weight: 800; letter-spacing: 0.07em;
    text-transform: uppercase; color: {MUTED};
    border-bottom: 2px solid {INK}; padding: 0 8px 6px 0;
  }}
  td {{ padding: 8px 8px 8px 0; border-bottom: 1px solid #E5E7EB; font-size: 14px; }}
  td:first-child {{ font-weight: 700; }}

  .note {{
    margin-top: 14px; padding: 14px 16px;
    background: {WASH}; border-left: 4px solid {BLUE};
    font-size: 14px;
  }}
  .empty {{ color: {MUTED}; font-size: 14px; }}
  footer {{
    margin-top: 44px; padding-top: 16px; border-top: 2px solid {INK};
    font-size: 12px; color: {MUTED};
  }}
  code {{ font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }}

  @media (max-width: 640px) {{
    .stats {{ grid-template-columns: repeat(2, 1fr); gap: 18px 0; }}
    h1 {{ font-size: 34px; }}
  }}
</style>
</head>
<body>
<div class="wrap">

  <header>
    <h1>pulseops</h1>
    <p class="tag">is the business wrong, or is the pipeline wrong</p>
    <p class="built">built {built} from {html.escape(project)}</p>
  </header>

  <div class="stats">
    {_stat(_money(total_revenue), "captured revenue (myr)")}
    {_stat(_money(total_orders), "orders")}
    {_stat(_money(quarantined), "quarantined", "red")}
    {_stat(_money(repaired), "recovered by replay", "blue")}
  </div>

  <section>
    <h2>revenue by day</h2>
    {_line_chart(daily, "order_date", "revenue")}
  </section>

  <section>
    <h2>revenue by outlet</h2>
    {_bar_chart(outlets, "outlet_id", "revenue", BLUE)}
  </section>

  <section>
    <h2>why records were rejected</h2>
    {_bar_chart(quarantine, "violation_code", "violation_occurrences", RED)}
    <p class="note">
      one record can break several rules, so these bars add up to more than the
      {_money(quarantined)} records actually quarantined. counting violations is
      not counting records.
    </p>
  </section>

  <section>
    <h2>what ingest could not see</h2>
    {_table(faults, [
        ("fault_type", "fault"),
        ("events_affected", "events"),
        ("why_ingest_cannot_see_it", "why it needs the warehouse"),
    ])}
    <p class="note">
      {_money(hidden)} faults that no single record could reveal about itself.
      the contract catches everything visible in one record and hands these over.
    </p>
  </section>

  <section>
    <h2>quarantine replay</h2>
    {_table(replay, [
        ("status", "outcome"),
        ("records", "records"),
        ("last_attempt", "last attempt"),
    ])}
    <p class="note">
      repairs rename and reformat data that is already there. anything that would
      need a value inventing is refused on purpose, because a green dashboard
      built on made up numbers is worse than a red one.
    </p>
  </section>

  <footer>
    read through the copilot's allowlist, so this page cannot show anything the
    agent is not allowed to query.<br>
    sources: {", ".join(f"<code>{html.escape(s)}</code>" for s in sources)}
  </footer>

</div>
</body>
</html>
"""


def build_dashboard(project_id: str, out_path: str | Path) -> Path:
    tools = CopilotTools(project_id=project_id)
    panels = _fetch(tools)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(panels, project_id), encoding="utf-8")
    return out
