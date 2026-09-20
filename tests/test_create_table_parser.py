import sqlite3

import hypothesis.strategies as st
import pytest
from hypothesis import given

from sqlite_utils.create_table_parser import (
    Check,
    ColumnComments,
    ParseError,
    Unique,
    UniqueColumn,
    parse_autoincrement,
    parse_checks,
    parse_column_comments,
    parse_uniques,
)


def test_parse_column_and_table_checks():
    sql = """
        CREATE TABLE people (
            age INTEGER CONSTRAINT positive CHECK (age > 0),
            status TEXT CHECK(status IN ('active', 'inactive')),
            CONSTRAINT adult CHECK(age >= 18)
        )
    """
    assert parse_checks(sql) == [
        Check("age > 0", name="positive", column="age"),
        Check(
            "status IN ('active', 'inactive')",
            column="status",
            options=["active", "inactive"],
        ),
        Check("age >= 18", name="adult"),
    ]
    checks = parse_checks(sql)
    assert checks[0].sql == "CONSTRAINT positive CHECK (age > 0)"
    assert sql[checks[0].start : checks[0].end] == checks[0].sql
    assert checks[1].sql == "CHECK(status IN ('active', 'inactive'))"
    assert sql[checks[2].start : checks[2].end] == checks[2].sql


def test_comments_are_trivia_not_constraints():
    sql = """
        CREATE /* fake CHECK (nope), ( */ TABLE t (
            a INTEGER /* CHECK (a < 0), phantom */,
            b INTEGER CHECK /* between keyword and expression */ (b > 0),
            /* CHECK (also_fake) */ CONSTRAINT upper CHECK(b < 10)
        )
    """
    sqlite3.connect(":memory:").execute(sql)
    assert parse_checks(sql) == [
        Check("b > 0", column="b"),
        Check("b < 10", name="upper"),
    ]


def test_parse_comments_owned_by_columns():
    sql = """
        CREATE TABLE t (
            -- Before id
            id /* Between name and type */ INTEGER /* After id */,
            /* Between column definitions */
            value TEXT CHECK(value != '') /* After value */,
            /* Before a table constraint, not a column */
            CHECK(value != 'forbidden')
        )
    """
    assert parse_column_comments(sql) == {
        "id": ColumnComments(before="-- Before id", after="/* After id */"),
        "value": ColumnComments(
            before="/* Between column definitions */",
            after="/* After value */",
        ),
    }


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("value IN ('one', 'two')", ["one", "two"]),
        ("((value IN ('one', 'two')))", ["one", "two"]),
        ("value NOT IN ('one', 'two')", None),
        ("value IN ('one', 'two') OR enabled", None),
        ("other IN ('one', 'two')", None),
        ("value IN (lower('one'), 'two')", None),
        ('value IN ("other")', None),
    ],
)
def test_options_only_for_exact_literal_in_check(expression, expected):
    sql = f"CREATE TABLE t(value TEXT CHECK({expression}), enabled INTEGER, other TEXT)"
    sqlite3.connect(":memory:").execute(sql)
    assert parse_checks(sql)[0].options == expected


@pytest.mark.parametrize("column", ["💩x", "e\u0301"])
def test_unquoted_unicode_identifiers(column):
    sql = f"CREATE TABLE t({column} INTEGER CHECK({column} > 0))"
    sqlite3.connect(":memory:").execute(sql)
    assert parse_checks(sql) == [Check(f"{column} > 0", column=column)]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT CHECK(x > 0)",
        "CREATE TABLE t(x INTEGER CHECK(x > 0)",
        "CREATE TABLE t(x TEXT CHECK(x != 'unterminated))",
        "CREATE TABLE t(x INTEGER /* unterminated)",
    ],
)
def test_invalid_sql_raises_parse_error(sql):
    with pytest.raises(ParseError):
        parse_checks(sql)


def test_virtual_table_has_no_checks():
    assert (
        parse_checks("CREATE /* comment */ VIRTUAL TABLE search USING fts5(text)") == []
    )


