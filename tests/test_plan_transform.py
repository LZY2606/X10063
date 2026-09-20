"""
Tests for Table.plan_transform() dry-run planning and the atomic execution of
the returned TransformPlan.

Coverage is organized along the three axes called out for schema transforms:
the two sides of the boundary (plan-only zero writes vs. execute writes),
symmetric entry points (plan_transform/transform/transform_sql/
TransformPlan.execute), and failure recovery (a failure at any step rolls the
database back to the original table).
"""

import dataclasses
import hashlib
import sqlite3
from pathlib import Path

import pytest

from sqlite_utils import Database
from sqlite_utils.db import (
    ColumnMapping,
    ForeignKey,
    PlannedForeignKey,
    PlannedIndex,
    PlannedTrigger,
    TransformError,
    TransformPlan,
    TransformStep,
)
from sqlite_utils.db import quote_identifier


@pytest.fixture
def populated_db(fresh_db):
    fresh_db.table("authors").insert({"id": 1, "name": "Jane"}, pk="id")
    fresh_db.table("books").insert(
        {"id": 1, "title": "Reality is Broken", "author_id": 1},
        pk="id",
        foreign_keys=("author_id",),
    )
    fresh_db.conn.execute("CREATE INDEX ix_books_title ON books(title)")
    fresh_db.conn.execute("CREATE TABLE trigger_log(msg TEXT)")
    fresh_db.conn.execute("""
        CREATE TRIGGER books_ai AFTER INSERT ON books BEGIN
            INSERT INTO trigger_log(msg) VALUES('echo-' || new.title);
        END;
        """)
    return fresh_db


def _file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


def test_plan_transform_returns_plan_without_running(populated_db):
    table = populated_db.table("books")
    plan = table.plan_transform(rename={"title": "book_title"})
    assert isinstance(plan, TransformPlan)
    assert plan.table == "books"
    assert plan.temporary_table == "books_new_plan"
    # Nothing was executed: no replacement table exists and data is unchanged
    assert not populated_db.table("books_new_plan").exists()
    assert list(table.rows) == [{"id": 1, "title": "Reality is Broken", "author_id": 1}]
    assert plan.steps and all(isinstance(step, TransformStep) for step in plan.steps)
    assert plan.sqls == [step.sql for step in plan.steps]


def test_plan_does_not_write_to_file(tmp_path):
    db_path = tmp_path / "docs.db"
    db = Database(str(db_path))
    db.table("books").insert_all(
        [{"id": 1, "title": "One"}, {"id": 2, "title": "Two"}], pk="id"
    )
    db.conn.execute("CREATE INDEX ix_title ON books(title)")
    db.conn.commit()
    before = _file_digest(db_path)

    for _ in range(3):
        db.table("books").plan_transform(rename={"title": "headline"})

    db.conn.commit()
    assert _file_digest(db_path) == before


def test_plan_is_byte_reproducible_on_the_same_schema():
    def build_plan():
        db = Database(memory=True)
        db.table("books").insert_all(
            [{"id": 1, "title": "One", "author_id": 2}], pk="id"
        )
        db.conn.execute("CREATE INDEX ix_title ON books(title)")
        db.conn.execute("""
            CREATE TRIGGER books_ai AFTER INSERT ON books BEGIN
                UPDATE books SET title = new.title WHERE id = new.id;
            END;
            """)
        return db.table("books").plan_transform(
            rename={"title": "headline"}, types={"id": int}
        )

    first = build_plan()
    second = build_plan()
    assert first.sqls == second.sqls
    assert dataclasses.replace(first, _database=None) == dataclasses.replace(
        second, _database=None
    )
    # Fixed temp table name makes the SQL itself reproducible
    assert 'CREATE TABLE "books_new_plan"' in first.sqls[0]


