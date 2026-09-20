"""Helpers for parsing constraints from SQLite CREATE TABLE SQL.

SQLite does not expose CHECK constraints through a pragma, so preserving them
across a table rebuild requires reading ``sqlite_schema.sql``.  This module is
deliberately small, but it uses a real lexer: strings, quoted identifiers and
comments are opaque, every token retains its source span and malformed input is
reported instead of being silently under-parsed.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class Check:
    check: str
    name: str = ""
    column: str = ""
    options: list[Any] | None = None
    # Source details are excluded from equality and repr so callers can compare
    # semantic constraints while still having the original SQL available for
    # diagnostics or future lossless edits.
    sql: str = field(default="", compare=False, repr=False)
    start: int = field(default=-1, compare=False, repr=False)
    end: int = field(default=-1, compare=False, repr=False)


@dataclass(frozen=True)
class ColumnComments:
    before: str = ""
    after: str = ""


@dataclass(frozen=True)
class UniqueColumn:
    name: str
    collation: str = ""
    order: str = ""


@dataclass
class Unique:
    columns: tuple[UniqueColumn, ...]
    name: str = ""
    column: str = ""
    conflict: str = ""
    sql: str = field(default="", compare=False, repr=False)
    start: int = field(default=-1, compare=False, repr=False)
    end: int = field(default=-1, compare=False, repr=False)


@dataclass(frozen=True)
class GeneratedColumn:
    """A ``GENERATED ALWAYS AS (...)`` column parsed from a CREATE TABLE."""

    name: str
    declared_type: str
    expression: str
    storage: str  # "VIRTUAL" (the SQLite default) or "STORED"
    # Full trailing clause, starting at GENERATED/AS and excluding trivia, so
    # the column can be re-emitted without guessing at option order.
    clause: str


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    start: int
    end: int

    def is_keyword(self, keyword: str) -> bool:
        return self.kind == "word" and self.text.upper() == keyword


_PUNCTUATION = frozenset("(),.;+-*/%<>=!~|&?:")
_TRIVIA = frozenset(("whitespace", "comment"))
_TABLE_CONSTRAINT_KEYWORDS = frozenset(("PRIMARY", "UNIQUE", "CHECK", "FOREIGN"))
_OTHER_COLUMN_CONSTRAINT_KEYWORDS = frozenset(
    ("PRIMARY", "UNIQUE", "REFERENCES", "DEFAULT", "NOT", "COLLATE", "GENERATED")
)
_SQLITE_KEYWORDS = frozenset(
    (
        "ABORT",
        "ACTION",
        "ADD",
        "AFTER",
        "ALL",
        "ALTER",
        "ANALYZE",
        "AND",
        "AS",
        "ASC",
        "ATTACH",
        "AUTOINCREMENT",
        "BEFORE",
        "BEGIN",
        "BETWEEN",
        "BY",
        "CASCADE",
        "CASE",
        "CAST",
        "CHECK",
        "COLLATE",
        "COLUMN",
        "COMMIT",
        "CONFLICT",
        "CONSTRAINT",
        "CREATE",
        "CROSS",
        "CURRENT_DATE",
        "CURRENT_TIME",
        "CURRENT_TIMESTAMP",
        "DATABASE",
        "DEFAULT",
        "DEFERRABLE",
        "DEFERRED",
        "DELETE",
        "DESC",
        "DETACH",
        "DISTINCT",
        "DO",
        "DROP",
        "EACH",
        "ELSE",
        "END",
        "ESCAPE",
        "EXCEPT",
        "EXCLUDE",
        "EXCLUSIVE",
        "EXISTS",
        "EXPLAIN",
        "FAIL",
        "FALSE",
        "FILTER",
        "FOLLOWING",
        "FOR",
        "FOREIGN",
        "FROM",
        "FULL",
        "GENERATED",
        "GLOB",
        "GROUP",
        "GROUPS",
        "HAVING",
        "IF",
        "IGNORE",
        "IMMEDIATE",
        "IN",
        "INDEX",
        "INDEXED",
        "INITIALLY",
        "INNER",
        "INSERT",
        "INSTEAD",
        "INTERSECT",
        "INTO",
        "IS",
        "ISNULL",
        "JOIN",
        "KEY",
        "LEFT",
        "LIKE",
        "LIMIT",
        "MATCH",
        "MATERIALIZED",
        "NATURAL",
        "NO",
        "NOT",
        "NOTHING",
        "NOTNULL",
        "NULL",
        "NULLS",
        "OF",
        "OFFSET",
        "ON",
        "OR",
        "ORDER",
        "OTHERS",
        "OUTER",
        "OVER",
        "PARTITION",
        "PLAN",
        "PRAGMA",
        "PRECEDING",
        "PRIMARY",
        "QUERY",
        "RAISE",
        "RANGE",
        "RECURSIVE",
        "REFERENCES",
        "REGEXP",
        "REINDEX",
        "RELEASE",
        "RENAME",
        "REPLACE",
        "RESTRICT",
        "RETURNING",
        "RIGHT",
        "ROLLBACK",
        "ROW",
        "ROWS",
        "SAVEPOINT",
        "SELECT",
        "SET",
        "STRICT",
        "TABLE",
        "TEMP",
        "TEMPORARY",
        "THEN",
        "TIES",
        "TO",
        "TRANSACTION",
        "TRIGGER",
        "TRUE",
        "UNBOUNDED",
        "UNION",
        "UNIQUE",
        "UPDATE",
        "USING",
        "VACUUM",
        "VALUES",
        "VIEW",
        "VIRTUAL",
        "WHEN",
        "WHERE",
        "WINDOW",
        "WITH",
        "WITHOUT",
    )
)
_INTEGER_RE = re.compile(r"[+-]?(?:0[xX][0-9a-fA-F]+|[0-9]+)\Z")
_FLOAT_RE = re.compile(
    r"[+-]?(?:(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|"
    r"[0-9]+[eE][+-]?[0-9]+)\Z"
)


def _lex(sql: str) -> list[_Token]:
    tokens: list[_Token] = []
    i = 0
    while i < len(sql):
        start = i
        char = sql[i]
        if char.isspace():
            i += 1
            while i < len(sql) and sql[i].isspace():
                i += 1
            tokens.append(_Token("whitespace", sql[start:i], start, i))
            continue
        if sql.startswith("--", i):
            newline = sql.find("\n", i + 2)
            i = len(sql) if newline == -1 else newline + 1
            tokens.append(_Token("comment", sql[start:i], start, i))
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end == -1:
                raise ParseError("Unterminated SQL comment")
            i = end + 2
            tokens.append(_Token("comment", sql[start:i], start, i))
            continue
        if char in ("'", '"', "`"):
            quote = char
            i += 1
            while i < len(sql):
                if sql[i] == quote:
                    if i + 1 < len(sql) and sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            else:
                raise ParseError(f"Unterminated {quote} quoted token")
            kind = "string" if quote == "'" else "identifier"
            tokens.append(_Token(kind, sql[start:i], start, i))
            continue
        if char == "[":
            end = sql.find("]", i + 1)
            if end == -1:
                raise ParseError("Unterminated [ quoted identifier")
            i = end + 1
            tokens.append(_Token("identifier", sql[start:i], start, i))
            continue
        if char in _PUNCTUATION:
            i += 1
            tokens.append(_Token("punct", char, start, i))
            continue
        # SQLite accepts any character >= U+0080 in a bare identifier.  More
        # generally, consume until a lexical delimiter rather than relying on
        # Python's narrower definition of an alphanumeric character.
        i += 1
        while i < len(sql):
            if sql[i].isspace() or sql[i] in _PUNCTUATION or sql[i] in "'\"`[":
                break
            i += 1
        tokens.append(_Token("word", sql[start:i], start, i))
    return tokens


def _meaningful(tokens: list[_Token]) -> list[_Token]:
    return [token for token in tokens if token.kind not in _TRIVIA]


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] in ("'", '"', "`") and token[-1] == token[0]:
        return token[1:-1].replace(token[0] * 2, token[0])
    if len(token) >= 2 and token[0] == "[" and token[-1] == "]":
        return token[1:-1]
    return token


def _matching_paren(tokens: list[_Token], open_index: int) -> int:
    if tokens[open_index].text != "(":
        raise ParseError("Expected an opening parenthesis")
    depth = 0
    for index in range(open_index, len(tokens)):
        if tokens[index].text == "(":
            depth += 1
        elif tokens[index].text == ")":
            depth -= 1
            if depth == 0:
                return index
    raise ParseError("Unbalanced parentheses")


def _split_spans(sql: str, tokens: list[_Token]) -> list[tuple[str, int, int]]:
    if not tokens:
        return []
    items: list[tuple[str, int, int]] = []
    depth = 0
    start = tokens[0].start
    for token in tokens:
        if token.text == "(":
            depth += 1
        elif token.text == ")":
            depth -= 1
            if depth < 0:
                raise ParseError("Unbalanced parentheses")
        elif token.text == "," and depth == 0:
            raw = sql[start : token.start]
            item = raw.strip()
            if item:
                item_start = start + len(raw) - len(raw.lstrip())
                items.append((item, item_start, item_start + len(item)))
            start = token.end
    if depth:
        raise ParseError("Unbalanced parentheses")
    raw = sql[start : tokens[-1].end]
    item = raw.strip()
    if item:
        item_start = start + len(raw) - len(raw.lstrip())
        items.append((item, item_start, item_start + len(item)))
    return items


def _split_ranges(sql: str, tokens: list[_Token]) -> list[str]:
    return [item for item, _, _ in _split_spans(sql, tokens)]


def _strip_outer_parens(tokens: list[_Token]) -> list[_Token]:
    while tokens and tokens[0].text == "(":
        close = _matching_paren(tokens, 0)
        if close != len(tokens) - 1:
            break
        tokens = tokens[1:-1]
    return tokens


_NO_LITERAL = object()


def _literal_value(text: str) -> Any:
    tokens = _meaningful(_lex(text))
    if len(tokens) == 1 and tokens[0].kind == "string":
        return _unquote(tokens[0].text)
    raw = "".join(token.text for token in tokens)
    if raw.upper() == "NULL":
        return None
    if raw.upper() == "TRUE":
        return True
    if raw.upper() == "FALSE":
        return False
    if _INTEGER_RE.fullmatch(raw):
        try:
            return (
                int(raw, 16) if raw.lower().lstrip("+-").startswith("0x") else int(raw)
            )
        except ValueError:
            return _NO_LITERAL
    if _FLOAT_RE.fullmatch(raw):
        try:
            return float(raw)
        except ValueError:
            return _NO_LITERAL
    return _NO_LITERAL


def _ascii_fold(identifier: str) -> str:
    return identifier.translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )


def _parse_options(expression: str, column: str) -> list[Any] | None:
    tokens = _strip_outer_parens(_meaningful(_lex(expression)))
    if len(tokens) < 4:
        return None
    lhs = tokens[0]
    if lhs.kind not in ("word", "identifier"):
        return None
    if column and _ascii_fold(_unquote(lhs.text)) != _ascii_fold(column):
        return None
    if not tokens[1].is_keyword("IN") or tokens[2].text != "(":
        return None
    close = _matching_paren(tokens, 2)
    if close != len(tokens) - 1:
        return None
    inner = expression[tokens[2].end : tokens[close].start]
    inner_tokens = _lex(inner)
    if not _meaningful(inner_tokens):
        return []
    values = []
    for item in _split_ranges(inner, inner_tokens):
        value = _literal_value(item)
        if value is _NO_LITERAL:
            return None
        values.append(value)
    return values


def _check_after(
    item: str,
    tokens: list[_Token],
    check_index: int,
    name: str,
    column: str,
    constraint_start: int,
    base_offset: int,
) -> tuple[Check, int]:
    if check_index + 1 >= len(tokens) or tokens[check_index + 1].text != "(":
        raise ParseError("CHECK must be followed by a parenthesized expression")
    close = _matching_paren(tokens, check_index + 1)
    expression = item[tokens[check_index + 1].end : tokens[close].start].strip()
    source_start = tokens[constraint_start].start
    source_end = tokens[close].end
    return (
        Check(
            expression,
            name=name,
            column=column,
            options=_parse_options(expression, column),
            sql=item[source_start:source_end],
            start=base_offset + source_start,
            end=base_offset + source_end,
        ),
        close + 1,
    )


def _column_checks(
    item: str, tokens: list[_Token], column: str, base_offset: int
) -> list[Check]:
    checks: list[Check] = []
    pending_name = ""
    pending_start: int | None = None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token.text == "(":
            index = _matching_paren(tokens, index) + 1
            continue
        if token.is_keyword("CONSTRAINT"):
            if index + 1 >= len(tokens):
                raise ParseError("CONSTRAINT is missing its name")
            pending_name = _unquote(tokens[index + 1].text)
            pending_start = index
            index += 2
            continue
        if token.is_keyword("CHECK"):
            check, index = _check_after(
                item,
                tokens,
                index,
                pending_name,
                column,
                pending_start if pending_start is not None else index,
                base_offset,
            )
            checks.append(check)
            pending_name = ""
            pending_start = None
            continue
        if (
            token.kind == "word"
            and token.text.upper() in _OTHER_COLUMN_CONSTRAINT_KEYWORDS
        ):
            pending_name = ""
            pending_start = None
        index += 1
    return checks


def _table_body(create_sql: str) -> tuple[str, int] | None:
    all_tokens = _lex(create_sql)
    tokens = _meaningful(all_tokens)
    if not tokens or not tokens[0].is_keyword("CREATE"):
        raise ParseError("Expected CREATE TABLE")
    index = 1
    if index < len(tokens) and (
        tokens[index].is_keyword("TEMP") or tokens[index].is_keyword("TEMPORARY")
    ):
        index += 1
    if index < len(tokens) and tokens[index].is_keyword("VIRTUAL"):
        return None
    if index >= len(tokens) or not tokens[index].is_keyword("TABLE"):
        raise ParseError("Expected CREATE TABLE")
    index += 1
    if (
        index + 2 < len(tokens)
        and tokens[index].is_keyword("IF")
        and tokens[index + 1].is_keyword("NOT")
        and tokens[index + 2].is_keyword("EXISTS")
    ):
        index += 3
    if index >= len(tokens):
        raise ParseError("CREATE TABLE is missing its table name")
    index += 1
    if index + 1 < len(tokens) and tokens[index].text == ".":
        index += 2
    if index < len(tokens) and tokens[index].is_keyword("AS"):
        return None
    if index >= len(tokens) or tokens[index].text != "(":
        raise ParseError("CREATE TABLE is missing its column list")
    close = _matching_paren(tokens, index)
    trailing = tokens[close + 1 :]
    allowed_trailing = {"STRICT", "WITHOUT", "ROWID", ",", ";"}
    if any(token.text.upper() not in allowed_trailing for token in trailing):
        raise ParseError("Unexpected SQL after CREATE TABLE column list")

    body_start = tokens[index].end
    body_end = tokens[close].start
    return create_sql[body_start:body_end], body_start


def parse_checks(create_sql: str) -> list[Check]:
    """Return CHECK constraints from a valid SQLite CREATE TABLE statement."""
    body_info = _table_body(create_sql)
    if body_info is None:
        return []
    body, body_start = body_info
    body_tokens = _lex(body)
    checks: list[Check] = []
    for item, item_start, _ in _split_spans(body, body_tokens):
        item_tokens = _meaningful(_lex(item))
        if not item_tokens:
            continue
        item_index = 0
        constraint_name = ""
        if item_tokens[item_index].is_keyword("CONSTRAINT"):
            if len(item_tokens) < 2:
                raise ParseError("CONSTRAINT is missing its name")
            constraint_name = _unquote(item_tokens[1].text)
            item_index = 2
        head = item_tokens[item_index] if item_index < len(item_tokens) else None
        if (
            head
            and head.kind == "word"
            and head.text.upper() in _TABLE_CONSTRAINT_KEYWORDS
        ):
            if head.is_keyword("CHECK"):
                check, _ = _check_after(
                    item,
                    item_tokens,
                    item_index,
                    constraint_name,
                    "",
                    0,
                    body_start + item_start,
                )
                checks.append(check)
            continue
        column = _unquote(item_tokens[0].text)
        checks.extend(
            _column_checks(item, item_tokens, column, body_start + item_start)
        )
    return checks


def parse_autoincrement(create_sql: str) -> str | None:
    """Return the AUTOINCREMENT column from a valid CREATE TABLE statement."""
    body_info = _table_body(create_sql)
    if body_info is None:
        return None
    body, _ = body_info
    for item, _, _ in _split_spans(body, _lex(body)):
        item_tokens = _meaningful(_lex(item))
        if not item_tokens:
            continue
        head = item_tokens[0]
        if (
            head.kind == "word" and head.text.upper() in _TABLE_CONSTRAINT_KEYWORDS
        ) or head.is_keyword("CONSTRAINT"):
            continue
        column = _unquote(head.text)
        index = 1
        while index < len(item_tokens):
            token = item_tokens[index]
            if token.text == "(":
                index = _matching_paren(item_tokens, index) + 1
                continue
            if token.is_keyword("AUTOINCREMENT"):
                return column
            index += 1
    return None


_CONFLICT_ACTIONS = frozenset(("ROLLBACK", "ABORT", "FAIL", "IGNORE", "REPLACE"))


def _conflict_after(tokens: list[_Token], index: int) -> tuple[str, int]:
    if index >= len(tokens) or not tokens[index].is_keyword("ON"):
        return "", index
    if index + 2 >= len(tokens) or not tokens[index + 1].is_keyword("CONFLICT"):
        raise ParseError("ON after UNIQUE must be followed by CONFLICT and an action")
    action = tokens[index + 2].text.upper()
    if tokens[index + 2].kind != "word" or action not in _CONFLICT_ACTIONS:
        raise ParseError("Invalid UNIQUE ON CONFLICT action")
    return action, index + 3


def _unique_columns(
    item: str, tokens: list[_Token], open_index: int
) -> tuple[tuple[UniqueColumn, ...], int]:
    close = _matching_paren(tokens, open_index)
    inner = item[tokens[open_index].end : tokens[close].start]
    columns: list[UniqueColumn] = []
    for raw_column in _split_ranges(inner, _lex(inner)):
        column_tokens = _meaningful(_lex(raw_column))
        if not column_tokens or column_tokens[0].kind not in (
            "word",
            "identifier",
            "string",
        ):
            raise ParseError("UNIQUE constraint has an invalid column")
        name = _unquote(column_tokens[0].text)
        collation = ""
        order = ""
        index = 1
        if index < len(column_tokens) and column_tokens[index].is_keyword("COLLATE"):
            if index + 1 >= len(column_tokens):
                raise ParseError("COLLATE in UNIQUE constraint is missing its name")
            collation = _unquote(column_tokens[index + 1].text)
            index += 2
        if index < len(column_tokens) and (
            column_tokens[index].is_keyword("ASC")
            or column_tokens[index].is_keyword("DESC")
        ):
            order = column_tokens[index].text.upper()
            index += 1
        if index != len(column_tokens):
            raise ParseError("UNIQUE constraint has an invalid indexed column")
        columns.append(UniqueColumn(name, collation=collation, order=order))
    if not columns:
        raise ParseError("UNIQUE constraint must include at least one column")
    return tuple(columns), close + 1


def _column_uniques(
    item: str, tokens: list[_Token], column: str, base_offset: int
) -> list[Unique]:
    uniques: list[Unique] = []
    collation = ""
    collation_index = 1
    while collation_index < len(tokens):
        token = tokens[collation_index]
        if token.text == "(":
            collation_index = _matching_paren(tokens, collation_index) + 1
            continue
        if token.is_keyword("COLLATE"):
            if collation_index + 1 >= len(tokens):
                raise ParseError("COLLATE is missing its name")
            collation = _unquote(tokens[collation_index + 1].text)
            collation_index += 2
            continue
        collation_index += 1
    pending_name = ""
    pending_start: int | None = None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token.text == "(":
            index = _matching_paren(tokens, index) + 1
            continue
        if token.is_keyword("CONSTRAINT"):
            if index + 1 >= len(tokens):
                raise ParseError("CONSTRAINT is missing its name")
            pending_name = _unquote(tokens[index + 1].text)
            pending_start = index
            index += 2
            continue
        if token.is_keyword("UNIQUE"):
            source_start = tokens[
                pending_start if pending_start is not None else index
            ].start
            conflict, next_index = _conflict_after(tokens, index + 1)
            source_end = tokens[next_index - 1].end
            uniques.append(
                Unique(
                    (UniqueColumn(column, collation=collation),),
                    name=pending_name,
                    column=column,
                    conflict=conflict,
                    sql=item[source_start:source_end],
                    start=base_offset + source_start,
                    end=base_offset + source_end,
                )
            )
            pending_name = ""
            pending_start = None
            index = next_index
            continue
        if (
            token.kind == "word"
            and token.text.upper() in _OTHER_COLUMN_CONSTRAINT_KEYWORDS
        ):
            pending_name = ""
            pending_start = None
        index += 1
    return uniques


def parse_uniques(create_sql: str) -> list[Unique]:
    """Return column-level and table-level UNIQUE constraints."""
    body_info = _table_body(create_sql)
    if body_info is None:
        return []
    body, body_start = body_info
    uniques: list[Unique] = []
    for item, item_start, _ in _split_spans(body, _lex(body)):
        item_tokens = _meaningful(_lex(item))
        if not item_tokens:
            continue
        item_index = 0
        constraint_name = ""
        if item_tokens[item_index].is_keyword("CONSTRAINT"):
            if len(item_tokens) < 2:
                raise ParseError("CONSTRAINT is missing its name")
            constraint_name = _unquote(item_tokens[1].text)
            item_index = 2
        head = item_tokens[item_index] if item_index < len(item_tokens) else None
        if head and head.is_keyword("UNIQUE"):
            if (
                item_index + 1 >= len(item_tokens)
                or item_tokens[item_index + 1].text != "("
            ):
                raise ParseError("Table UNIQUE must be followed by a column list")
            columns, next_index = _unique_columns(item, item_tokens, item_index + 1)
            conflict, next_index = _conflict_after(item_tokens, next_index)
            if next_index != len(item_tokens):
                raise ParseError("Unexpected SQL after UNIQUE constraint")
            source_start = item_tokens[0].start
            source_end = item_tokens[next_index - 1].end
            uniques.append(
                Unique(
                    columns,
                    name=constraint_name,
                    conflict=conflict,
                    sql=item[source_start:source_end],
                    start=body_start + item_start + source_start,
                    end=body_start + item_start + source_end,
                )
            )
            continue
        if (
            head
            and head.kind == "word"
            and head.text.upper() in _TABLE_CONSTRAINT_KEYWORDS
        ):
            continue
        column = _unquote(item_tokens[0].text)
        uniques.extend(
            _column_uniques(
                item,
                item_tokens,
                column,
                body_start + item_start,
            )
        )
    return uniques


def parse_column_comments(create_sql: str) -> dict[str, ColumnComments]:
    """Return comments immediately before and after each column definition."""
    body_info = _table_body(create_sql)
    if body_info is None:
        return {}
    body, _ = body_info
    comments: dict[str, ColumnComments] = {}
    for item, _, _ in _split_spans(body, _lex(body)):
        item_tokens = _meaningful(_lex(item))
        if not item_tokens:
            continue
        item_index = 0
        if item_tokens[item_index].is_keyword("CONSTRAINT"):
            item_index = 2
        head = item_tokens[item_index] if item_index < len(item_tokens) else None
        if (
            head
            and head.kind == "word"
            and head.text.upper() in _TABLE_CONSTRAINT_KEYWORDS
        ):
            continue
        column = _unquote(item_tokens[0].text)
        before = item[: item_tokens[0].start].strip()
        after = item[item_tokens[-1].end :].strip()
        if before or after:
            comments[column] = ColumnComments(before=before, after=after)
    return comments


def _is_identifier_token(tokens: list[_Token], index: int) -> bool:
    token = tokens[index]
    if index + 1 < len(tokens) and tokens[index + 1].text in ("(", "."):
        return False
    if index and (
        tokens[index - 1].is_keyword("COLLATE") or tokens[index - 1].is_keyword("AS")
    ):
        return False
    if token.kind == "identifier":
        return True
    if token.kind != "word" or token.text.upper() in _SQLITE_KEYWORDS:
        return False
    return True


def check_references_identifier(expression: str, identifier: str) -> bool:
    tokens = _meaningful(_lex(expression))
    folded = _ascii_fold(identifier)
    return any(
        _is_identifier_token(tokens, index)
        and _ascii_fold(_unquote(token.text)) == folded
        for index, token in enumerate(tokens)
    )


def sql_ends_in_line_comment(sql: str) -> bool:
    """Return True if appended SQL would be swallowed by a ``--`` comment."""
    tokens = _lex(sql)
    if not tokens:
        return False
    final = tokens[-1]
    return (
        final.kind == "comment"
        and final.text.startswith("--")
        and not final.text.endswith(("\n", "\r"))
    )


def _valid_bare_identifier(identifier: str) -> bool:
    if not identifier or identifier.upper() in _SQLITE_KEYWORDS:
        return False
    first = identifier[0]
    if not (first == "_" or first.isalpha() or ord(first) >= 0x80):
        return False
    return all(
        char == "_" or char == "$" or char.isalnum() or ord(char) >= 0x80
        for char in identifier[1:]
    )


def _quote_replacement(original: str, replacement: str) -> str:
    if original.startswith('"'):
        return '"{}"'.format(replacement.replace('"', '""'))
    if original.startswith("`"):
        return "`{}`".format(replacement.replace("`", "``"))
    if original.startswith("[") and "]" not in replacement:
        return f"[{replacement}]"
    if _valid_bare_identifier(replacement):
        return replacement
    return '"{}"'.format(replacement.replace('"', '""'))


def _quote_if_bare(token_text: str, replacement: str) -> str:
    # Rewriting NEW./OLD. references normalizes a bare source column to a
    # quoted destination column, which is always valid.
    if token_text.startswith(('"', "`")) or token_text.startswith("["):
        return _quote_replacement(token_text, replacement)
    if _valid_bare_identifier(replacement):
        return replacement
    return '"{}"'.format(replacement.replace('"', '""'))


def rewrite_check_expression(expression: str, rename: dict[str, str]) -> str:
    """Rewrite column identifiers in a CHECK expression, preserving trivia."""
    if not rename:
        return expression
    tokens = _lex(expression)
    meaningful = _meaningful(tokens)
    replacements = {_ascii_fold(key): value for key, value in rename.items()}
    edits: list[tuple[int, int, str]] = []
    for index, token in enumerate(meaningful):
        if not _is_identifier_token(meaningful, index):
            continue
        replacement = replacements.get(_ascii_fold(_unquote(token.text)))
        if replacement is not None:
            edits.append(
                (token.start, token.end, _quote_replacement(token.text, replacement))
            )
    for start, end, replacement in reversed(edits):
        expression = expression[:start] + replacement + expression[end:]
    return expression


_GENERATED_TYPE_STOPWORDS = frozenset(
    (
        "CONSTRAINT",
        "PRIMARY",
        "NOT",
        "NULL",
        "UNIQUE",
        "CHECK",
        "DEFAULT",
        "COLLATE",
        "REFERENCES",
        "GENERATED",
        "AS",
    )
)
_GENERATED_STORAGE = frozenset(("VIRTUAL", "STORED"))


def parse_generated_columns(create_sql: str) -> dict[str, GeneratedColumn]:
    """
    Return generated column definitions keyed by column name from a CREATE TABLE.

    Both ``"c" AS (expr)`` and ``"c" TYPE GENERATED ALWAYS AS (expr) STORED``
    forms are supported. Raises :class:`ParseError` for malformed generated
    column syntax.
    """
    body_info = _table_body(create_sql)
    if body_info is None:
        return {}
    body, _ = body_info
    generated: dict[str, GeneratedColumn] = {}
    for item, _, _ in _split_spans(body, _lex(body)):
        tokens = _meaningful(_lex(item))
        if not tokens:
            continue
        index = 0
        if tokens[index].is_keyword("CONSTRAINT"):
            index += 2
        if index >= len(tokens):
            continue
        head = tokens[index]
        if head.kind == "word" and head.text.upper() in _TABLE_CONSTRAINT_KEYWORDS:
            continue
        column = _unquote(head.text)
        index += 1
        # Optional declared type (which may itself contain parentheses, e.g.
        # VARCHAR(255)). Stop at the first top-level constraint keyword.
        type_tokens: list[_Token] = []
        while index < len(tokens):
            token = tokens[index]
            if token.text == "(":
                close = _matching_paren(tokens, index)
                type_tokens.extend(tokens[index : close + 1])
                index = close + 1
                continue
            if token.kind == "word" and token.text.upper() in _GENERATED_TYPE_STOPWORDS:
                break
            if token.kind == "identifier":
                break
            type_tokens.append(token)
            index += 1
        declared_type = "".join(token.text for token in type_tokens).strip()
        clause_start_index = index
        as_index = None
        for candidate in range(index, len(tokens)):
            if tokens[candidate].is_keyword("AS"):
                as_index = candidate
                break
            if tokens[candidate].kind == "word" and tokens[
                candidate
            ].text.upper() not in ("GENERATED", "ALWAYS"):
                break
        if as_index is None:
            continue
        if as_index + 1 >= len(tokens) or tokens[as_index + 1].text != "(":
            raise ParseError(
                f"Generated column {column!r}: AS must be followed by a parenthesized expression"
            )
        expression_open = as_index + 1
        expression_close = _matching_paren(tokens, expression_open)
        expression = item[
            tokens[expression_open].end : tokens[expression_close].start
        ].strip()
        storage = "VIRTUAL"
        for token in tokens[expression_close + 1 :]:
            if token.kind == "word" and token.text.upper() in _GENERATED_STORAGE:
                storage = token.text.upper()
                break
        clause = item[tokens[clause_start_index].start :].strip()
        generated[column] = GeneratedColumn(
            name=column,
            declared_type=declared_type,
            expression=expression,
            storage=storage,
            clause=clause,
        )
    return generated


@dataclass(frozen=True)
class TriggerRewrite:
    """
    The result of planning how a CREATE TRIGGER statement survives a rebuild.

    ``recreate`` is True when ``sql`` can be executed against the replacement
    table (verbatim or with ``NEW.``/``OLD.`` column references rewritten).
    Otherwise ``reason`` explains why the trigger will be lost and must be
    recreated manually.
    """

    recreate: bool
    sql: str = ""
    reason: str = ""
    referenced_columns: tuple[str, ...] = ()


_TABLE_INTRODUCING_KEYWORDS = frozenset(("FROM", "JOIN", "INTO", "UPDATE"))


def _trigger_regions(
    tokens: list[_Token], table_name: str
) -> tuple[int, tuple[int, int], tuple[int, int]]:
    """
    Return ``(on_table_index, when_span, body_span)`` for a CREATE TRIGGER.

    The WHEN span covers the optional WHEN expression (only bare and
    ``NEW.``/``OLD.``-qualified column references live there); the body span
    covers BEGIN...END, where table references are also scanned. The
    ``UPDATE OF <columns>`` list in the header is intentionally excluded.
    """
    begin_index = None
    for index, token in enumerate(tokens):
        if token.is_keyword("BEGIN"):
            begin_index = index
            break
    if begin_index is None:
        raise ParseError("CREATE TRIGGER statement is missing BEGIN")
    end_index = None
    for index in range(begin_index + 1, len(tokens)):
        if tokens[index].is_keyword("END"):
            end_index = index
            break
    if end_index is None:
        raise ParseError("CREATE TRIGGER statement is missing END")
    on_index = None
    for index in range(begin_index):
        token = tokens[index]
        if not token.is_keyword("ON"):
            continue
        if index + 1 >= len(tokens):
            continue
        following = tokens[index + 1]
        if following.kind in ("identifier", "word"):
            name = following.text
            if following.kind == "identifier":
                name = _unquote(name)
            if _ascii_fold(name) == _ascii_fold(table_name):
                on_index = index
                break
    if on_index is None:
        raise ParseError(
            f"Could not find ON {table_name!r} clause in CREATE TRIGGER statement"
        )
    # A trailing FOR EACH ROW may sit between ON and WHEN/BEGIN.
    when_span: tuple[int, int] = (-1, -1)
    scan = on_index + 2
    if scan < begin_index and tokens[scan].is_keyword("WHEN"):
        when_span = (scan + 1, begin_index)
    return on_index + 1, when_span, (begin_index + 1, end_index)


def plan_trigger_sql(
    sql: str,
    table_name: str,
    rename: dict[str, str] | None = None,
    drop: Iterable[str] | None = None,
) -> TriggerRewrite:
    """
    Decide how a trigger on ``table_name`` survives a transform.

    With no columns renamed or dropped the trigger is recreated verbatim.
    ``NEW."col"``/``OLD."col"`` references to renamed columns are rewritten.
    A trigger that references a dropped column (qualified or not), or that
    references a renamed column through an unqualified identifier (which
    cannot be mechanically distinguished from a function name or a column of
    another table), cannot be recreated safely and is reported with a
    diagnostic reason instead. Such cases - common for hand-written triggers -
    must be dropped and recreated by hand.
    """
    rename = rename or {}
    drop_set = {fold for fold in (_ascii_fold(column) for column in (drop or ()))}
    rename_folded = {_ascii_fold(key): value for key, value in rename.items()}
    all_tokens = _lex(sql)
    tokens = _meaningful(all_tokens)
    if not tokens or not tokens[0].is_keyword("CREATE"):
        raise ParseError("Expected a CREATE TRIGGER statement")
    _, when_span, body_span = _trigger_regions(tokens, table_name)

    referenced: set[str] = set()
    edits: list[tuple[int, int, str]] = []
    dropped_refs: list[str] = []
    bare_renamed: list[str] = []
    referenced_tables: set[str] = set()

    def _update_set_target_table(region: list[_Token], inner_index: int) -> str | None:
        """Name of the table whose column a top-level UPDATE ... SET targets."""
        depth = 0
        for back in range(inner_index - 1, -1, -1):
            token = region[back]
            if token.text == ")":
                depth += 1
            elif token.text == "(":
                depth -= 1
                if depth < 0:
                    return None
            if depth == 0 and token.is_keyword("SET"):
                # SET belongs to the nearest preceding UPDATE; commas after
                # SET stay in the same clause. Any clause keyword aborts the
                # assignment list, so tokens after WHERE/ON/etc are not
                # assignment targets even if an earlier SET exists.
                for further in range(inner_index - 1, back, -1):
                    earlier = region[further]
                    if earlier.kind == "word" and earlier.text.upper() in (
                        "WHERE",
                        "ON",
                        "WHEN",
                        "FROM",
                    ):
                        return None
                # Find "UPDATE <name>" preceding this SET clause
                for further in range(back - 1, -1, -1):
                    earlier = region[further]
                    if earlier.is_keyword("UPDATE"):
                        target_token = region[further + 1]
                        return (
                            _unquote(target_token.text)
                            if target_token.kind == "identifier"
                            else target_token.text
                        )
        return None

    def _scan(region: list[_Token], offset: int, allow_tables: bool) -> None:
        for inner_index, token in enumerate(region):
            previous = region[inner_index - 1] if inner_index else None
            paren_depth = sum(1 for t in region[:inner_index] if t.text == "(") - sum(
                1 for t in region[:inner_index] if t.text == ")"
            )
            update_target = (
                _update_set_target_table(region, inner_index)
                if allow_tables and paren_depth == 0
                else None
            )
            in_own_update_set = update_target is not None and _ascii_fold(
                update_target
            ) == _ascii_fold(table_name)
            if (
                allow_tables
                and previous is not None
                and previous.kind == "word"
                and previous.text.upper() in _TABLE_INTRODUCING_KEYWORDS
                and token.kind in ("identifier", "word")
                and not (
                    # FROM ( ... ) is a subquery, not a table name;
                    # INTO <table> ( ... ) names the table before its
                    # column list and must still be captured.
                    previous.text.upper() == "FROM"
                    and (
                        inner_index + 1 < len(region)
                        and region[inner_index + 1].text == "("
                    )
                )
            ):
                name_token = token
                if (
                    inner_index + 2 < len(region)
                    and region[inner_index + 1].text == "."
                    and region[inner_index + 2].kind in ("identifier", "word")
                ):
                    name_token = region[inner_index + 2]
                referenced_tables.add(
                    _unquote(name_token.text)
                    if name_token.kind == "identifier"
                    else name_token.text
                )
            if not _is_identifier_token(region, inner_index):
                continue
            name = _unquote(token.text) if token.kind == "identifier" else token.text
            folded = _ascii_fold(name)
            qualified = (
                inner_index >= 2
                and region[inner_index - 1].text == "."
                and region[inner_index - 2].kind == "word"
                and region[inner_index - 2].text.upper() in ("NEW", "OLD")
            )
            if qualified:
                referenced.add(name)
                if folded in drop_set:
                    dropped_refs.append(name)
                elif folded in rename_folded:
                    edits.append(
                        (
                            token.start,
                            token.end,
                            _quote_if_bare(token.text, rename_folded[folded]),
                        )
                    )
                continue
            if previous is not None and previous.text == ".":
                continue
            if paren_depth and not in_own_update_set:
                continue
            if update_target is not None and not in_own_update_set:
                # SET target of UPDATE on a *different* table
                continue
            if (
                allow_tables
                and previous is not None
                and previous.kind == "word"
                and previous.text.upper() in _TABLE_INTRODUCING_KEYWORDS
            ):
                continue
            if folded in drop_set:
                dropped_refs.append(name)
            elif folded in rename_folded:
                if in_own_update_set:
                    edits.append(
                        (
                            token.start,
                            token.end,
                            _quote_replacement(token.text, rename_folded[folded]),
                        )
                    )
                else:
                    bare_renamed.append(name)

    if when_span[0] != -1:
        _scan(tokens[when_span[0] : when_span[1]], when_span[0], False)
    _scan(tokens[body_span[0] : body_span[1]], body_span[0], True)

    referenced_columns = tuple(
        sorted(referenced | set(dropped_refs) | set(bare_renamed))
    )
    changing = bool(rename or drop_set)
    if not changing:
        return TriggerRewrite(
            recreate=True, sql=sql, referenced_columns=referenced_columns
        )
    if dropped_refs:
        column = sorted(set(dropped_refs))[0]
        return TriggerRewrite(
            recreate=False,
            reason=(
                f"references dropped column {column!r}; recreate the trigger manually "
                "after the transform"
            ),
            referenced_columns=referenced_columns,
        )
    if bare_renamed:
        column = sorted(set(bare_renamed))[0]
        return TriggerRewrite(
            recreate=False,
            reason=(
                f"references renamed column {column!r} without a NEW./OLD. qualifier; "
                "recreate the trigger manually after the transform"
            ),
            referenced_columns=referenced_columns,
        )
    rewritten = sql
    for start, end, replacement in reversed(edits):
        rewritten = rewritten[:start] + replacement + rewritten[end:]
    return TriggerRewrite(
        recreate=True, sql=rewritten, referenced_columns=referenced_columns
    )
