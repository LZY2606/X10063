"""
Tests for Table.plan_transform() (dry-run plans) and the atomicity of
Table.transform().

The coverage is organised around the three audit axes for this feature:
- boundary symmetry: plan SQL equals executed SQL, from both the Python and
  CLI entry points;
- dry-run safety: planning writes nothing and is byte-for-byte reproducible;
- failure recovery: a failed transform rolls the whole table rebuild back.
"""

import pytest

from sqlite_utils.db import ColumnMapping, TransformError, TransformPlan
from sqlite_utils.utils import sqlite3


def _schema_snapshot(db):
    "Every persisted object definition for rollback checks."
    return list(
        db.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
    )


# --------------------------------------------------------------------------- #
# Dry-run safety
# --------------------------------------------------------------------------- #


def test_plan_transform_returns_plan_without_writing(fresh_db):
    table = fresh_db.table("dogs")
    table.insert_all(
        [
            {"id": 1, "name": "Cleo", "age": "5"},
            {"id": 2, "name": "Pancakes", "age": "4"},
        ],
        pk="id",
    )
    before = _schema_snapshot(fresh_db)

    plan = table.plan_transform(types={"age": int})

    assert isinstance(plan, TransformPlan)
    assert _schema_snapshot(fresh_db) == before
    assert not list(
        fresh_db.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%_new%'"
        ).fetchall()
    )
    assert table.columns_dict["age"] is str


def test_plan_transform_is_byte_reproducible(fresh_db):
    import sqlite_utils

    table = fresh_db.table("dogs")
    table.insert_all([{"id": 1, "name": "Cleo", "age": "5"}], pk="id")
    kwargs = {"types": {"age": int}, "rename": {"name": "full_name"}}
    first = table.plan_transform(**kwargs)
    second = table.plan_transform(**kwargs)

    other = sqlite_utils.Database(memory=True)
    other["dogs"].insert_all([{"id": 1, "name": "Cleo", "age": "5"}], pk="id")
    third = other.table("dogs").plan_transform(**kwargs)

    assert first.sql_steps == second.sql_steps
    assert first.sql_steps == third.sql_steps


def test_plan_uses_deterministic_tmp_name(fresh_db):
    fresh_db["dogs"].insert({"id": 1}, pk="id")
    plan = fresh_db.table("dogs").plan_transform()
    assert '"dogs_new"' in plan.sql_steps[0]


def test_plan_sql_matches_transform_sql_explicit_suffix(fresh_db):
    fresh_db["dogs"].insert({"id": 1, "name": "x"}, pk="id")
    table = fresh_db.table("dogs")
    plan = table.plan_transform(rename={"name": "n"}, tmp_suffix="abc")
    via_sql = table.transform_sql(rename={"name": "n"}, tmp_suffix="abc")
    assert plan.sql_steps == via_sql


# --------------------------------------------------------------------------- #
# Column mappings
# --------------------------------------------------------------------------- #


def test_plan_column_mappings_rename_type_drop(fresh_db):
    table = fresh_db.table("t")
    table.insert({"id": 1, "name": "a", "age": "1", "extra": "z"}, pk="id")
    plan = table.plan_transform(
        types={"age": int}, rename={"name": "full_name"}, drop={"extra"}
    )
    assert ColumnMapping("id", "id") in plan.columns
    assert ColumnMapping("full_name", "name") in plan.columns
    age = next(column for column in plan.columns if column.destination == "age")
    assert age.type_change == ("TEXT", "INTEGER")
    assert plan.dropped_columns == ("extra",)
    assert plan.new_columns == ()
    text = str(plan)
    assert "name -> full_name" in text
    assert "TEXT -> INTEGER" in text
    assert "Dropped columns: extra" in text


def test_plan_generated_column_mapping(fresh_db):
    fresh_db.execute(
        "CREATE TABLE g (id INTEGER PRIMARY KEY, a TEXT, "
        "b TEXT GENERATED ALWAYS AS (upper(a)) VIRTUAL)"
    )
    plan = fresh_db.table("g").plan_transform()
    mapping = next(column for column in plan.columns if column.destination == "b")
    assert mapping.generated is True
    assert mapping.source is None
    assert str(mapping) == "b GENERATED"


# --------------------------------------------------------------------------- #
# Generated columns - the most dangerous silent-data-loss regression
# --------------------------------------------------------------------------- #