@pytest.mark.parametrize(
    "sql,expected",
    [
        (
            "CREATE TABLE t(id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT)",
            "id",
        ),
        (
            'CREATE TABLE t("quoted id" INTEGER PRIMARY KEY AUTOINCREMENT)',
            "quoted id",
        ),
        (
            'CREATE TABLE t("autoincrement" INTEGER PRIMARY KEY, value TEXT)',
            None,
        ),
        (
            "CREATE TABLE t(id INTEGER PRIMARY KEY /* AUTOINCREMENT */, value TEXT)",
            None,
        ),
        (
            "CREATE TABLE t(id INTEGER PRIMARY KEY, value TEXT CHECK(value != 'AUTOINCREMENT'))",
            None,
        ),
    ],
)
def test_parse_autoincrement(sql, expected):
    sqlite3.connect(":memory:").execute(sql)
    assert parse_autoincrement(sql) == expected


def test_parse_column_and_table_uniques():
    sql = """
        CREATE TABLE memberships (
            email TEXT COLLATE RTRIM CONSTRAINT unique_email UNIQUE ON CONFLICT IGNORE,
            account_id INTEGER,
            CONSTRAINT unique_membership UNIQUE (
                account_id DESC,
                email COLLATE NOCASE ASC
            ) ON CONFLICT REPLACE
        )
    """
    sqlite3.connect(":memory:").execute(sql)
    assert parse_uniques(sql) == [
        Unique(
            (UniqueColumn("email", collation="RTRIM"),),
            name="unique_email",
            column="email",
            conflict="IGNORE",
        ),
        Unique(
            (
                UniqueColumn("account_id", order="DESC"),
                UniqueColumn("email", collation="NOCASE", order="ASC"),
            ),
            name="unique_membership",
            conflict="REPLACE",
        ),
    ]
    uniques = parse_uniques(sql)
    assert uniques[0].sql == "CONSTRAINT unique_email UNIQUE ON CONFLICT IGNORE"
    assert sql[uniques[1].start : uniques[1].end] == uniques[1].sql


def test_unique_like_text_in_comments_and_checks_is_ignored():
    sql = """
        CREATE TABLE t (
            value TEXT /* UNIQUE ON CONFLICT REPLACE */
                CHECK(value != 'UNIQUE(other)'),
            other TEXT
        )
    """
    sqlite3.connect(":memory:").execute(sql)
    assert parse_uniques(sql) == []


comment_or_space = st.sampled_from(
    [
        " ",
        "\n  ",
        "/* comment with , ( ) and CHECK(fake) */",
        "-- comment with , ( ) and CHECK(fake)\n",
    ]
)


@given(gaps=st.lists(comment_or_space, min_size=5, max_size=5))
def test_comments_and_whitespace_can_separate_check_tokens(gaps):
    sql = (
        f"CREATE{gaps[0]}TABLE{gaps[1]}t{gaps[2]}("
        f"value INTEGER CHECK{gaps[3]}(value{gaps[4]}> 0))"
    )
    connection = sqlite3.connect(":memory:")
    connection.execute(sql)
    stored_sql = connection.execute(
        "select sql from sqlite_master where name = 't'"
    ).fetchone()[0]
    assert parse_checks(stored_sql) == [Check(f"value{gaps[4]}> 0", column="value")]


safe_string_text = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cc", "Cs"),
        blacklist_characters=("'",),
    ),
    max_size=40,
)


@given(value=safe_string_text)
def test_check_like_text_inside_strings_is_opaque(value):
    sql = f"CREATE TABLE t(value TEXT CHECK(value != '{value}'))"
    connection = sqlite3.connect(":memory:")
    connection.execute(sql)
    stored_sql = connection.execute(
        "select sql from sqlite_master where name = 't'"
    ).fetchone()[0]
    checks = parse_checks(stored_sql)
    assert len(checks) == 1
    assert checks[0].column == "value"
    assert checks[0].check == f"value != '{value}'"


