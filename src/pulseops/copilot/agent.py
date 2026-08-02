"""the copilot itself: gemini, wired to the read-only tools and nothing else.

the model is the least important part of this file. it picks tools and writes
prose. everything that could actually hurt the warehouse was decided in
guard.py, and the model never gets a chance to argue with it: a refused tool
call returns the refusal as an ordinary tool result, the model reads it, and
carries on. no exception propagates, no retry loop, no way to escalate.

the system prompt tells it to cite sources and to say it does not know. that is
a preference, not a control. controls live in the guard.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .tools import CopilotTools, ToolResult

DEFAULT_MODEL = "gemini-2.5-pro"

# the model cannot see the warehouse, and a model that cannot see a schema will
# invent one. left to guess it asked for a table called `fact_orders`, which has
# never existed, and then reported that it could not answer the question. the
# fix is not a cleverer prompt, it is telling it what is actually there.
#
# the table names here are checked against the guard's allowlist by a test, so
# this cannot drift into describing tables the copilot is not allowed to read.
SCHEMA_NOTE = """\
Tables you can read, with their useful columns. Always fully qualify them as
`{project}.dataset.table`.

pulseops_mart.fct_order_line
    one row per line item on an order
    event_id, order_id, line_position, order_date, event_ts
    outlet_id, outlet_key, menu_item_id, menu_item_key, menu_item_name
    channel (dine_in|takeaway|delivery)
    payment_status (captured|pending|failed|refunded)
    payment_method (card|ewallet|cash|online_banking)
    qty, unit_price_myr, line_total_myr, captured_revenue_myr

pulseops_mart.dim_outlet
    outlet_key, outlet_id, outlet_name, city, state, opened_on, seats

pulseops_mart.dim_menu_item
    menu_item_key, menu_item_id, menu_item_name, category, list_price_myr,
    is_vegetarian, is_unknown_member

pulseops_mart.dim_date
    date_key, date_day, year, quarter, month, month_name, day_name, is_weekend

pulseops_mart.dq_warehouse_faults
    fault_type, events_affected, why_ingest_cannot_see_it

pulseops_quarantine.orders_quarantine
    message_id, event_id, schema_version, quarantined_ts
    violation_codes (repeated), violations, payload

pulseops_quarantine.replay_log
    event_id, replayed_at, status (repaired|unrepairable|still_invalid),
    rules_applied, reason

Notes that matter:
- captured_revenue_myr is zero for failed payments. line_total_myr is not.
  "revenue" means captured_revenue_myr unless someone says otherwise.
- one quarantined record can carry several violation codes, so counting codes
  is not counting records.
- there is no customer table, no weather data, and nothing about the future.
"""

SYSTEM_PROMPT = """\
You are the PulseOps data-reliability copilot. You help answer one kind of
question above all others: when a number looks wrong, is the business wrong or
is the pipeline wrong?

You have read-only access to a BigQuery warehouse through the tools provided.
You cannot write, update, delete or replay anything, and you should not offer
to. If a user asks you to change data, say plainly that you only read, and tell
them which command a human would run.

How to work:

- Prefer the named tools over writing SQL. Use run_sql only when no named tool
  answers the question.
- Before blaming the business for a drop in a number, check whether records
  were quarantined over the same period. A pipeline fault and a genuine decline
  look identical in a revenue chart and completely different in the quality
  tables.
- Always say which tables your answer came from. An unsourced answer is not
  usable during an incident.
- If the data does not support an answer, say so. Do not estimate, do not fill
  gaps, and never present a guess as a finding.