def test_transform_preserves_virtual_generated_column(fresh_db):
    fresh_db.execute(
        "CREATE TABLE g (id INTEGER PRIMARY KEY, a TEXT, "
        "b TEXT GENERATED ALWAYS AS (upper(a)) VIRTUAL)"
    )
    fresh_db.execute("INSERT INTO g (id, a) VALUES (1, 'hi')")
    table = fresh_db.table("g")
    table.transform()
    assert "GENERATED ALWAYS AS (upper(a)) VIRTUAL" in table.schema
    assert list(table.rows) == [{"id": 1, "a": "hi", "b": "HI"}]


def test_transform_preserves_stored_generated_column_with_constraints(fresh_db):
    fresh_db.execute(
        "CREATE TABLE h (id INTEGER PRIMARY KEY, a TEXT NOT NULL, "
        "d TEXT NOT NULL GENERATED ALWAYS AS (a || '!') STORED "
        "CHECK(length(d) > 0))"
    )
    fresh_db.execute("INSERT INTO h (id, a) VALUES (1, 'x')")
    table = fresh_db.table("h")
    table.transform()
    assert "GENERATED ALWAYS AS (a || '!') STORED" in table.schema
    assert "NOT NULL" in table.schema
    assert "CHECK (length(d) > 0)" in table.schema
    assert list(table.rows) == [{"id": 1, "a": "x", "d": "x!"}]


def test_transform_renames_source_column_in_generated_expression(fresh_db):
    fresh_db.execute(
        "CREATE TABLE g (id INTEGER PRIMARY KEY, a TEXT, "
        "b TEXT GENERATED ALWAYS AS (upper(a)) VIRTUAL)"
    )
    fresh_db.execute("INSERT INTO g (id, a) VALUES (1, 'hi')")
    table = fresh_db.table("g")
    table.transform(rename={"a": "aa"})
    assert "GENERATED ALWAYS AS (upper(aa)) VIRTUAL" in table.schema
    assert list(table.rows) == [{"id": 1, "aa": "hi", "b": "HI"}]


def test_transform_renames_generated_column_itself(fresh_db):
    fresh_db.execute(
        "CREATE TABLE g (id INTEGER PRIMARY KEY, a TEXT, "
        "b TEXT GENERATED ALWAYS AS (upper(a)) VIRTUAL)"
    )
    table = fresh_db.table("g")
    table.transform(rename={"b": "b2"})
    xinfo_names = [
        row[1] for row in fresh_db.execute("PRAGMA table_xinfo(g)").fetchall()
    ]
    assert "b2" in xinfo_names
    assert "b" not in xinfo_names
    assert "GENERATED ALWAYS AS (upper(a)) VIRTUAL" in table.schema
    fresh_db.execute("INSERT INTO g (id, a) VALUES (1, 'hi')")
    assert table.get(1)["b2"] == "HI"


def test_transform_generated_column_is_idempotent(fresh_db):
    fresh_db.execute(
        "CREATE TABLE g (id INTEGER PRIMARY KEY, a TEXT, " "b AS (a + 1) STORED)"
    )
    table = fresh_db.table("g")
    first = table.transform_sql(tmp_suffix="X")
    second = table.transform_sql(tmp_suffix="X")
    assert first == second


def test_drop_column_referenced_by_generated_column_errors(fresh_db):
    fresh_db.execute(
        "CREATE TABLE g (id INTEGER PRIMARY KEY, a TEXT, "
        "b TEXT GENERATED ALWAYS AS (upper(a)) VIRTUAL)"
    )
    table = fresh_db.table("g")
    with pytest.raises(TransformError, match="generated column 'b'"):
        table.transform(drop={"a"})


def test_generated_column_cannot_be_primary_key(fresh_db):
    fresh_db.execute(
        "CREATE TABLE g (id INTEGER PRIMARY KEY, a TEXT, b AS (a + 1) VIRTUAL)"
    )
    with pytest.raises(TransformError, match="primary key"):
        fresh_db.table("g").transform(pk="b")


# --------------------------------------------------------------------------- #
# Partial and expression indexes
# --------------------------------------------------------------------------- #