def test_parse_generated_columns_virtual_stored_and_shorthand():
    from sqlite_utils.create_table_parser import parse_generated_columns

    sql = """
        CREATE TABLE g (
            id INTEGER PRIMARY KEY,
            first TEXT,
            full TEXT GENERATED ALWAYS AS (first || ' ' || last) VIRTUAL,
            doubled INTEGER AS (id * 2) STORED,
            bare AS (id + 1),
            CHECK (id > 0)
        )
    """
    generated = parse_generated_columns(sql)
    assert set(generated) == {"full", "doubled", "bare"}
    assert generated["full"].declared_type == "TEXT"
    assert generated["full"].expression == "first || ' ' || last"
    assert generated["full"].storage == "VIRTUAL"
    assert generated["doubled"].declared_type == "INTEGER"
    assert generated["doubled"].expression == "id * 2"
    assert generated["doubled"].storage == "STORED"
    assert generated["bare"].declared_type == ""
    assert generated["bare"].storage == "VIRTUAL"
    assert generated["bare"].clause.startswith("AS (id + 1)")


def test_parse_generated_columns_empty_for_plain_table():
    from sqlite_utils.create_table_parser import parse_generated_columns

    assert parse_generated_columns(
        "CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT)"
    ) == {}


def test_plan_trigger_sql_qualified_rename_and_drop():
    from sqlite_utils.create_table_parser import plan_trigger_sql

    sql = (
        "CREATE TRIGGER t_ai AFTER INSERT ON t BEGIN "
        "INSERT INTO logs(msg) VALUES (new.name || old.name); END;"
    )
    rewritten = plan_trigger_sql(sql, "t", rename={"name": "full_name"})
    assert rewritten.recreate is True
    assert "new.full_name" in rewritten.sql
    assert "old.full_name" in rewritten.sql
    dropped = plan_trigger_sql(sql, "t", drop={"name"})
    assert dropped.recreate is False
    assert "dropped column 'name'" in dropped.reason


def test_plan_trigger_sql_quoted_qualified_and_verbatim():
    from sqlite_utils.create_table_parser import plan_trigger_sql

    sql = 'CREATE TRIGGER "a_ai" AFTER INSERT ON "a" BEGIN INSERT INTO "a_fts" ("body") VALUES (new."body"); END;'
    rewritten = plan_trigger_sql(sql, "a", rename={"body": "body2"})
    assert rewritten.recreate is True
    assert 'new."body2"' in rewritten.sql
    verbatim = plan_trigger_sql(sql, "a")
    assert verbatim.recreate is True
    assert verbatim.sql == sql


def test_plan_trigger_sql_bare_rename_in_when_is_not_rewritten():
    from sqlite_utils.create_table_parser import plan_trigger_sql

    sql = (
        "CREATE TRIGGER t_ai AFTER INSERT ON t WHEN name IS NOT NULL "
        "BEGIN INSERT INTO logs(x) VALUES(1); END;"
    )
    result = plan_trigger_sql(sql, "t", rename={"name": "full_name"})
    assert result.recreate is False
    assert "NEW./OLD." in result.reason


def test_plan_trigger_sql_self_update_set_is_rewritten():
    from sqlite_utils.create_table_parser import plan_trigger_sql

    sql = (
        "CREATE TRIGGER t_ai AFTER INSERT ON t "
        "BEGIN UPDATE t SET name = upper(new.name); END;"
    )
    result = plan_trigger_sql(sql, "t", rename={"name": "full_name"})
    assert result.recreate is True
    assert "SET full_name = upper(new.full_name)" in result.sql


def test_plan_trigger_sql_external_update_set_target_ignored():
    from sqlite_utils.create_table_parser import plan_trigger_sql

    sql = "CREATE TRIGGER t_ai AFTER INSERT ON t BEGIN UPDATE other SET name = 'x'; END;"
    result = plan_trigger_sql(sql, "t", rename={"name": "full_name"})
    assert result.recreate is True
    assert "full_name" not in result.sql
