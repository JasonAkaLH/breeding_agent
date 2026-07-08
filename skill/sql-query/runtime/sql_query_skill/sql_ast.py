from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import traverse_scope


class SQLAstError(ValueError):
    """Raised when SQL cannot be parsed into a SQLGlot AST."""


@dataclass(frozen=True)
class SQLAstColumn:
    name: str
    table: str = ""


@dataclass(frozen=True)
class SQLAstBranch:
    expression: exp.Select
    sql: str
    tables: tuple[str, ...]
    alias_to_table: dict[str, str]
    columns: tuple[SQLAstColumn, ...]
    projection_literals: dict[str, str]
    has_order: bool
    has_limit: bool


@dataclass(frozen=True)
class SQLAstAnalysis:
    expression: exp.Expression
    normalized_sql: str
    statement_kind: str
    tables: tuple[str, ...]
    columns: tuple[SQLAstColumn, ...]
    branches: tuple[SQLAstBranch, ...]
    is_union_all: bool
    has_union: bool
    final_order_fields: tuple[tuple[str, str], ...]
    final_limit: int | None


def analyze_sql(sql: str, *, dialect: str = "mysql") -> SQLAstAnalysis:
    raw_sql = str(sql or "").strip()
    if not raw_sql:
        raise SQLAstError("SQL is empty.")
    try:
        expressions = sqlglot.parse(raw_sql, read=dialect)
    except ParseError as exc:
        raise SQLAstError(str(exc)) from exc
    if len(expressions) != 1 or expressions[0] is None:
        raise SQLAstError("SQL must contain exactly one statement.")

    root = expressions[0]
    tables = tuple(_dedupe(_real_table_names(root)))
    columns = tuple(_all_columns(root))
    branches = tuple(_branches(root, dialect=dialect))
    unions = list(root.find_all(exp.Union)) if not isinstance(root, exp.Union) else [root, *list(root.find_all(exp.Union))]
    return SQLAstAnalysis(
        expression=root,
        normalized_sql=root.sql(dialect=dialect),
        statement_kind=_statement_kind(root),
        tables=tables,
        columns=columns,
        branches=branches,
        is_union_all=bool(unions) and all(union.args.get("distinct") is False for union in unions),
        has_union=bool(unions),
        final_order_fields=_order_fields(root),
        final_limit=_limit_value(root.args.get("limit")),
    )


def is_readonly_query(analysis: SQLAstAnalysis) -> bool:
    return analysis.statement_kind in {"select", "union"}


def branch_has_constraint(branch: SQLAstBranch, *, field: str, operator: str, value: Any, table: str = "") -> bool:
    operator = str(operator or "").upper()
    where = branch.expression.args.get("where")
    if where is None:
        return False
    if operator == "=":
        return any(
            _binary_matches(predicate, exp.EQ, field=field, value=value, table=table, branch=branch)
            for predicate in where.find_all(exp.EQ)
        )
    if operator == ">=":
        return any(
            _binary_matches(predicate, exp.GTE, field=field, value=value, table=table, branch=branch)
            for predicate in where.find_all(exp.GTE)
        )
    if operator == "<=":
        return any(
            _binary_matches(predicate, exp.LTE, field=field, value=value, table=table, branch=branch)
            for predicate in where.find_all(exp.LTE)
        )
    if operator == "BETWEEN":
        return any(_between_matches(predicate, field=field, value=value, table=table, branch=branch) for predicate in where.find_all(exp.Between))
    if operator == "LIKE":
        return any(_like_matches(predicate, field=field, value=value, table=table, branch=branch) for predicate in where.find_all(exp.Like))
    return False


def branch_has_count(branch: SQLAstBranch) -> bool:
    return any(isinstance(node, exp.Count) for node in branch.expression.find_all(exp.Count))


def branch_projection_literal(branch: SQLAstBranch, alias: str) -> str:
    return branch.projection_literals.get(str(alias).lower(), "")


def final_has_order(analysis: SQLAstAnalysis, *, field: str, direction: str = "DESC") -> bool:
    expected_field = str(field or "").lower()
    expected_direction = str(direction or "DESC").upper()
    return any(name.lower() == expected_field and order_direction.upper() == expected_direction for name, order_direction in analysis.final_order_fields)


def final_has_limit(analysis: SQLAstAnalysis, limit: int) -> bool:
    return analysis.final_limit == int(limit)


def _statement_kind(root: exp.Expression) -> str:
    if isinstance(root, exp.Union):
        return "union"
    return str(root.key or type(root).__name__).lower()