def test_plan_reports_column_mapping_and_before_after_columns(populated_db):
    table = populated_db.table("books")
    plan = table.plan_transform(
        rename={"title": "book_title"}, drop={"author_id"}, types={"id": int}
    )
    by_old = {mapping.old_name: mapping for mapping in plan.column_mapping}
    assert by_old["title"] == ColumnMapping(
        "title", "book_title", copy=True, copy_expression=quote_identifier("title")
    )
    assert by_old["title"].copy_expression == quote_identifier("title")
    assert by_old["author_id"].dropped is True
    assert by_old["author_id"].new_name is None
    assert plan.dropped_columns == ("author_id",)
    assert plan.renamed_columns == {"title": "book_title"}
    assert [column.name for column in plan.columns_before] == [
        "id",
        "title",
        "author_id",
    ]
    assert [column.name for column in plan.columns_after] == ["id", "book_title"]
    # The copy SQL excludes the dropped column and carries the rename
    copy_sql = next(sql for sql in plan.sqls if sql.startswith("INSERT INTO"))
    assert '"book_title"' in copy_sql
    assert '"author_id"' not in copy_sql.split("FROM")[0]


def test_plan_empty_string_to_numeric_copy_expression(populated_db):
    populated_db.table("books").insert({"id": 2, "title": "", "author_id": 1}, pk="id")
    plan = populated_db.table("books").plan_transform(types={"title": int})
    mapping = {m.old_name: m for m in plan.column_mapping}["title"]
    assert mapping.copy_expression == "NULLIF(\"title\", '')"


def test_plan_reports_indexes_kept_and_their_sql(populated_db):
    plan = populated_db.table("books").plan_transform(types={"id": int})
    (index_plan,) = plan.indexes
    assert isinstance(index_plan, PlannedIndex)
    assert index_plan.name == "ix_books_title"
    assert index_plan.kept is True
    assert "CREATE INDEX ix_books_title ON books(title)" in index_plan.recreated_sql
    assert plan.indexes_lost == ()
    assert plan.indexes_kept == (index_plan,)


def test_plan_rename_partial_index_keeps_old_error_boundary(populated_db):
    populated_db.conn.execute(
        "CREATE INDEX ix_partial ON books(author_id) WHERE title IS NOT NULL"
    )
    # Existing API: renaming columns used by a partial/expression index raises
    # TransformError up front - planning must do the same and write nothing
    before = list(populated_db.conn.execute("SELECT name, sql FROM sqlite_master"))
    with pytest.raises(TransformError, match="partial or expression index"):
        populated_db.table("books").plan_transform(rename={"title": "headline"})
    after = list(populated_db.conn.execute("SELECT name, sql FROM sqlite_master"))
    assert before == after


def test_plan_index_referencing_dropped_column_keeps_old_error(populated_db):
    with pytest.raises(TransformError, match="is not in updated table"):
        populated_db.table("books").plan_transform(drop={"title"})


def test_plan_reports_trigger_kept_and_rewrites_qualified_columns(populated_db):
    plan = populated_db.table("books").plan_transform(rename={"title": "headline"})
    (trigger_plan,) = plan.triggers
    assert isinstance(trigger_plan, PlannedTrigger)
    assert trigger_plan.kept is True
    assert trigger_plan.name == "books_ai"
    assert (
        'new."headline"' in trigger_plan.recreated_sql
        or "new.headline" in trigger_plan.recreated_sql
    )
    assert plan.triggers_lost == ()


def test_plan_marks_trigger_lost_for_dropped_column_with_reason_and_sql(fresh_db):
    fresh_db.executescript("""
        CREATE TABLE logs(msg);
        CREATE TABLE t(id INTEGER PRIMARY KEY, name TEXT);
        CREATE TRIGGER t_ai AFTER INSERT ON t BEGIN
            INSERT INTO logs(msg) VALUES ('x ' || new.name);
        END;
        """)
    plan = fresh_db.table("t").plan_transform(drop={"name"})
    (lost,) = plan.triggers_lost
    assert lost.name == "t_ai"
    assert lost.kept is False
    assert "dropped column 'name'" in lost.reason
    assert "CREATE TRIGGER t_ai" in lost.original_sql
    # The trigger SQL is not among the executable steps
    assert not any("CREATE TRIGGER" in sql for sql in plan.sqls)
    assert any("will be dropped" in warning for warning in plan.warnings)


