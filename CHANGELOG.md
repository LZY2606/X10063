# Changelog

This file records the implementation work for **"sqlite-utils: dry-run plans
and atomic execution for `table.transform()`"**. The canonical project
changelog remains `docs/changelog.rst`; the notes here focus on the design
choices, the coverage gaps this change closes, the adjacent semantics guarded
against regression, and the single most dangerous counter-example for
SQLite schema transforms.

## What was added

- **`Table.plan_transform(...) -> TransformPlan`** — a dry-run that accepts
  exactly the same keyword arguments as `transform()` and returns a
  structured, executable plan. It only reads schema metadata; no tables,
  indexes, triggers or pragma values are created or modified. The
  replacement table always uses the fixed name `"<table>_new_plan"`, so a
  plan is byte-for-byte reproducible on the same schema and arguments, both
  inside one process and across fresh connections/databases.
- **`TransformPlan.execute(db=None, table=None)`** — applies every planned
  step inside the existing `Database.atomic()` transaction. Any failure
  (replacement-table creation, the `INSERT ... SELECT` copy, the drop/swap
  renames, index recreation, trigger recreation or the final
  `foreign_key_check`) rolls the database back to the original table. A plan
  remembers the database that built it, so `plan.execute()` works directly;
  it can also be run with `plan.execute(db)` or `plan.execute(table=...)`.
- The plan surfaces the full blast radius before execution:
  - `columns_before`, `columns_after`, `column_mapping` (dropped/renamed,
    copied vs. recomputed, per-column copy `SELECT` expression),
  - `indexes` / `indexes_kept` / `indexes_lost` with the exact recreated
    `CREATE INDEX` SQL,
  - `triggers` / `triggers_kept` / `triggers_lost` with recreated SQL, the
    original SQL for manual recovery, referenced columns and a diagnostic
    reason,
  - `foreign_keys`, `foreign_keys_dropped` (single-column and composite),
  - `fts_virtual_tables`, `fts_shadow_tables` and a `warnings` tuple,
  - `disables_foreign_keys` / `defers_foreign_keys`, the ordered `steps`
    (each with a human-readable description), and `str(plan)` rendering.
- **CLI:** `sqlite-utils transform ... --plan` prints the annotated plan
  without executing it; it is mutually exclusive with `--sql`.
- `Table.columns_all` — column introspection that includes generated
  columns (which `PRAGMA table_info` omits) via `PRAGMA table_xinfo`, in
  `CREATE TABLE` order.

## Gaps in the previous coverage that this closes

