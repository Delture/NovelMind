from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

def _has_columns(
    connection: Connection,
    table_name: str,
    expected_columns: set[str],
) -> None:
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
    return (
        "".join(str(sqltext).casefold().split())
        .replace('"', "")
        .replace("`", "")
    )


def _validate_active_job_index(
    connection: Connection,
) -> bool:
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
    actual_columns = inspect(connection).get_columns(table_name)
    if {item["name"] for item in actual_columns} != set(expected):
        raise RuntimeError(
            f"数据库表 {table_name} 的字段集合不正确，"
            "请先备份并修复数据库结构。"
        )

    primary_columns = inspect(connection).get_pk_constraint(table_name)["constrained_columns"]
    primary_positions = {name: i + 1 for i, name in enumerate(primary_columns)}
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
            or primary_positions.get(column["name"], 0) != primary_key
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
            if actual.get(name) != normalized_expected.get(str(name))
        )
        raise RuntimeError(
            f"数据库表 {table_name} 的检查约束 "
            f"{', '.join(mismatched_names)} 定义不正确，"
            "请先备份并修复数据库结构。"
        )