def test_plan_marks_trigger_lost_for_unqualified_renamed_column(fresh_db):
    # Bare "name" inside an expression cannot be rewritten safely - could be a
    # function argument or a column of another table.
    fresh_db.executescript("""
        CREATE TABLE logs(msg);
        CREATE TABLE t(id INTEGER PRIMARY KEY, name TEXT);
        CREATE TRIGGER t_ai AFTER INSERT ON t
        WHEN name IS NOT NULL
        BEGIN
            INSERT INTO logs(msg) VALUES ('x');
        END;
        """)
    plan = fresh_db.table("t").plan_transform(rename={"name": "full_name"})
    (lost,) = plan.triggers_lost
    assert "renamed column 'name'" in lost.reason
    assert "NEW./OLD." in lost.reason


def test_plan_counts_trigger_survives_unrelated_rename(fresh_db):
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"}, pk="id")
    fresh_db.table("dogs").enable_counts()
    plan = fresh_db.table("dogs").plan_transform(rename={"name": "full_name"})
    assert {trigger.name for trigger in plan.triggers_kept} == {
        "dogs_counts_insert",
        "dogs_counts_delete",
    }
    plan.execute()
    assert fresh_db.table("dogs").count == 1
    fresh_db.table("dogs").insert({"id": 2, "full_name": "Pancake"})
    assert fresh_db.table("dogs").count == 2


def test_plan_reports_foreign_keys_kept_and_dropped(fresh_db):
    fresh_db.table("authors").insert({"id": 1, "name": "Jane"}, pk="id")
    fresh_db.table("country").insert({"id": 1, "name": "France"}, pk="id")
    fresh_db.table("city").insert({"id": 1, "name": "Paris"}, pk="id")
    fresh_db.table("places").insert(
        {"id": 1, "city": 1, "country": 1},
        foreign_keys=("city", "country"),
    )
    plan = fresh_db.table("places").plan_transform(drop_foreign_keys=("country",))
    kept = {fk.columns[0] for fk in plan.foreign_keys if not fk.dropped}
    assert kept == {"city"}
    (dropped_fk,) = plan.foreign_keys_dropped
    assert isinstance(dropped_fk, PlannedForeignKey)
    assert dropped_fk.columns == ("country",)
    assert dropped_fk.dropped is True


def test_plan_composite_foreign_key_survives_quoted_rename(fresh_db):
    fresh_db.conn.executescript("""
        CREATE TABLE "parent table" (
            "k 1" INTEGER, "k 2" INTEGER,
            PRIMARY KEY("k 1", "k 2")
        );
        CREATE TABLE "child""x" (
            id INTEGER PRIMARY KEY,
            "c 1" INTEGER, "c 2" INTEGER,
            FOREIGN KEY("c 1", "c 2")
                REFERENCES "parent table"("k 1", "k 2")
        );
        """)
    fresh_db.table("parent table").insert({"k 1": 1, "k 2": 2})
    fresh_db.table('child"x').insert({"id": 1, "c 1": 1, "c 2": 2})
    table = fresh_db.table('child"x')
    plan = table.plan_transform(rename={"c 1": "cc 1"})
    (fk_plan,) = plan.foreign_keys
    assert fk_plan.is_compound is True
    assert fk_plan.columns == ("cc 1", "c 2")
    assert fk_plan.other_columns == ("k 1", "k 2")
    plan.execute()
    assert table.foreign_keys == [
        ForeignKey(
            table='child"x',
            column=None,
            other_table="parent table",
            other_column=None,
            columns=("cc 1", "c 2"),
            other_columns=("k 1", "k 2"),
            is_compound=True,
        )
    ]
    assert list(table.rows) == [{"id": 1, "cc 1": 1, "c 2": 2}]