def test_partial_index_preserved_when_unrelated_column_changes(fresh_db):
    fresh_db["t"].insert_all(
        [{"id": 1, "name": "a", "age": "1"}, {"id": 2, "name": None, "age": "2"}],
        pk="id",
    )
    fresh_db.execute("CREATE INDEX idx_partial ON t(name) WHERE name IS NOT NULL")
    table = fresh_db.table("t")
    plan = table.plan_transform(types={"age": int})
    assert plan.indexes_preserved == ("idx_partial",)
    table.transform(types={"age": int})
    index = next(index for index in table.indexes if index.name == "idx_partial")
    assert index.partial == 1


def test_partial_index_referencing_renamed_column_errors(fresh_db):
    fresh_db["t"].insert({"id": 1, "name": "a"}, pk="id")
    fresh_db.execute("CREATE INDEX idx_p ON t(name) WHERE name IS NOT NULL")
    with pytest.raises(TransformError, match="partial or expression index"):
        fresh_db.table("t").transform(rename={"name": "full_name"})


def test_partial_index_where_predicate_renamed_column_errors(fresh_db):
    # The renamed column only appears in the WHERE predicate, not as a key
    fresh_db["t"].insert({"id": 1, "name": "a", "age": 1}, pk="id")
    fresh_db.execute("CREATE INDEX idx_p ON t(age) WHERE name IS NOT NULL")
    with pytest.raises(TransformError, match="partial or expression index"):
        fresh_db.table("t").transform(rename={"name": "full_name"})


def test_expression_index_renamed_column_errors(fresh_db):
    fresh_db["t"].insert({"id": 1, "name": "a"}, pk="id")
    fresh_db.execute("CREATE INDEX idx_e ON t(lower(name))")
    with pytest.raises(TransformError, match="partial or expression index"):
        fresh_db.table("t").transform(rename={"name": "full_name"})


def test_simple_index_is_rebuilt_on_rename(fresh_db):
    fresh_db["t"].insert({"id": 1, "name": "a"}, pk="id")
    fresh_db.execute('CREATE INDEX idx_name ON t("name")')
    plan = fresh_db.table("t").plan_transform(rename={"name": "full_name"})
    assert plan.indexes_rebuilt == ("idx_name",)
    assert plan.indexes_preserved == ()


# --------------------------------------------------------------------------- #
# Composite foreign keys
# --------------------------------------------------------------------------- #


def test_plan_composite_foreign_key_preserved(fresh_db):
    fresh_db["parent"].create({"a": int, "b": int, "v": str}, pk=("a", "b"))
    fresh_db["child"].create({"id": int, "pa": int, "pb": int}, pk="id")
    fresh_db.table("child").add_foreign_key(("pa", "pb"), "parent", ("a", "b"))
    table = fresh_db.table("child")
    plan = table.plan_transform(types={"pa": str})
    assert len(plan.foreign_keys_preserved) == 1
    fk = plan.foreign_keys_preserved[0]
    assert fk.is_compound is True
    assert fk.columns == ("pa", "pb")
    assert fk.other_columns == ("a", "b")
    table.transform(types={"pa": str})
    after = table.foreign_keys[0]
    assert after.is_compound is True
    assert after.other_table == "parent"
    assert after.columns == ("pa", "pb")


def test_plan_drop_composite_foreign_key_by_tuple(fresh_db):
    fresh_db["parent"].create({"a": int, "b": int}, pk=("a", "b"))
    fresh_db["child"].create({"id": int, "pa": int, "pb": int}, pk="id")
    fresh_db.table("child").add_foreign_key(("pa", "pb"), "parent", ("a", "b"))
    plan = fresh_db.table("child").plan_transform(drop_foreign_keys=[("pa", "pb")])
    assert plan.foreign_keys_preserved == ()
    assert len(plan.foreign_keys_removed) == 1
    assert plan.foreign_keys_removed[0].columns == ("pa", "pb")
    assert "pa, pb -> parent(a, b)" in str(plan)


def test_plan_composite_foreign_key_renamed_columns(fresh_db):
    fresh_db["parent"].create({"a": int, "b": int}, pk=("a", "b"))
    fresh_db["child"].create({"id": int, "pa": int, "pb": int}, pk="id")
    fresh_db.table("child").add_foreign_key(("pa", "pb"), "parent", ("a", "b"))
    fresh_db.table("child").transform(rename={"pa": "parent_a", "pb": "parent_b"})
    fk = fresh_db.table("child").foreign_keys[0]
    assert fk.columns == ("parent_a", "parent_b")