def _real_table_names(root: exp.Expression) -> list[str]:
    names: list[str] = []
    for scope in traverse_scope(root):
        for _alias, source in scope.sources.items():
            if isinstance(source, exp.Table):
                names.append(_table_name(source))
    if not names:
        cte_aliases = _cte_aliases(root)
        for table in root.find_all(exp.Table):
            if table.name in cte_aliases:
                continue
            names.append(_table_name(table))
    return names


def _branches(root: exp.Expression, *, dialect: str) -> list[SQLAstBranch]:
    union = _root_or_wrapped_union(root)
    if union is not None:
        selects = _flatten_union_selects(union)
    elif isinstance(root, exp.Select):
        cte_sources = _direct_cte_sources(root)
        selects = _flatten_cte_sources(cte_sources, outer_select=root) if cte_sources else [root]
    else:
        selects = list(root.find_all(exp.Select))[:1]
    return [_branch(select, dialect=dialect) for select in selects if isinstance(select, exp.Select)]


def _root_or_wrapped_union(root: exp.Expression) -> exp.Union | None:
    if isinstance(root, exp.Union):
        return root
    if not isinstance(root, exp.Select):
        return None
    from_expr = root.args.get("from_")
    if from_expr is None:
        return None
    source = from_expr.this
    if isinstance(source, exp.Subquery) and isinstance(source.this, exp.Union):
        return source.this
    return None


def _flatten_union_selects(node: exp.Expression) -> list[exp.Select]:
    if isinstance(node, exp.Select):
        return [node]
    if isinstance(node, exp.Subquery):
        return _flatten_union_selects(node.this)
    if isinstance(node, exp.Union):
        return _flatten_union_selects(node.this) + _flatten_union_selects(node.expression)
    return [select for select in node.find_all(exp.Select)]


def _direct_cte_sources(select: exp.Select) -> list[tuple[str, exp.Expression]]:
    cte_map = _cte_source_map(select)
    if not cte_map:
        return []
    direct_sources = _direct_table_sources(select)
    if len(direct_sources) != 1 or direct_sources[0] not in cte_map:
        return []
    source = direct_sources[0]
    return [(source, cte_map[source])]


def _flatten_cte_sources(sources: list[tuple[str, exp.Expression]], *, outer_select: exp.Select | None = None) -> list[exp.Select]:
    selects: list[exp.Select] = []
    for source_alias, source in sources:
        union = _root_or_wrapped_union(source)
        if union is not None:
            flattened = _flatten_union_selects(union)
        elif isinstance(source, exp.Union):
            flattened = _flatten_union_selects(source)
        elif isinstance(source, exp.Select):
            nested_sources = _direct_cte_sources(source)
            flattened = _flatten_cte_sources(nested_sources, outer_select=source) if nested_sources else [source]
        else:
            flattened = list(source.find_all(exp.Select))
        if outer_select is not None:
            selects.extend(_apply_outer_select_to_branch(select, outer_select=outer_select, source_alias=source_alias) for select in flattened)
        else:
            selects.extend(flattened)
    return selects


def _apply_outer_select_to_branch(select: exp.Select, *, outer_select: exp.Select, source_alias: str) -> exp.Select:
    branch = select.copy()
    outer_where = outer_select.args.get("where")
    if isinstance(outer_where, exp.Where):
        outer_predicate = _remove_cte_column_qualifier(outer_where.this.copy(), source_alias=source_alias)
        inner_where = branch.args.get("where")
        if isinstance(inner_where, exp.Where):
            branch.set("where", exp.Where(this=exp.and_(inner_where.this.copy(), outer_predicate)))
        else:
            branch.set("where", exp.Where(this=outer_predicate))
    return branch


