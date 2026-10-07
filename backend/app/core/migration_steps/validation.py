"""validation：保留历史数据升级和约束检查。"""
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

def _has_columns(
    connection: Connection,
    table_name: str,
    expected_columns: set[str],
) -> None:
    """Raise a readable error when an existing table is incomplete."""
    actual_columns = {
        column["name"]
        for column in inspect(connection).get_columns(table_name)
    }
    missing_columns = expected_columns - actual_columns
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise RuntimeError(
            f"数据库表 {table_name} 缺少字段：{missing}。"
            "请先备份并修复数据库结构。"
        )


def _has_cascade_foreign_key(
    connection: Connection,
    table_name: str,
    local_column: str,
    referred_table: str,
) -> bool:
    """Return whether an expected cascading foreign key exists."""
    for foreign_key in inspect(connection).get_foreign_keys(table_name):
        ondelete = str(
            foreign_key.get("options", {}).get("ondelete", "")
        ).upper()
        if (
            foreign_key.get("constrained_columns") == [local_column]
            and foreign_key.get("referred_table") == referred_table
            and ondelete == "CASCADE"
        ):
            return True
    return False


def _has_unique_columns(
    connection: Connection,
    table_name: str,
    expected_columns: list[str],
) -> bool:
    """Return whether a table enforces uniqueness for exact columns."""
    inspector = inspect(connection)
    candidates = (
        inspector.get_unique_constraints(table_name)
        + inspector.get_indexes(table_name)
    )
    return any(
        set(item.get("column_names", [])) == set(expected_columns)
        and (
            item in inspector.get_unique_constraints(table_name)
            or bool(item.get("unique"))
        )
        for item in candidates
    )


def _validate_index(
    connection: Connection,
    table_name: str,
    index_name: str,
    expected_columns: list[str],
) -> None:
    """Validate a named non-unique lookup index."""
    matching = [
        item
        for item in inspect(connection).get_indexes(table_name)
        if item.get("name") == index_name
    ]
    if (
        len(matching) != 1
        or set(matching[0].get("column_names", [])) != set(expected_columns)
        or bool(matching[0].get("unique"))
    ):
        raise RuntimeError(
            f"数据库索引 {index_name} 的定义不正确，"
            "请先备份并修复数据库结构。"
        )


def _normalized_sql(sqltext: object) -> str:
    """Normalize a reflected SQL expression for strict comparison."""
    return (
        "".join(str(sqltext).casefold().split())
        .replace('"', "")
        .replace("`", "")
    )


def _validate_active_job_index(
    connection: Connection,
) -> bool:
    """Validate the partial unique index, returning whether it exists."""
    index_name = "ux_analysis_jobs_active_novel_type"
    matching = [
        item
        for item in inspect(connection).get_indexes("analysis_jobs")
        if item.get("name") == index_name
    ]
    if not matching:
        return False

    item = matching[0]
    where_clause = item.get("dialect_options", {}).get("sqlite_where")
    expected_where = "status IN ('queued', 'running')"
    if (
        len(matching) != 1
        or not bool(item.get("unique"))
        or item.get("column_names") != ["novel_id", "job_type"]
        or _normalized_sql(where_clause)
        != _normalized_sql(expected_where)
    ):
        raise RuntimeError(
            f"数据库索引 {index_name} 的定义不正确，"
            "请先备份并修复数据库结构。"
        )
    return True


def _validate_exact_columns(
    connection: Connection,
    table_name: str,
    expected: dict[str, tuple[str, bool, int, str | None]],
) -> None:
    """Validate column identities and meaningful type/null/key/default contracts."""
    actual_columns = inspect(connection).get_columns(table_name)
    if {item["name"] for item in actual_columns} != set(expected):
        raise RuntimeError(
            f"数据库表 {table_name} 的字段集合不正确，"
            "请先备份并修复数据库结构。"
        )

    for column in actual_columns:
        expected_type, nullable, primary_key, default = expected[
            column["name"]
        ]
        actual_default = column.get("default")
        normalized_default = (
            _normalized_sql(actual_default).strip("()'")
            if actual_default is not None
            else None
        )
        if (
            str(column["type"]).upper() != expected_type
            or bool(column["nullable"]) is not nullable
            or int(column["primary_key"]) != primary_key
            or (normalized_default != default and not (
                table_name == "character_appearances"
                and column["name"] in {"analysis_id", "novel_id"}
                and default == "0" and normalized_default is None
            ))
        ):
            raise RuntimeError(
                f"数据库表 {table_name} 的字段 {column['name']} "
                "定义不正确，请先备份并修复数据库结构。"
            )