# --------------------------------------------------------------------------- #
# FTS shadow tables
# --------------------------------------------------------------------------- #


def test_plan_lists_fts_dependencies_and_warnings(fresh_db):
    fresh_db["articles"].create({"id": int, "title": str, "body": str}, pk="id")
    fresh_db["articles"].insert({"id": 1, "title": "cat", "body": "x"})
    fresh_db["articles"].enable_fts(["title"], create_triggers=True)
    plan = fresh_db.table("articles").plan_transform()
    assert "articles_fts" in plan.dependencies
    # FTS5 shadow tables are reported too
    assert "articles_fts_data" in plan.dependencies
    assert any("articles_fts" in warning for warning in plan.warnings)
    assert set(plan.triggers_rebuilt) == {
        "articles_ai",
        "articles_ad",
        "articles_au",
    }


def test_transform_with_fts_keeps_search_working(fresh_db):
    fresh_db["articles"].create({"id": int, "title": str, "body": str}, pk="id")
    fresh_db["articles"].insert_all(
        [
            {"id": 1, "title": "cat dog", "body": "x"},
            {"id": 2, "title": "fish", "body": "y"},
        ]
    )
    fresh_db["articles"].enable_fts(["title"], create_triggers=True)
    # Type-only transform must leave the FTS index and sync triggers intact
    fresh_db.table("articles").transform()
    fresh_db["articles"].insert({"id": 3, "title": "kitten", "body": "z"})
    hits = [row["title"] for row in fresh_db["articles_fts"].search("kitten")]
    assert hits == ["kitten"]
    # Existing content is still searchable
    assert [row["title"] for row in fresh_db["articles_fts"].search("cat")] == [
        "cat dog"
    ]


def test_transform_rename_fts_indexed_column_errors(fresh_db):
    fresh_db["articles"].create({"id": int, "title": str}, pk="id")
    fresh_db["articles"].enable_fts(["title"])
    with pytest.raises(TransformError, match="FTS table 'articles_fts'"):
        fresh_db.table("articles").transform(rename={"title": "headline"})


def test_transform_drop_fts_indexed_column_errors(fresh_db):
    fresh_db["articles"].create({"id": int, "title": str}, pk="id")
    fresh_db["articles"].enable_fts(["title"])
    with pytest.raises(TransformError, match="FTS table 'articles_fts'"):
        fresh_db.table("articles").transform(drop={"title"})


def test_transform_fts_rename_unrelated_column_keeps_index(fresh_db):
    fresh_db["articles"].create({"id": int, "title": str, "note": str}, pk="id")
    fresh_db["articles"].enable_fts(["title"], create_triggers=True)
    # Renaming a column the FTS index does not read is fine
    fresh_db.table("articles").transform(rename={"note": "memo"})
    fresh_db["articles"].insert({"id": 1, "title": "cat", "memo": "m"})
    assert [row["title"] for row in fresh_db["articles_fts"].search("cat")] == ["cat"]


# --------------------------------------------------------------------------- #
# Quoted identifiers
# --------------------------------------------------------------------------- #


def test_plan_and_transform_quoted_identifiers(fresh_db):
    fresh_db.execute(
        'CREATE TABLE "weird table" ('
        '"id" INTEGER PRIMARY KEY, "first name" TEXT, "select" TEXT)'
    )
    fresh_db.execute(
        'INSERT INTO "weird table" (id, "first name", "select") VALUES (1, "a", "b")'
    )
    fresh_db.execute('CREATE INDEX "weird idx" ON "weird table" ("first name")')
    table = fresh_db.table("weird table")
    plan = table.plan_transform(rename={"first name": "given name"})
    # Quoted identifiers survive into every emitted statement
    assert '"weird table_new"' in plan.sql_steps[0]
    assert '"given name"' in plan.sql_steps[0]
    assert '"weird idx"' in "\n".join(plan.sql_steps)
    assert plan.indexes_rebuilt == ("weird idx",)
    table.transform(rename={"first name": "given name"})
    assert table.columns_dict == {"id": int, "given name": str, "select": str}
    assert list(table.rows) == [{"id": 1, "given name": "a", "select": "b"}]
    assert table.indexes[0].columns == ["given name"]


# --------------------------------------------------------------------------- #
# Atomic execution / failure recovery
# --------------------------------------------------------------------------- #