- Be brief. Lead with the answer, then the evidence.
"""

SYSTEM_PROMPT = SYSTEM_PROMPT + "\n" + SCHEMA_NOTE

TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "quarantine_summary",
        "description": (
            "Counts of records rejected by the data contract in the last N days, "
            "grouped by violation code. Use this to find out whether a data "
            "problem explains a change in a business number."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "How many days back to look."}
            },
        },
    },
    {
        "name": "warehouse_faults",
        "description": (
            "Fault classes the ingest-time contract cannot detect: duplicate "
            "deliveries, orphaned menu items, and late arrivals, with counts."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "replay_history",
        "description": (
            "What has been recovered from quarantine and what was refused as "
            "unrepairable, over the last N days."
        ),
        "parameters": {
            "type": "object",
            "properties": {"days": {"type": "integer"}},
        },
    },
    {
        "name": "revenue_by_outlet",
        "description": (
            "Captured revenue and order counts per outlet per day for the last "
            "N days. This is the business number, from the mart."
        ),
        "parameters": {
            "type": "object",
            "properties": {"days": {"type": "integer"}},
        },
    },
    {
        "name": "run_sql",
        "description": (
            "Run a read-only SELECT against the allowlisted mart and quality "
            "tables. Rejected if it writes, reads an unlisted table, or would "
            "scan too much data."
        ),
        "parameters": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    },
]


@dataclass
class Turn:
    """one exchange, kept so the eval suite can score how it got there."""

    question: str
    answer: str = ""
    tool_calls: list[str] = field(default_factory=list)
    tools_refused: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "tool_calls": self.tool_calls,
            "tools_refused": self.tools_refused,
            "sources": sorted(set(self.sources)),
            "error": self.error,
        }


class Copilot:
    """gemini plus the tool layer, with a bounded agent loop."""

    def __init__(
        self,
        project_id: str,
        tools: CopilotTools | None = None,
        model: str = DEFAULT_MODEL,
        # "global" rather than asia-southeast1 on purpose: the data lives in
        # Singapore, but gemini 2.5 pro is not served from that region and a
        # 404 at request time is a worse failure than a documented one here.
        # only the question and the tool results cross regions, never the tables.
        location: str = "global",
        client: Any | None = None,
        max_steps: int = 6,
    ) -> None:
        self.project_id = project_id
        self.model = model
        self.location = location
        self.tools = tools or CopilotTools(project_id=project_id)
        self.max_steps = max_steps
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            from google import genai

            self._client = genai.Client(
                vertexai=True, project=self.project_id, location=self.location
            )
        return self._client

    def _call_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        handler = {
            "quarantine_summary": lambda: self.tools.quarantine_summary(
                days=int(args.get("days", 30))
            ),
            "warehouse_faults": self.tools.warehouse_faults,
            "replay_history": lambda: self.tools.replay_history(
                days=int(args.get("days", 30))
            ),
            "revenue_by_outlet": lambda: self.tools.revenue_by_outlet(
                days=int(args.get("days", 30))
            ),
            "run_sql": lambda: self.tools.run_sql(str(args.get("sql", ""))),
        }.get(name)

        if handler is None:
            return ToolResult(ok=False, error=f"no such tool: {name}")
        return handler()

    def ask(self, question: str) -> Turn:
        """answer one question, calling tools until it has enough or runs out."""
        from google.genai import types

        turn = Turn(question=question)

        contents = [types.Content(role="user", parts=[types.Part(text=question)])]
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[types.Tool(function_declarations=TOOL_DECLARATIONS)],
            temperature=0.0,
        )

        for _ in range(self.max_steps):
            try:
                response = self.client.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
            except Exception as exc:  # pragma: no cover, network
                turn.error = f"model call failed: {exc}"
                return turn

            candidate = response.candidates[0] if response.candidates else None
            if candidate is None or not candidate.content:
                turn.error = "model returned nothing"
                return turn

            calls = [p.function_call for p in (candidate.content.parts or []) if p.function_call]

            if not calls:
                turn.answer = (response.text or "").strip()
                return turn

            contents.append(candidate.content)

            # gemini requires exactly one response part per call part, all in a
            # single message. replying to a two-call turn with two separate
            # messages is a 400, which is a fiddly thing to discover at runtime.
            response_parts = []

            for call in calls:
                args = dict(call.args or {})
                turn.tool_calls.append(call.name)

                result = self._call_tool(call.name, args)
                if not result.ok:
                    # the refusal goes back as data, not as an exception. the
                    # model gets to read why and try something legal instead.
                    turn.tools_refused.append(call.name)
                else:
                    turn.sources.extend(result.sources)

                response_parts.append(
                    types.Part.from_function_response(
                        name=call.name,
                        response={"result": json.loads(json.dumps(result.as_dict()))},
                    )
                )

            contents.append(types.Content(role="user", parts=response_parts))

        turn.error = f"gave up after {self.max_steps} steps"
        return turn
