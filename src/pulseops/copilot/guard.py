"""the thing standing between a language model and your warehouse.

read this first, because it is the honest bit: **this guard is the second line
of defence, not the first**. the first is IAM. the copilot is meant to run as a
service account with roles/bigquery.dataViewer and nothing else, so that even a
perfectly crafted bypass of everything below still cannot write a row. a regex
is not a sql parser and anyone claiming otherwise is selling something.

so why bother? because the guard fails loudly and specifically at the moment
the model asks for something it should not, which gives you an auditable
refusal instead of a silent permission error three layers down. defence in
depth, with the cheap layer doing the explaining and the expensive layer doing
the actual enforcing.

what it enforces:

  one statement, no stacking          "select 1; drop table x" is rejected
  select or with only                 anything that mutates is rejected
  no scripting, no procedures         declare, begin, call, execute immediate
  every table on the allowlist        including through a wildcard or an alias
  a bounded result                    a limit is added when one is missing
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# marts and the quality views. deliberately no raw and no staging: the copilot
# answers questions, and a question that genuinely needs raw is a question for a
# human with a reason.
DEFAULT_ALLOWED_TABLES: frozenset[str] = frozenset(
    {
        "pulseops_mart.fct_order_line",
        "pulseops_mart.dim_outlet",
        "pulseops_mart.dim_menu_item",
        "pulseops_mart.dim_date",
        "pulseops_mart.dq_warehouse_faults",
        "pulseops_quarantine.orders_quarantine",
        "pulseops_quarantine.replay_log",
    }
)

MAX_LIMIT = 1000

# every way bigquery lets you change something, plus the scripting constructs
# that could smuggle one in.
#
# "replace" is deliberately absent. the dangerous form is "create or replace",
# which "create" already catches, whereas blocking the bare word would also
# reject REPLACE() and every legitimate string tidy-up a analyst might write.
# a guard that cries wolf gets switched off.
_FORBIDDEN = (
    "insert", "update", "delete", "merge", "truncate", "drop", "create",
    "alter", "grant", "revoke", "export", "load", "call",
    "declare", "begin", "commit", "rollback", "execute", "assert",
)

_COMMENT_LINE = re.compile(r"--[^\n]*")
_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING_LITERAL = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")

# from/join followed by a table reference, backticked or bare
_TABLE_REF = re.compile(
    r"\b(?:from|join)\s+`?([A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_*\-]+){1,2})`?",
    re.IGNORECASE,
)
_HAS_LIMIT = re.compile(r"\blimit\s+\d+\s*$", re.IGNORECASE)


class SqlGuardError(Exception):
    """raised when a query is refused. the message is shown to the model."""


@dataclass(frozen=True)
class GuardResult:
    sql: str
    tables: tuple[str, ...]
    limit_added: bool


def _strip_noise(sql: str) -> str:
    """remove comments and string contents before looking for keywords.

    string literals get blanked rather than deleted so that a table name living
    inside a quoted string cannot be mistaken for a real reference, and so that
    'delete' appearing in a menu item name does not trip the keyword check.
    """
    without_comments = _COMMENT_BLOCK.sub(" ", _COMMENT_LINE.sub(" ", sql))
    return _STRING_LITERAL.sub("''", without_comments)


def _normalise_table(reference: str) -> str:
    """reduce project.dataset.table to dataset.table for allowlist comparison."""
    parts = reference.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else reference


def check_sql(
    sql: str,
    allowed_tables: frozenset[str] = DEFAULT_ALLOWED_TABLES,
    max_limit: int = MAX_LIMIT,
) -> GuardResult:
    """approve a query or refuse it with a reason the model can act on."""
    if not sql or not sql.strip():
        raise SqlGuardError("empty query")

    # two versions from here on, and keeping them straight matters.
    #
    # `cleaned` has comments removed and string literals blanked, and exists
    # only so the checks below cannot be fooled by a keyword hiding in a
    # comment or a table name inside a quoted string. it is NOT runnable:
    # blanking the literals changes what the query means.
    #
    # `original` is what actually gets executed. an earlier version of this
    # function returned `cleaned` by mistake, so "where status = 'failed'" ran
    # as "where status = ''" and every such query came back empty. it never
    # errored, it just quietly answered the wrong question.
    original = sql.strip().rstrip(";").strip()
    cleaned = _strip_noise(sql).strip().rstrip(";").strip()

    if not cleaned:
        raise SqlGuardError("query is only comments")

    # stacking check runs on the comment-stripped text, so a semicolon hidden
    # behind "--" cannot smuggle a second statement past it
    if ";" in cleaned:
        raise SqlGuardError("only one statement allowed, found a semicolon mid-query")

    lowered = " ".join(cleaned.lower().split())

    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise SqlGuardError("only select and with queries are allowed, this is read-only")

    for keyword in _FORBIDDEN:
        if re.search(rf"\b{keyword}\b", lowered):
            raise SqlGuardError(
                f"'{keyword}' is not permitted. this tool can read data and nothing else, "
                "a human has to make changes"
            )

    references = _TABLE_REF.findall(cleaned)
    if not references:
        raise SqlGuardError("no table found, the query must read from an allowlisted table")

    tables: list[str] = []
    for reference in references:
        if "*" in reference:
            raise SqlGuardError(f"wildcard table access is not allowed: {reference}")
        normalised = _normalise_table(reference)
        if normalised not in allowed_tables:
            raise SqlGuardError(
                f"table {normalised} is not on the allowlist. readable tables are: "
                + ", ".join(sorted(allowed_tables))
            )
        tables.append(normalised)

    guarded = original
    limit_added = False
    if not _HAS_LIMIT.search(lowered):
        guarded = f"{original}\nlimit {max_limit}"
        limit_added = True

    return GuardResult(sql=guarded, tables=tuple(dict.fromkeys(tables)), limit_added=limit_added)