def _validate_named_unique(
    connection: Connection,
    table_name: str,
    constraint_name: str,
    expected_columns: list[str],
) -> None:
    """Validate one named unique constraint with exact column order."""
    inspector = inspect(connection)
    candidates = (
        inspector.get_unique_constraints(table_name)
        + inspector.get_indexes(table_name)
    )
    matching = [
        item
        for item in candidates
        if item.get("name") == constraint_name
    ]
    if (
        len(matching) != 1
        or set(matching[0].get("column_names", [])) != set(expected_columns)
        or (
            matching[0]
            not in inspector.get_unique_constraints(table_name)
            and not bool(matching[0].get("unique"))
        )
    ):
        raise RuntimeError(
            f"数据库唯一约束 {constraint_name} 的定义不正确，"
            "请先备份并修复数据库结构。"
        )


def _ensure_named_unique_index(
    connection: Connection,
    table_name: str,
    index_name: str,
    expected_columns: list[str],
) -> None:
    """Create a legacy-compatible parent key or validate its name."""
    inspector = inspect(connection)
    if table_name not in inspector.get_table_names():
        raise RuntimeError(
            f"数据库缺少 {table_name} 表，无法创建父级唯一键。"
        )
    named_objects = [
        item
        for item in (
            inspector.get_unique_constraints(table_name)
            + inspector.get_indexes(table_name)
        )
        if item.get("name") == index_name
    ]
    if named_objects:
        _validate_named_unique(
            connection,
            table_name,
            index_name,
            expected_columns,
        )
        return

    column_sql = ", ".join(expected_columns)
    connection.execute(
        text(
            f"CREATE UNIQUE INDEX {index_name} "
            f"ON {table_name} ({column_sql})"
        )
    )
    _validate_named_unique(
        connection,
        table_name,
        index_name,
        expected_columns,
    )


def _validate_exact_cascade_foreign_key(
    connection: Connection,
    table_name: str,
    constrained_columns: list[str],
    referred_table: str,
    referred_columns: list[str],
) -> None:
    """严格校验一个复合级联外键的本地列、目标表和目标列。

    ``connection`` 用于反射，``table_name`` 是待查表，``local_columns`` 与
    ``referred_columns`` 必须按约束顺序对应，``referred_table`` 是父表。缺失、重复
    或 ondelete 不是 CASCADE 都抛 RuntimeError，防止损坏结构继续运行。
    """
    matching = [
        item
        for item in inspect(connection).get_foreign_keys(table_name)
        if item.get("constrained_columns") == constrained_columns
    ]
    if len(matching) != 1:
        raise RuntimeError(
            f"数据库表 {table_name} 的外键 "
            f"{','.join(constrained_columns)} 定义不正确，"
            "请先备份并修复数据库结构。"
        )

    foreign_key = matching[0]
    ondelete = str(
        foreign_key.get("options", {}).get("ondelete", "")
    ).upper()
    if (
        foreign_key.get("referred_table") != referred_table
        or foreign_key.get("referred_columns") != referred_columns
        or ondelete != "CASCADE"
    ):
        raise RuntimeError(
            f"数据库表 {table_name} 的外键 "
            f"{','.join(constrained_columns)} 定义不正确，"
            "请先备份并修复数据库结构。"
        )


def _validate_exact_checks(
    connection: Connection,
    table_name: str,
    expected: dict[str, str],
) -> None:
    """Validate the complete named CHECK-constraint set."""
    actual = {
        item.get("name"): _normalized_sql(item.get("sqltext", ""))
        for item in inspect(connection).get_check_constraints(table_name)
    }
    normalized_expected = {
        name: _normalized_sql(sqltext)
        for name, sqltext in expected.items()
    }
    if actual != normalized_expected:
        mismatched_names = sorted(
            str(name)
            for name in set(actual) | set(normalized_expected)
            if actual.get(name) != normalized_expected.get(name)
        )
        raise RuntimeError(
            f"数据库表 {table_name} 的检查约束 "
            f"{', '.join(mismatched_names)} 定义不正确，"
            "请先备份并修复数据库结构。"
        )