def test_plan_preserves_virtual_and_stored_generated_columns(fresh_db):
    fresh_db.conn.executescript("""
        CREATE TABLE g (
            id INTEGER PRIMARY KEY,
            first TEXT,
            last TEXT,
            full TEXT GENERATED ALWAYS AS (first || ' ' || last) VIRTUAL,
            doubled INTEGER AS (id * 2) STORED
        );
        """)
    fresh_db.table("g").insert({"id": 1, "first": "Ada", "last": "Lovelace"})
    table = fresh_db.table("g")
    plan = table.plan_transform(rename={"first": "firstname"})
    after = {column.name: column for column in plan.columns_after}
    assert after["full"].generated is not None
    assert after["doubled"].generated.storage == "STORED"
    # The expression follows the renamed column
    assert "firstname" in after["full"].generated.expression
    # Generated columns are never copied
    mappings = {m.old_name: m for m in plan.column_mapping}
    assert mappings["full"].copy is False
    assert mappings["doubled"].copy is False
    assert mappings["first"].copy is True
    # The copy INSERT lists neither generated column
    copy_sql = next(sql for sql in plan.sqls if sql.startswith("INSERT INTO"))
    assert '"full"' not in copy_sql.split("FROM")[0]
    assert '"doubled"' not in copy_sql.split("FROM")[0]
    plan.execute()
    assert list(table.rows) == [
        {
            "id": 1,
            "firstname": "Ada",
            "last": "Lovelace",
            "full": "Ada Lovelace",
            "doubled": 2,
        }
    ]
    table.insert({"id": 2, "firstname": "Grace", "last": "Hopper"})
    assert list(table.rows)[1]["full"] == "Grace Hopper"
    assert list(table.rows)[1]["doubled"] == 4


def test_transform_previously_dropped_generated_columns_silently(fresh_db):
    # Regression: before plan_transform, generated columns were invisible to
    # PRAGMA table_info and a transform silently deleted them.
    fresh_db.conn.executescript(
        "CREATE TABLE g (id INTEGER PRIMARY KEY, v TEXT AS (upper(id) || '!'));"
    )
    fresh_db.table("g").insert({"id": 1})
    fresh_db.table("g").transform(types={"id": int})
    row = fresh_db.conn.execute("SELECT id, v FROM g").fetchone()
    assert row == (1, "1!")


def test_plan_generated_column_type_change_is_rejected(fresh_db):
    fresh_db.conn.executescript(
        "CREATE TABLE g (id INTEGER PRIMARY KEY, v TEXT AS (id));"
    )
    with pytest.raises(TransformError, match="generated column 'v'"):
        fresh_db.table("g").plan_transform(types={"v": int})


def test_plan_fts_shadow_tables_reported_and_triggers_survive(fresh_db):
    fresh_db.table("articles").insert_all(
        [{"id": 1, "title": "cat dog", "body": "hello"}], pk="id"
    )
    fresh_db.table("articles").enable_fts(["title", "body"], create_triggers=True)
    plan = fresh_db.table("articles").plan_transform(types={"id": int})
    assert "articles_fts" in plan.fts_virtual_tables
    assert {
        "articles_fts_data",
        "articles_fts_docsize",
        "articles_fts_idx",
        "articles_fts_config",
    }.issubset(set(plan.fts_shadow_tables))
    assert {trigger.name for trigger in plan.triggers_kept} == {
        "articles_ai",
        "articles_ad",
        "articles_au",
    }
    assert any("FTS" in warning for warning in plan.warnings)
    plan.execute()
    # Sync still works after a transform that keeps the FTS columns
    fresh_db.table("articles").insert({"id": 2, "title": "fish", "body": "water"})
    assert [row["title"] for row in fresh_db.table("articles").search("fish")] == [
        "fish"
    ]


def test_plan_fts_indexed_column_rename_rewrites_trigger_and_kept_search(fresh_db):
    # The FTS trigger inserts by FTS column position, so rewriting new.body to
    # new.body2 keeps the FTS index populated with the renamed column's data.
    fresh_db.table("articles").insert_all([{"id": 1, "body": "hello world"}], pk="id")
    fresh_db.table("articles").enable_fts(["body"], create_triggers=True)
    plan = fresh_db.table("articles").plan_transform(rename={"body": "body2"})
    (insert_trigger,) = (t for t in plan.triggers_kept if t.name == "articles_ai")
    assert 'new."body2"' in insert_trigger.recreated_sql
    plan.execute()
    fresh_db.table("articles").insert({"id": 2, "body2": "kitten"})
    assert [row["body2"] for row in fresh_db.table("articles").search("kitten")] == [
        "kitten"
    ]


