"""adversarial tests for the sql guard.

these are written as attacks rather than as unit tests, because that is what
they are defending against. if any of the refusal tests below starts passing a
query through, the copilot has gained the ability to change the warehouse and
the "blocks 100 percent of unsafe requests" claim becomes a lie.
"""

from __future__ import annotations

import pytest

from pulseops.copilot.guard import MAX_LIMIT, SqlGuardError, check_sql

MART = "pulseops_mart.fct_order_line"


# ---------------------------------------------------------------------------
# things that must be refused
# ---------------------------------------------------------------------------

WRITES = [
    f"delete from {MART} where 1=1",
    f"update {MART} set qty = 0",
    f"insert into {MART} (qty) values (1)",
    f"truncate table {MART}",
    f"drop table {MART}",
    f"alter table {MART} drop column qty",
    f"create or replace table {MART} as select 1",
    f"merge {MART} t using {MART} s on t.event_id = s.event_id"
    " when matched then delete",
]


@pytest.mark.parametrize("sql", WRITES)
def test_every_form_of_write_is_refused(sql):
    with pytest.raises(SqlGuardError):
        check_sql(sql)


def test_statement_stacking_is_refused():
    with pytest.raises(SqlGuardError, match="one statement"):
        check_sql(f"select 1 from {MART}; drop table {MART}")


def test_semicolon_hidden_behind_a_line_comment_is_still_caught():
    """the classic. strip comments first, then look for stacking."""
    with pytest.raises(SqlGuardError):
        check_sql(f"select 1 from {MART} -- harmless\n; drop table {MART}")


def test_write_hidden_in_a_block_comment_does_not_trip_the_guard():
    """the inverse case: a commented-out delete is inert and must not be
    refused, otherwise the guard is unusable on real annotated sql."""
    result = check_sql(f"/* we used to delete here */ select event_id from {MART}")
    assert "select" in result.sql.lower()


# these two are refused by whichever check trips first, which is the statement
# and prefix checks rather than the keyword list. asserting on the exact message
# would be testing the order of the guard's internals rather than its behaviour,
# so these only assert that nothing gets through.


def test_scripting_is_refused():
    with pytest.raises(SqlGuardError):
        check_sql(f"declare x int64; select 1 from {MART}")


def test_declare_without_stacking_is_still_refused():
    with pytest.raises(SqlGuardError):
        check_sql(f"select 1 from {MART} where declare = 1")


def test_execute_immediate_is_refused():
    with pytest.raises(SqlGuardError):
        check_sql(f"execute immediate 'drop table {MART}'")


def test_case_and_whitespace_do_not_help():
    with pytest.raises(SqlGuardError):
        check_sql(f"DeLeTe\n\n  FROM\t{MART}")


def test_tables_outside_the_allowlist_are_refused():
    with pytest.raises(SqlGuardError, match="allowlist"):
        check_sql("select * from pulseops_raw.orders_raw")


def test_information_schema_is_refused():
    with pytest.raises(SqlGuardError, match="allowlist"):
        check_sql("select * from pulseops-muste.pulseops_mart.INFORMATION_SCHEMA.TABLES")


def test_wildcard_tables_are_refused():
    with pytest.raises(SqlGuardError, match="wildcard"):
        check_sql("select * from pulseops_mart.fct_*")


def test_a_join_to_a_forbidden_table_is_caught():
    """the allowlist has to hold for every reference, not just the first."""
    with pytest.raises(SqlGuardError, match="allowlist"):
        check_sql(
            f"select o.event_id from {MART} o "
            "join pulseops_raw.orders_raw r on o.event_id = r.event_id"
        )


def test_a_query_reading_nothing_is_refused():
    with pytest.raises(SqlGuardError, match="no table"):
        check_sql("select 1")


def test_empty_and_comment_only_queries_are_refused():
    with pytest.raises(SqlGuardError, match="empty"):
        check_sql("   ")
    with pytest.raises(SqlGuardError, match="only comments"):
        check_sql("-- just thinking out loud")


# ---------------------------------------------------------------------------
# things that must be allowed, because a guard nobody can use gets removed
# ---------------------------------------------------------------------------


def test_a_plain_select_passes():
    result = check_sql(f"select outlet_id, sum(line_total_myr) from {MART} group by 1")
    assert result.tables == (MART,)


def test_a_cte_passes():
    result = check_sql(
        f"with daily as (select order_date, sum(captured_revenue_myr) r from {MART} "
        "group by 1) select * from daily"
    )
    assert MART in result.tables


def test_joining_two_allowed_tables_passes():
    result = check_sql(
        f"select d.outlet_name from {MART} f "
        "join pulseops_mart.dim_outlet d on f.outlet_key = d.outlet_key"
    )
    assert set(result.tables) == {MART, "pulseops_mart.dim_outlet"}


def test_replace_the_function_is_not_mistaken_for_the_keyword():
    """REGEXP_REPLACE and REPLACE are ordinary string functions. blocking them
    would make the guard reject honest work."""
    result = check_sql(f"select replace(menu_item_name, 'Ayam', 'Chicken') from {MART}")
    assert result.sql


def test_payload_does_not_trip_the_load_keyword():
    result = check_sql(f"select event_id as payload_id from {MART}")
    assert result.sql


def test_a_project_qualified_table_is_accepted():
    result = check_sql(f"select 1 from `pulseops-muste.{MART}`")
    assert result.tables == (MART,)


def test_string_literals_survive_the_guard():
    """the guard must not change what the query means.

    it blanks string literals internally so a keyword or table name hiding
    inside quotes cannot fool the checks. an earlier version then returned that
    blanked text as the approved query, so `where payment_status = 'failed'`
    executed as `where payment_status = ''` and silently returned zero rows.
    no error, no warning, just a confidently wrong answer.
    """
    sql = f"select count(*) from {MART} where payment_status = 'failed'"
    result = check_sql(sql)
    assert "'failed'" in result.sql


def test_comments_may_be_stripped_but_logic_may_not():
    sql = f"select channel from {MART} where channel = 'dine_in' -- only eat-in"
    result = check_sql(sql)
    assert "'dine_in'" in result.sql


def test_multiple_literals_all_survive():
    sql = (
        f"select * from {MART} "
        "where payment_status in ('failed', 'refunded') and channel = 'delivery'"
    )
    result = check_sql(sql)
    for literal in ("'failed'", "'refunded'", "'delivery'"):
        assert literal in result.sql


# ---------------------------------------------------------------------------
# bounding the result
# ---------------------------------------------------------------------------


def test_a_limit_is_added_when_missing():
    result = check_sql(f"select * from {MART}")
    assert result.limit_added
    assert result.sql.strip().endswith(f"limit {MAX_LIMIT}")


def test_an_existing_limit_is_respected():
    result = check_sql(f"select * from {MART} limit 5")
    assert not result.limit_added
    assert "limit 5" in result.sql
    assert str(MAX_LIMIT) not in result.sql


def test_the_allowlist_can_be_narrowed():
    """a caller can hand the guard a smaller set than the default."""
    with pytest.raises(SqlGuardError, match="allowlist"):
        check_sql(f"select 1 from {MART}", allowed_tables=frozenset({"pulseops_mart.dim_date"}))