1. **Triggers were silently destroyed.** `DROP TABLE` drops a table's
   triggers; the old code rebuilt indexes but never rebuilt triggers, so a
   plain `transform(rename=...)` left a table with no triggers and no
   warning. The builder now plans every trigger on the table:
   - Triggers are recreated verbatim when no relevant column changes.
   - `NEW."col"` / `OLD."col"` references and self-table `UPDATE ... SET`
     targets are rewritten mechanically when a column is renamed (this is
     how FTS sync triggers, `enable_counts()` triggers and hand-written
     triggers keep working).
   - A trigger that references a dropped column, or references a renamed
     column through an unqualified identifier that cannot be safely
     disambiguated (`WHEN name IS NOT NULL`, function arguments, another
     table's `SET` target), is reported as **lost** with the original SQL
     and a reason instead of being rewritten into a trigger that fails at
     fire time.
   - With `keep_table=`, triggers are explicitly detached from the backup
     and recreated on the live table (previously they silently stayed on
     the frozen backup).
2. **Generated columns were silently deleted.** They are invisible to
   `PRAGMA table_info`, so `transform()` produced a `CREATE TABLE` without
   them and copied as if they did not exist. They are now parsed from the
   stored schema (`parse_generated_columns`), preserved across transforms,
   their expressions rewritten when an input column is renamed, their
   values recomputed (never copied into the replacement table), and an
   attempt to set `types=`/`defaults=`/`NOT NULL` on a generated column
   raises `TransformError` up front.
3. **FTS shadow tables were invisible to the transform.** The `_fts`,
   `_fts_data`, `_fts_idx`, `_fts_docsize`, `_fts_config` objects and their
   content-table triggers are now enumerated and warned about; sync
   triggers are rewritten and recreated so a column rename keeps the index
   populated, while a change that genuinely breaks FTS (dropping an indexed
   column) reports the trigger as lost and warns that the FTS index must be
   rebuilt with `enable_fts()`/`populate_fts()`.
4. **No way to preview a transform.** `transform_sql()` returned only a
   list of strings with a random temporary-table suffix; callers could not
   see column mapping, index/trigger fate or foreign-key handling, and the
   output was not reproducible.
5. **Pragma state leak on rollback.** `PRAGMA legacy_alter_table` is not
   transactional: the in-plan `...=OFF` step is undone by a `ROLLBACK`,
   leaving the connection with legacy rename semantics, so a *later*
   `ALTER TABLE ... RENAME` would silently rewrite view definitions. The
   executor now restores `legacy_alter_table`, `foreign_keys` and
   `defer_foreign_keys` around the transaction on every exit path.

## Implementation choices

- **One builder, three symmetric entry points.** `transform()`,
  `transform_sql(tmp_suffix=...)` and `plan_transform()` all run through
  `_build_transform_plan()`. `transform_sql()` keeps its random suffix
  default and exact string output (every pinned SQL assertion in the
  existing suite still passes); only the plan uses the fixed, reproducible
  `_new_plan` suffix. The executor is shared by direct execution and
  `plan.execute()`, so planned and immediate execution cannot drift apart.
- **Planning never writes.** All pre-flight validation (missing table,
  unsupported STRICT, partial/expression index + rename/drop, index on a
  dropped column, destructive `ON DELETE` foreign key inside an open
  transaction, temp-table name collision, CHECK on a dropped column) happens
  while reading schema, before returning a plan.
- **Conservative trigger rewriting.** Mechanical token-level rewriting
  (reusing the create-table lexer, with trivia preserved) is only applied
  where the target is unambiguous: qualified `NEW.`/`OLD.` column
  references and the self table's `UPDATE ... SET` list. Parenthesized
  `INSERT` column lists are deliberately not treated as this-table column
  references, which is exactly what lets an FTS trigger be rewritten
  correctly while an external table's column of the same name is not.
- **Generated columns are preserved, never coerced.** `NOT NULL`, `DEFAULT`
  and inline `REFERENCES` are stripped for them inside `create_table_sql`,
  they are excluded from the copy statement, and `AUTOINCREMENT` can never
  attach to them.
- **Atomicity relies on the existing `Database.atomic()` semantics**
  (savepoint when a transaction is already open, `BEGIN` otherwise), plus
  connection-pragma management outside that block because pragma changes
  survive rollbacks.

## Adjacent semantics with regression protection

These behaviors were intentionally left byte-identical and are pinned by
the existing suite plus new tests:

- `transform_sql()` strings (temporary suffix handling, quoting, step order,
  `NULLIF(col, '')` empty-string coercion for TEXT→numeric,
  `legacy_alter_table` dance, `sqlite_sequence` preservation).
- The pre-existing hard errors for partial/expression indexes and indexes
  on dropped columns (now also raised at plan time, with zero writes).
- `PRAGMA foreign_keys` handling: disable-and-recheck when no transaction is
  open, defer + refuse (with the existing `TransactionError` message) when a
  transaction is open and destructive incoming foreign keys exist.
- Casing resolution (`resolve_casing`), quoted/keyword/unusual identifiers
  (a table literally named `child"x`, columns with spaces), and composite
  foreign keys survive renames.
- `keep_table=` semantics (old table kept, views untouched, indexes/triggers
  live on the new table, not the backup).
- Implicit UNIQUE-constraint indexes are represented in `CREATE TABLE`, not
  re-emitted as explicit indexes.

## The most dangerous counter-example

The canonical SQLite table-rebuild hazard is:

> The transform drops the old table halfway through a multi-statement
> sequence, and a **later** step fails. Without a single transaction the
> replacement table is half-populated, the original table is gone, indexes
> and triggers are missing, and — because `PRAGMA legacy_alter_table` is
> connection-level and non-transactional — the connection is left with
> legacy rename semantics that silently rewrite unrelated view definitions
> on the next rename.

The sharpest version combines this with a trigger and foreign keys: drop
the old table, swap the new one into place, then fail while recreating an
index/trigger (or at the `foreign_key_check`) while `PRAGMA foreign_keys` is
on with an `ON DELETE CASCADE`-style relationship nearby.

It is pinned by:

- `test_failed_plan_step_rolls_back_entire_database` — injects a failing
  statement after the drop/swap and asserts the original schema, rows,
  trigger SQL, index SQL, the absence of any `*_new_*` table and pragma
  restoration.
- `test_failed_transform_restores_legacy_alter_table_and_views` — after a
  failed transform a subsequent rename must **not** rewrite the dependent
  view.
- `test_copy_failure_rolls_back_table_and_rows` and
  `test_failed_transform_restores_foreign_keys_pragma` — failure during the
  data copy (`NOT NULL`) restores table, rows and both pragmas.
- `test_foreign_key_violation_at_commit_rolls_back` — the pre-commit
  `foreign_key_check` failure restores the original table.
- `test_transform_on_delete_cascade_does_not_delete_records` and
  `test_transform_in_transaction_refuses_destructive_on_delete` (existing) —
  destructive incoming foreign keys neither cascade nor run unsafely.