def test_plan_execute_renames_and_recreates_index(populated_db):
    table = populated_db.table("books")
    plan = table.plan_transform(rename={"title": "book_title"})
    plan.execute(table=table)
    assert table.columns_dict == {
        "id": int,
        "book_title": str,
        "author_id": int,
    }
    assert list(table.rows) == [
        {"id": 1, "book_title": "Reality is Broken", "author_id": 1}
    ]
    # Index and foreign key survived
    assert [index.name for index in table.indexes if index.origin != "pk"] == [
        "ix_books_title"
    ]
    assert table.foreign_keys == [
        ForeignKey(
            table="books",
            column="author_id",
            other_table="authors",
            other_column="id",
        )
    ]
    # Trigger survived and works against the renamed column
    table.insert({"id": 2, "book_title": "Second", "author_id": None})
    assert list(populated_db.table("trigger_log").rows) == [{"msg": "echo-Second"}]


def test_plan_execute_with_database_only(populated_db):
    plan = populated_db.table("books").plan_transform(drop={"author_id"})
    returned = plan.execute(populated_db)
    assert returned.name == "books"
    assert "author_id" not in returned.columns_dict


def test_transform_and_plan_execute_are_symmetric(populated_db):
    # transform() and plan_transform(...).execute() apply identical SQL
    def clone_db():
        clone = Database(memory=True)
        for line in populated_db.conn.iterdump():
            clone.conn.execute(line)
        return clone

    db_a = clone_db()
    db_b = clone_db()
    db_a.table("books").transform(rename={"title": "book_title"})
    plan = db_b.table("books").plan_transform(rename={"title": "book_title"})
    plan.execute(table=db_b.table("books"))
    for name in ("books", "authors"):
        assert db_a.table(name).schema == db_b.table(name).schema
    assert list(db_a.table("books").rows) == list(db_b.table("books").rows)


def test_transform_sql_matches_plan_sqls_with_suffix(populated_db):
    table = populated_db.table("books")
    kwargs = {"rename": {"title": "book_title"}, "tmp_suffix": "plan"}
    assert (
        table.transform_sql(**kwargs)
        == table.plan_transform(rename={"title": "book_title"}).sqls
    )


def test_failed_plan_step_rolls_back_entire_database(populated_db):
    table = populated_db.table("books")
    original_schema = table.schema
    original_rows = list(table.rows)
    original_trigger = table.triggers_dict
    original_index = list(
        populated_db.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='books'"
        )
    )

    plan = table.plan_transform(rename={"title": "book_title"})
    # Corrupt the trigger-recreation step so the failure happens late, after
    # the old table has already been dropped inside the transaction.
    steps = list(plan.steps)
    trigger_position = next(
        i for i, step in enumerate(steps) if step.sql.startswith("CREATE TRIGGER")
    )
    steps.insert(
        trigger_position,
        TransformStep(
            index=999,
            sql='INSERT INTO "books"("no_such_column") VALUES (1);',
            description="injected failure",
        ),
    )
    broken = dataclasses.replace(plan, steps=tuple(steps))
    with pytest.raises(sqlite3.OperationalError, match="no_such_column"):
        broken.execute(table=table)

    # Original table, rows, trigger and index are all back
    assert table.schema == original_schema
    assert list(table.rows) == original_rows
    assert table.triggers_dict == original_trigger
    assert (
        list(
            populated_db.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='books'"
            )
        )
        == original_index
    )
    # No temporary table lingers
    assert (
        populated_db.conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name LIKE '%new%'"
        ).fetchone()[0]
        == 0
    )
    # Connection pragma state was restored
    assert populated_db.conn.execute("PRAGMA foreign_keys").fetchone()[0] in (0, 1)
    assert populated_db.conn.execute("PRAGMA legacy_alter_table").fetchone()[0] == 0


def test_copy_failure_rolls_back_table_and_rows(fresh_db):
    fresh_db.table("dogs").insert_all(
        [{"id": 1, "name": "Cleo"}, {"id": 2, "name": None}], pk="id"
    )
    table = fresh_db.table("dogs")
    schema = table.schema
    rows = list(table.rows)
    with pytest.raises(sqlite3.IntegrityError):
        table.transform(not_null={"name"})
    assert table.schema == schema
    assert list(table.rows) == rows
    assert (
        fresh_db.conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name LIKE '%new%'"
        ).fetchone()[0]
        == 0
    )