def _remove_cte_column_qualifier(expression: exp.Expression, *, source_alias: str) -> exp.Expression:
    def transform(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and str(node.table or "") == source_alias:
            replacement = node.copy()
            replacement.set("table", None)
            return replacement
        return node

    return expression.transform(transform)


def _cte_source_map(root: exp.Expression) -> dict[str, exp.Expression]:
    with_expression = root.args.get("with_")
    if not isinstance(with_expression, exp.With):
        return {}
    result: dict[str, exp.Expression] = {}
    for cte in with_expression.expressions:
        if isinstance(cte, exp.CTE) and cte.alias_or_name:
            result[str(cte.alias_or_name)] = cte.this
    return result


def _cte_aliases(root: exp.Expression) -> set[str]:
    aliases: set[str] = set()
    for with_expression in root.find_all(exp.With):
        for cte in with_expression.expressions:
            if isinstance(cte, exp.CTE) and cte.alias_or_name:
                aliases.add(str(cte.alias_or_name))
    return aliases


def _direct_table_sources(select: exp.Select) -> list[str]:
    sources: list[str] = []
    from_expression = select.args.get("from_")
    if from_expression is not None:
        source = from_expression.this
        if isinstance(source, exp.Table):
            sources.append(source.name)
    for join in select.args.get("joins") or []:
        source = join.this
        if isinstance(source, exp.Table):
            sources.append(source.name)
    return [source for source in sources if source]


def _branch(select: exp.Select, *, dialect: str) -> SQLAstBranch:
    alias_to_table = _alias_to_table(select)
    return SQLAstBranch(
        expression=select,
        sql=select.sql(dialect=dialect),
        tables=tuple(_dedupe(alias_to_table.values())),
        alias_to_table=alias_to_table,
        columns=tuple(_all_columns(select)),
        projection_literals=_projection_literals(select),
        has_order=select.args.get("order") is not None,
        has_limit=select.args.get("limit") is not None,
    )


def _alias_to_table(expression: exp.Expression) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for table in expression.find_all(exp.Table):
        name = table.name
        if not name:
            continue
        aliases[name] = name
        alias = table.alias
        if alias:
            aliases[alias] = name
    return aliases


def _table_name(table: exp.Table) -> str:
    db = table.db
    name = table.name
    return f"{db}.{name}" if db else name


def _all_columns(expression: exp.Expression) -> list[SQLAstColumn]:
    return [SQLAstColumn(name=column.name, table=str(column.table or "")) for column in expression.find_all(exp.Column)]


def _projection_literals(select: exp.Select) -> dict[str, str]:
    result: dict[str, str] = {}
    for expression in select.expressions:
        if not isinstance(expression, exp.Alias):
            continue
        alias = str(expression.alias_or_name or "").lower()
        literal = expression.this
        if alias and isinstance(literal, exp.Literal):
            result[alias] = str(literal.this)
    return result


def _order_fields(root: exp.Expression) -> tuple[tuple[str, str], ...]:
    order = root.args.get("order")
    if order is None:
        return ()
    fields: list[tuple[str, str]] = []
    for ordered in order.expressions:
        expression = ordered.this if isinstance(ordered, exp.Ordered) else ordered
        name = expression.name if isinstance(expression, exp.Column) else str(expression)
        direction = "DESC" if isinstance(ordered, exp.Ordered) and ordered.args.get("desc") else "ASC"
        if name:
            fields.append((name, direction))
    return tuple(fields)


def _limit_value(limit: exp.Expression | None) -> int | None:
    if limit is None:
        return None
    expression = limit.expression
    if isinstance(expression, exp.Literal):
        try:
            return int(expression.this)
        except (TypeError, ValueError):
            return None
    return None


def _binary_matches(predicate: exp.Expression, predicate_type: type[exp.Expression], *, field: str, value: Any, table: str, branch: SQLAstBranch) -> bool:
    if not isinstance(predicate, predicate_type):
        return False
    left = predicate.this
    right = predicate.expression
    return (_column_matches(left, field=field, table=table, branch=branch) and _literal_matches(right, value)) or (
        _column_matches(right, field=field, table=table, branch=branch) and _literal_matches(left, value)
    )


def _between_matches(predicate: exp.Between, *, field: str, value: Any, table: str, branch: SQLAstBranch) -> bool:
    if not _column_matches(predicate.this, field=field, table=table, branch=branch):
        return False
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return False
    return _literal_matches(predicate.args.get("low"), value[0]) and _literal_matches(predicate.args.get("high"), value[1])


def _like_matches(predicate: exp.Like, *, field: str, value: Any, table: str, branch: SQLAstBranch) -> bool:
    if not _column_matches(predicate.this, field=field, table=table, branch=branch):
        return False
    pattern = _literal_text(predicate.expression)
    return str(value or "") in pattern


def _column_matches(node: exp.Expression | None, *, field: str, table: str, branch: SQLAstBranch) -> bool:
    if not isinstance(node, exp.Column):
        return False
    if node.name != str(field):
        return False
    if not table:
        return True
    qualifier = str(node.table or "")
    if not qualifier:
        return table in branch.tables or not branch.tables
    return branch.alias_to_table.get(qualifier, qualifier) == table


def _literal_matches(node: exp.Expression | None, value: Any) -> bool:
    text = _literal_text(node)
    if text == "":
        return False
    return text == str(value)


def _literal_text(node: exp.Expression | None) -> str:
    if isinstance(node, exp.Literal):
        return str(node.this)
    return ""


def _dedupe(values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result
