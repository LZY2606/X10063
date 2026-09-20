# Changelog

## Unreleased — `table.transform()` dry-run plans and atomic execution

### What was added

- **`Table.plan_transform(...)`** performs the same argument validation as
  `transform()` but returns a `sqlite_utils.db.TransformPlan` instead of
  writing anything. The plan exposes:
  - `sql_steps` / `sql` — the exact ordered SQL statements `transform()` runs;
  - `columns` — one `sqlite_utils.db.ColumnMapping` per output column
    (source → destination, generated columns, `TEXT -> INTEGER` type changes);
  - `dropped_columns`, `new_columns`;
  - `indexes_preserved`, `indexes_rebuilt`, `indexes_lost`;
  - `triggers_preserved`, `triggers_rebuilt`, `triggers_lost`;
  - `foreign_keys_preserved`, `foreign_keys_added`, `foreign_keys_removed`
    (composite keys included);
  - `dependencies` (FTS virtual + shadow tables and sync triggers) and
    `warnings`.
  Plans render as human-readable text via `str(plan)`.
- **`Table.transform(plan=plan)`** executes a previously built plan. Passing
  `plan=` with any other transform argument, or a plan built for another
  table, raises `ValueError`.
- **`sqlite-utils transform --plan`** prints the plan without executing it
  (symmetric CLI entry point alongside the existing `--sql`).
- `transform_sql()` is retained with its existing signature and behaviour
  (random temp-table suffix by default, `tmp_suffix=` to override) and now
  returns exactly the statements the plan and `transform()` execute.

### Implementation choices

- `transform_sql()`, `plan_transform()` and `transform()` all share one
  internal builder (`_build_transform`) returning a structured result, so the
  planned SQL and the executed SQL can never drift apart — the "two symmetric
  entries" are literally the same code path.
- Dry-run determinism: `plan_transform()` builds the replacement table as
  `<table>_new` instead of `<table>_new_<os.urandom()>`. Everything else in a
  plan is already derived from live schema introspection (no timestamps,
  counters or random values), so plans are byte-for-byte reproducible.
  Explicit `tmp_suffix=` still overrides the name.
- Atomicity was already provided by the existing `with self.db.atomic():`
  transaction (SQLite supports transactional DDL). The transform also runs
  inside the same transaction when nested in an outer `with db.atomic()` via a
  savepoint, so a mid-transform failure rolls back to the savepoint while the
  outer transaction survives.
- Generated columns are parsed from the stored `CREATE TABLE` text
  (`parse_generated_columns`) because `PRAGMA table_info` hides them; their
  type, `NOT NULL`/`CHECK`/`UNIQUE` constraints, storage kind (`VIRTUAL` /
  `STORED`) and expression are reconstructed structurally, with column
  references in the expression rewritten through the existing
  `rewrite_check_expression()` tokenizer. Generated columns are excluded from
  the `INSERT ... SELECT` copy (SQLite computes them) and cannot be targeted
  as a primary key or a foreign key.
- Triggers are recreated after the table swap using their stored SQL (with
  renamed column references rewritten), instead of being silently deleted by
  `DROP TABLE`. With `keep_table=`, old triggers are dropped so they do not
  remain attached to the backup table.
- Partial/expression indexes are only rejected when a renamed or dropped
  column is actually referenced by the index key or its `WHERE` predicate
  (checked via `check_references_identifier`); previously *any* rename/drop
  rejected them, and unrelated changes now leave the index untouched.
- FTS handling uses a content-table scan (both the bracketed and quoted
  `content=` spellings, FTS4 and FTS5) to report dependencies; renaming or
  dropping an FTS-indexed column raises `TransformError` before any write.

### Gaps in the previous coverage that are now covered

- **Generated columns were silently dropped** by a rebuild (they are absent
  from `PRAGMA table_info`, so the copy logic never saw them).
- **Triggers were silently dropped** by the `DROP TABLE` step.
- There was no way to inspect the impact of a transform before running it.
- Partial/expression indexes had no way to survive a transform that did not
  touch the columns they reference.
- FTS sync triggers and shadow-table relationships were invisible to callers,
  and renaming an indexed column left a broken FTS index mid-transaction.

### Regression guards for neighbouring semantics

- The full pre-existing suite (1488 tests) passes unchanged; the new behaviour
  is purely additive (`plan=`, `--plan`) except for cases that previously
  caused silent data/schema loss, which now raise `TransformError`.
- Default argument values and ordering of `transform()` / `transform_sql()`
  are unchanged: no-argument calls still reformat the schema, `pk=DEFAULT`
  still preserves the existing key, `strict=None` still preserves strict mode,
  and `transform_sql()` keeps its random suffix.
- `transform_sql()` output for existing callers is unchanged except for the
  newly appended trigger-recreation statements (which restore objects that
  were previously lost).
- Existing errors keep their diagnostic context: the incompatible-index,
  CHECK, and transaction errors still embed the object name and original SQL,
  and the new generated-column/trigger/FTS errors follow the same pattern,
  ending in "No changes have been applied to this table."
- `PRAGMA legacy_alter_table` is restored inside the transaction, so it also
  returns to its prior value after a rolled-back failure.

### Most dangerous counter-example and its regression test

**A generated column was silently destroyed by a plain, argument-less
`table.transform()`** — the operation the docs present as a harmless schema
reformat. Because `PRAGMA table_info` omits generated columns, the rebuild
created a table without the column and `INSERT ... SELECT` simply did not copy
it: the derived data and its expression vanished with no error (worse,
`VIRTUAL` columns lose data and `STORED` columns lose both definition and
stored bytes). The symmetric failure mode for triggers was just as bad:
`DROP TABLE` deleted every trigger, including FTS sync triggers.

The regression tests are:

- `test_transform_preserves_virtual_generated_column`,
  `test_transform_preserves_stored_generated_column_with_constraints`,
  `test_transform_renames_source_column_in_generated_expression`,
  `test_transform_renames_generated_column_itself`,
  `test_transform_generated_column_is_idempotent`,
  `test_drop_column_referenced_by_generated_column_errors` in
  `tests/test_plan_transform.py`;
- `test_transform_preserves_triggers_across_rebuild`,
  `test_drop_column_referenced_by_trigger_errors`,
  `test_drop_unrelated_column_keeps_trigger` for the trigger analogue;
- `test_transform_with_fts_keeps_search_working` and
  `test_transform_rename_fts_indexed_column_errors` for FTS;
- `test_failed_transform_rolls_back_entire_table`,
  `test_failed_plan_execution_rolls_back`,
  `test_transform_plan_and_direct_execution_equivalent` and
  `test_legacy_alter_table_pragma_restored_after_failure` pin atomic recovery
  and the plan/execute symmetry.

### Known limitations surfaced (not silently mis-handled)

- A generated column declared with a non-default `COLLATE` raises
  `TransformError` rather than dropping the collation; recreating it requires
  column-level collation rendering the builder does not currently emit.
- A view that references a renamed/dropped column stays defined and errors on
  its next query (unchanged SQLite behaviour, already documented).