def test_failed_transform_restores_foreign_keys_pragma(fresh_db):
    fresh_db.conn.execute("PRAGMA foreign_keys=ON")
    fresh_db.table("dogs").insert_all([{"id": 1, "name": None}], pk="id")
    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.table("dogs").transform(not_null={"name"})
    assert fresh_db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert fresh_db.conn.execute("PRAGMA legacy_alter_table").fetchone()[0] == 0


def test_failed_transform_restores_legacy_alter_table_and_views(fresh_db):
    # The most dangerous rollback corner: PRAGMA legacy_alter_table=ON inside
    # the transform survives a ROLLBACK. If it is left on, a later RENAME
    # would silently rewrite view definitions.
    fresh_db.table("dogs").insert({"id": 1, "name": None}, pk="id")
    fresh_db.conn.execute("CREATE VIEW v_dogs AS SELECT * FROM dogs")
    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.table("dogs").transform(not_null={"name"})
    assert fresh_db.conn.execute("PRAGMA legacy_alter_table").fetchone()[0] == 0
    fresh_db.conn.execute('ALTER TABLE dogs RENAME TO "canines"')
    view_sql = fresh_db.conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='v_dogs'"
    ).fetchone()[0]
    assert "dogs" in view_sql


def test_foreign_key_violation_at_commit_rolls_back(fresh_db):
    fresh_db.conn.execute("PRAGMA foreign_keys=ON")
    fresh_db.table("authors").insert({"id": 3, "name": "Tina"}, pk="id")
    fresh_db.table("books").insert(
        {"id": 1, "title": "Book", "author_id": 3},
        pk="id",
        foreign_keys={"author_id"},
    )
    schema = fresh_db.table("authors").schema
    with pytest.raises(Exception):
        fresh_db.table("authors").transform(rename={"id": "id2"})
    assert fresh_db.table("authors").schema == schema


def test_plan_temporary_table_collision_is_rejected(fresh_db):
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"}, pk="id")
    fresh_db.conn.execute('CREATE TABLE "dogs_new_plan" (x)')
    with pytest.raises(TransformError, match="temporary table 'dogs_new_plan'"):
        fresh_db.table("dogs").plan_transform(rename={"name": "full_name"})


def test_plan_transform_requires_existing_table(fresh_db):
    with pytest.raises(ValueError, match="doesn't exist"):
        fresh_db.table("ghost").plan_transform(rename={"x": "y"})


def test_plan_render_is_readable_and_lists_loss_and_steps(populated_db):
    populated_db.conn.execute("CREATE TABLE logs(msg)")
    populated_db.conn.execute("""
        CREATE TRIGGER books_log AFTER INSERT ON books
        WHEN title IS NOT NULL
        BEGIN
            INSERT INTO logs(msg) VALUES (new.title);
        END;
        """)
    plan = populated_db.table("books").plan_transform(rename={"title": "headline"})
    rendered = str(plan)
    assert "Transform plan for table 'books'" in rendered
    assert "RENAME COLUMN 'title' -> 'headline'" in rendered
    assert "LOST TRIGGER 'books_log'" in rendered
    assert rendered.count("CREATE TRIGGER") == 1
    assert "recreate the trigger manually" in rendered
    assert "INSERT INTO" in rendered


def test_plan_keep_table_drops_indexes_and_triggers_from_backup(fresh_db):
    fresh_db.conn.execute("CREATE TABLE logs(msg)")
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"}, pk="id")
    fresh_db.conn.execute("CREATE INDEX ix_dogs_name ON dogs(name)")
    fresh_db.conn.execute(
        "CREATE TRIGGER dogs_ai AFTER INSERT ON dogs "
        "BEGIN INSERT INTO logs(msg) VALUES('x'); END;"
    )
    plan = fresh_db.table("dogs").plan_transform(
        rename={"name": "full_name"}, keep_table="dogs_backup"
    )
    assert plan.keep_table == "dogs_backup"
    # The backup swap renames the old table instead of dropping it
    assert any("RENAME TO" in sql and "dogs_backup" in sql for sql in plan.sqls)
    assert not any(sql.startswith("DROP TABLE") for sql in plan.sqls)
    plan.execute()
    assert fresh_db.table("dogs_backup").exists()
    # Indexes/triggers are recreated on the live table, not the backup
    assert [i.name for i in fresh_db.table("dogs").indexes if i.origin != "pk"] == [
        "ix_dogs_name"
    ]
    assert [t.name for t in fresh_db.table("dogs").triggers] == ["dogs_ai"]
    assert fresh_db.table("dogs_backup").triggers == []
    # Live table has the renamed column; backup keeps the old one
    assert "full_name" in fresh_db.table("dogs").columns_dict
    assert "name" in fresh_db.table("dogs_backup").columns_dict