def test_failed_transform_rolls_back_entire_table(fresh_db):
    table = fresh_db.table("t")
    table.insert_all(
        [
            {"id": 1, "name": "a", "age": "1"},
            {"id": 2, "name": None, "age": "2"},
        ],
        pk="id",
    )
    fresh_db.execute("CREATE INDEX idx_age ON t(age)")
    fresh_db.execute(
        "CREATE TRIGGER trg_t AFTER INSERT ON t BEGIN "
        "UPDATE t SET age = age WHERE 0; END;"
    )
    before_schema = _schema_snapshot(fresh_db)
    before_rows = list(fresh_db.execute("SELECT id, name, age FROM t"))

    with pytest.raises(sqlite3.IntegrityError):
        table.transform(not_null={"name": True})

    # Original table, data, index and trigger are all exactly as before
    assert _schema_snapshot(fresh_db) == before_schema
    assert list(fresh_db.execute("SELECT id, name, age FROM t")) == before_rows
    assert [index.name for index in table.indexes] == ["idx_age"]
    assert [trigger.name for trigger in table.triggers] == ["trg_t"]
    assert not list(
        fresh_db.execute("SELECT name FROM sqlite_master WHERE name LIKE '%_new%'")
    )


def test_failed_plan_execution_rolls_back(fresh_db):
    table = fresh_db.table("t")
    table.insert_all([{"id": 1, "v": "a"}, {"id": 2, "v": None}], pk="id")
    plan = table.plan_transform(not_null={"v": True})
    before_schema = _schema_snapshot(fresh_db)
    with pytest.raises(sqlite3.IntegrityError):
        table.transform(plan=plan)
    assert _schema_snapshot(fresh_db) == before_schema
    assert table.exists()


def test_transform_plan_and_direct_execution_equivalent(fresh_db):
    """Symmetric entry: executing the plan must match direct transform()."""
    import sqlite_utils

    def build_db():
        database = sqlite_utils.Database(memory=True)
        database["t"].insert_all([{"id": 1, "name": "a", "age": "1"}], pk="id")
        database.execute("CREATE INDEX idx_age ON t(age)")
        database.execute(
            "CREATE TRIGGER trg_t AFTER INSERT ON t BEGIN "
            "UPDATE t SET age = '9' WHERE id = new.id; END;"
        )
        return database

    direct = build_db()
    direct.table("t").transform(types={"age": int}, rename={"name": "full_name"})

    planned = build_db()
    plan = planned.table("t").plan_transform(
        types={"age": int}, rename={"name": "full_name"}
    )
    planned.table("t").transform(plan=plan)

    assert direct.table("t").schema == planned.table("t").schema
    assert list(direct.table("t").rows) == list(planned.table("t").rows)
    assert [(index.name, index.columns) for index in direct.table("t").indexes] == [
        (index.name, index.columns) for index in planned.table("t").indexes
    ]
    assert [trigger.sql for trigger in direct.table("t").triggers] == [
        trigger.sql for trigger in planned.table("t").triggers
    ]


def test_transform_rejects_plan_with_other_arguments(fresh_db):
    fresh_db["t"].insert({"id": 1}, pk="id")
    plan = fresh_db.table("t").plan_transform()
    with pytest.raises(ValueError, match="plan="):
        fresh_db.table("t").transform(plan=plan, rename={"id": "pk"})


def test_transform_rejects_plan_for_other_table(fresh_db):
    fresh_db["a"].insert({"id": 1}, pk="id")
    fresh_db["b"].insert({"id": 1}, pk="id")
    plan = fresh_db.table("a").plan_transform()
    with pytest.raises(ValueError, match="table 'b'"):
        fresh_db.table("b").transform(plan=plan)


def test_legacy_alter_table_pragma_restored_after_failure(fresh_db):
    assert fresh_db.execute("PRAGMA legacy_alter_table").fetchone()[0] == 0
    table = fresh_db.table("t")
    table.insert_all([{"id": 1, "v": "a"}, {"id": 2, "v": None}], pk="id")
    with pytest.raises(sqlite3.IntegrityError):
        table.transform(not_null={"v": True})
    assert fresh_db.execute("PRAGMA legacy_alter_table").fetchone()[0] == 0


def test_plan_transform_missing_table_raises(fresh_db):
    with pytest.raises(ValueError, match="doesn't exist"):
        fresh_db.table("nope").plan_transform()


