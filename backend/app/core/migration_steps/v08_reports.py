"""v08_reports：保留历史数据升级和约束检查。"""
from sqlalchemy import text
from sqlalchemy.engine import Connection
from .validation import (
    _validate_exact_columns,
    _validate_named_unique,
    _validate_exact_cascade_foreign_key,
    _validate_exact_checks,
)

def _add_novel_report_table(connection: Connection) -> None:
    """Create and strictly validate the current report snapshot."""
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS novel_reports (
                id INTEGER NOT NULL PRIMARY KEY,
                novel_id INTEGER NOT NULL,
                source_hash VARCHAR(64) NOT NULL,
                report_data JSON NOT NULL,
                model VARCHAR(100) NOT NULL,
                prompt_version VARCHAR(50) NOT NULL,
                summary_count INTEGER NOT NULL,
                character_count INTEGER NOT NULL,
                generation_steps INTEGER NOT NULL,
                includes_character_analysis BOOLEAN NOT NULL
                    DEFAULT 0,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT uq_novel_reports_novel_id UNIQUE (novel_id),
                CONSTRAINT ck_novel_reports_source_hash CHECK (
                    length(source_hash) = 64
                    AND source_hash NOT GLOB '*[^0-9a-f]*'
                ),
                CONSTRAINT ck_novel_reports_data_object CHECK (
                    json_valid(report_data)
                    AND json_type(report_data) = 'object'
                ),
                CONSTRAINT ck_novel_reports_model CHECK (
                    length(trim(model)) BETWEEN 1 AND 100
                ),
                CONSTRAINT ck_novel_reports_prompt_version CHECK (
                    length(trim(prompt_version)) BETWEEN 1 AND 50
                ),
                CONSTRAINT ck_novel_reports_summary_count_positive
                    CHECK (summary_count > 0),
                CONSTRAINT ck_novel_reports_character_count_nonnegative
                    CHECK (character_count >= 0),
                CONSTRAINT ck_novel_reports_generation_steps_positive
                    CHECK (generation_steps > 0),
                CONSTRAINT ck_novel_reports_character_analysis_boolean
                    CHECK (includes_character_analysis IN (0, 1)),
                FOREIGN KEY(novel_id) REFERENCES novels(id) ON DELETE CASCADE
            )
            """
        )
    )
    _validate_exact_columns(
        connection,
        "novel_reports",
        {
            "id": ("INTEGER", False, 1, None),
            "novel_id": ("INTEGER", False, 0, None),
            "source_hash": ("VARCHAR(64)", False, 0, None),
            "report_data": ("JSON", False, 0, None),
            "model": ("VARCHAR(100)", False, 0, None),
            "prompt_version": ("VARCHAR(50)", False, 0, None),
            "summary_count": ("INTEGER", False, 0, None),
            "character_count": ("INTEGER", False, 0, None),
            "generation_steps": ("INTEGER", False, 0, None),
            "includes_character_analysis": (
                "BOOLEAN",
                False,
                0,
                "0",
            ),
            "created_at": ("DATETIME", False, 0, None),
            "updated_at": ("DATETIME", False, 0, None),
        },
    )
    _validate_named_unique(
        connection,
        "novel_reports",
        "uq_novel_reports_novel_id",
        ["novel_id"],
    )
    _validate_exact_cascade_foreign_key(
        connection,
        "novel_reports",
        ["novel_id"],
        "novels",
        ["id"],
    )
    _validate_exact_checks(
        connection,
        "novel_reports",
        {
            "ck_novel_reports_source_hash": (
                "length(source_hash) = 64 "
                "AND source_hash NOT GLOB '*[^0-9a-f]*'"
            ),
            "ck_novel_reports_data_object": (
                "json_valid(report_data) "
                "AND json_type(report_data) = 'object'"
            ),
            "ck_novel_reports_model": (
                "length(trim(model)) BETWEEN 1 AND 100"
            ),
            "ck_novel_reports_prompt_version": (
                "length(trim(prompt_version)) BETWEEN 1 AND 50"
            ),
            "ck_novel_reports_summary_count_positive": (
                "summary_count > 0"
            ),
            "ck_novel_reports_character_count_nonnegative": (
                "character_count >= 0"
            ),
            "ck_novel_reports_generation_steps_positive": (
                "generation_steps > 0"
            ),
            "ck_novel_reports_character_analysis_boolean": (
                "includes_character_analysis IN (0, 1)"
            ),
        },
    )