def test_plan_execute_is_reusable_only_against_unchanged_schema(fresh_db):
    # A plan is executable, then executing again errors cleanly because the
    # temp table was consumed - but the database stays consistent.
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"}, pk="id")
    plan = fresh_db.table("dogs").plan_transform(rename={"name": "full_name"})
    plan.execute()
    # Rebuilding against the (already transformed) schema is a fresh plan
    second = fresh_db.table("dogs").plan_transform(types={"id": int})
    second.execute()
    assert fresh_db.table("dogs").columns_dict == {"id": int, "full_name": str}


def test_plan_foreign_key_pragma_flags(fresh_db):
    fresh_db.table("dogs").insert({"id": 1}, pk="id")
    fresh_db.conn.execute("PRAGMA foreign_keys=ON")
    plan = fresh_db.table("dogs").plan_transform(types={"id": int})
    assert plan.disables_foreign_keys is True
    assert plan.defers_foreign_keys is False
    # Inside an open transaction it defers instead
    fresh_db.conn.execute("PRAGMA foreign_keys=ON")
    fresh_db.execute("BEGIN")
    try:
        nested = fresh_db.table("dogs").plan_transform(rename={"id": "id2"})
        assert nested.defers_foreign_keys is True
        assert nested.disables_foreign_keys is False
    finally:
        fresh_db.execute("ROLLBACK")


def test_plan_step_descriptions_cover_each_sql(fresh_db):
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo", "age": "5"}, pk="id")
    fresh_db.conn.execute("CREATE INDEX ix_dogs_name ON dogs(name)")
    fresh_db.conn.execute("CREATE TABLE logs(msg)")
    fresh_db.conn.execute(
        "CREATE TRIGGER dogs_ai AFTER INSERT ON dogs "
        "BEGIN INSERT INTO logs(msg) VALUES(new.name); END;"
    )
    plan = fresh_db.table("dogs").plan_transform(rename={"name": "full_name"})
    assert plan.steps[0].description == "Create replacement table"
    assert plan.steps[1].description.startswith("Copy data")
    descriptions = {step.description for step in plan.steps}
    assert "Recreate index on the rebuilt table" in descriptions
    assert "Recreate trigger on the rebuilt table" in descriptions
    assert "Drop the old table" in descriptions
    assert "Move the replacement table into place" in descriptions
    # Steps are numbered sequentially from 0
    assert [step.index for step in plan.steps] == list(range(len(plan.steps)))


def test_cli_plan_is_dry_run(db_path):
    from click.testing import CliRunner
    from sqlite_utils import cli

    db = Database(db_path)
    db.table("dogs").insert({"id": 1, "name": "Cleo", "age": "5"}, pk="id")
    db.conn.execute("CREATE INDEX ix_dogs_name ON dogs(name)")
    original_schema = db.table("dogs").schema

    result = CliRunner().invoke(
        cli.cli,
        ["transform", db_path, "dogs", "--rename", "name", "full_name", "--plan"],
    )
    assert result.exit_code == 0, result.output
    assert "Transform plan for table 'dogs'" in result.output
    assert 'CREATE TABLE "dogs_new_plan"' in result.output
    assert "RENAME COLUMN 'name' -> 'full_name'" in result.output
    assert "Recreate index on the rebuilt table" in result.output
    # Nothing was executed
    assert db.table("dogs").schema == original_schema
    assert not db.table("dogs_new_plan").exists()

    # --sql and --plan are mutually exclusive
    rejected = CliRunner().invoke(
        cli.cli,
        ["transform", db_path, "dogs", "--plan", "--sql"],
    )
    assert rejected.exit_code != 0
    assert "--plan" in rejected.output or "cannot be used together" in rejected.output