def test_transform_preserves_triggers_across_rebuild(fresh_db):
    fresh_db["t"].create({"id": int, "name": str, "age": str}, pk="id")
    fresh_db.execute(
        "CREATE TRIGGER trg_t AFTER INSERT ON t BEGIN "
        "UPDATE t SET age = 'z' WHERE id = new.id; END;"
    )
    fresh_db.table("t").transform(types={"age": int})
    triggers = fresh_db.table("t").triggers
    assert [trigger.name for trigger in triggers] == ["trg_t"]
    fresh_db.table("t").insert({"id": 1, "name": "a", "age": None})
    assert fresh_db.table("t").get(1)["age"] == "z"


def test_drop_column_referenced_by_trigger_errors(fresh_db):
    """Most dangerous trigger regression: a stale trigger referencing a
    dropped column would either be silently lost or fail on next insert."""
    fresh_db["t"].create({"id": int, "name": str, "extra": str}, pk="id")
    fresh_db["t"].insert({"id": 1, "name": "a", "extra": "z"})
    fresh_db.execute(
        "CREATE TRIGGER bad AFTER INSERT ON t BEGIN "
        "UPDATE t SET name = upper(name) WHERE id = new.id; END;"
    )
    with pytest.raises(TransformError, match="trigger 'bad'"):
        fresh_db.table("t").transform(drop={"name"})
    # Table and trigger both untouched
    assert "name" in fresh_db.table("t").columns_dict
    assert [trigger.name for trigger in fresh_db.table("t").triggers] == ["bad"]


def test_drop_unrelated_column_keeps_trigger(fresh_db):
    fresh_db["t"].create({"id": int, "name": str, "extra": str}, pk="id")
    fresh_db.execute(
        "CREATE TRIGGER bad AFTER INSERT ON t BEGIN "
        "UPDATE t SET name = upper(name) WHERE id = new.id; END;"
    )
    fresh_db.table("t").transform(drop={"extra"})
    assert [trigger.name for trigger in fresh_db.table("t").triggers] == ["bad"]
    fresh_db.table("t").insert({"id": 1, "name": "b"})
    assert fresh_db.table("t").get(1)["name"] == "B"


def test_plan_execution_with_colliding_tmp_table_errors(fresh_db):
    fresh_db["t"].create({"id": int, "name": str}, pk="id")
    fresh_db["t_new"].create({"id": int}, pk="id")
    plan = fresh_db.table("t").plan_transform(rename={"name": "n"})
    with pytest.raises(TransformError, match="temporary table 't_new'"):
        fresh_db.table("t").transform(plan=plan)
    assert fresh_db.table("t").exists()
    assert fresh_db.table("t_new").exists()
    assert "name" in fresh_db.table("t").columns_dict


def test_plan_foreign_key_replacement_classification(fresh_db):
    fresh_db["p"].create({"id": int}, pk="id")
    fresh_db["c"].create({"id": int, "a": int, "b": int}, pk="id")
    fresh_db["c"].add_foreign_key("a", "p", "id")
    plan = fresh_db.table("c").plan_transform(foreign_keys=[("b", "p", "id")])
    assert [fk.columns for fk in plan.foreign_keys_added] == [("b",)]
    assert [fk.columns for fk in plan.foreign_keys_removed] == [("a",)]
    assert plan.foreign_keys_preserved == ()


def test_plan_add_foreign_key_keeps_existing(fresh_db):
    fresh_db["p"].create({"id": int}, pk="id")
    fresh_db["c"].create({"id": int, "a": int, "b": int}, pk="id")
    fresh_db["c"].add_foreign_key("a", "p", "id")
    plan = fresh_db.table("c").plan_transform(
        add_foreign_keys=[("a", "p", "id"), ("b", "p", "id")]
    )
    assert [fk.columns for fk in plan.foreign_keys_added] == [("b",)]
    assert [fk.columns for fk in plan.foreign_keys_preserved] == [("a",)]


def test_plan_keep_table_with_fts_warns(fresh_db):
    fresh_db["a"].create({"id": int, "t": str}, pk="id")
    fresh_db["a"].enable_fts(["t"])
    plan = fresh_db.table("a").plan_transform(keep_table="a_backup")
    assert "a_fts" in plan.dependencies
    assert any("keep_table" in warning for warning in plan.warnings)