def test_plan_transform_rejects_virtual_table_with_diagnostic(fresh_db):
    fresh_db.table("articles").insert_all([{"id": 1, "body": "hello"}], pk="id")
    fresh_db.table("articles").enable_fts(["body"])
    with pytest.raises(TransformError, match="FTS5 virtual table"):
        fresh_db.table("articles_fts").plan_transform(types={"body": int})


def test_plan_execute_inside_outer_transaction_rolls_back_with_it(fresh_db):
    fresh_db.table("dogs").insert_all(
        [{"id": 1, "name": "Cleo"}, {"id": 2, "name": "Pancake"}], pk="id"
    )
    try:
        with fresh_db.atomic():
            fresh_db.table("dogs").plan_transform(
                rename={"name": "full_name"}
            ).execute()
            assert fresh_db.table("dogs").columns_dict == {
                "id": int,
                "full_name": str,
            }
            raise RuntimeError("abort the outer transaction")
    except RuntimeError:
        pass
    # The outer rollback undoes the whole transform
    assert fresh_db.table("dogs").columns_dict == {"id": int, "name": str}
    assert list(fresh_db.table("dogs").rows) == [
        {"id": 1, "name": "Cleo"},
        {"id": 2, "name": "Pancake"},
    ]


def test_self_referential_foreign_key_survives_transform(fresh_db):
    fresh_db.conn.executescript("""
        CREATE TABLE emp(
            id INTEGER PRIMARY KEY,
            name TEXT,
            manager_id INTEGER REFERENCES emp(id)
        );
        """)
    fresh_db.table("emp").insert({"id": 1, "name": "Boss", "manager_id": None})
    fresh_db.table("emp").insert({"id": 2, "name": "Worker", "manager_id": 1})
    plan = fresh_db.table("emp").plan_transform(rename={"name": "full_name"})
    (fk_plan,) = plan.foreign_keys
    assert fk_plan.other_table == "emp"
    assert fk_plan.columns == ("manager_id",)
    plan.execute()
    assert fresh_db.table("emp").foreign_keys == [
        ForeignKey(
            table="emp",
            column="manager_id",
            other_table="emp",
            other_column="id",
        )
    ]


def test_plan_can_drop_and_rename_generated_columns(fresh_db):
    fresh_db.conn.executescript("""
        CREATE TABLE g (
            id INTEGER PRIMARY KEY,
            first TEXT,
            full TEXT GENERATED ALWAYS AS (first || '!') VIRTUAL,
            keepme TEXT AS (id) STORED
        );
        """)
    fresh_db.table("g").insert({"id": 1, "first": "a"})
    plan = fresh_db.table("g").plan_transform(
        drop={"full"}, rename={"first": "firstname"}
    )
    assert [column.name for column in plan.columns_after] == [
        "id",
        "firstname",
        "keepme",
    ]
    # The surviving generated expression follows the renamed input
    keepme = {column.name: column for column in plan.columns_after}["keepme"]
    assert keepme.generated.expression == "id"
    plan.execute()
    assert fresh_db.conn.execute("SELECT * FROM g").fetchone() == (1, "a", "1")
    assert "GENERATED" not in fresh_db.table("g").schema
    assert "AS (id) STORED" in fresh_db.table("g").schema


def test_plan_generated_column_not_in_copy_sql_even_when_renamed(fresh_db):
    fresh_db.conn.executescript(
        "CREATE TABLE g (id INTEGER PRIMARY KEY, v TEXT AS (upper(id) || 'z'));"
    )
    fresh_db.table("g").insert({"id": 1})
    plan = fresh_db.table("g").plan_transform(rename={"v": "upper_v"})
    copy_sql = next(sql for sql in plan.sqls if sql.startswith("INSERT INTO"))
    assert '"upper_v"' not in copy_sql.split("FROM")[0]
    assert '"v"' not in copy_sql.split("FROM")[0]
    mapping = {m.old_name: m for m in plan.column_mapping}["v"]
    assert mapping.copy is False
    assert mapping.new_name == "upper_v"
    plan.execute()
    assert fresh_db.conn.execute("SELECT upper_v FROM g").fetchone()[0] == "1z"
