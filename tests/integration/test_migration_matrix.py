"""Upgrade populated historical schemas without using or opening a user database."""

import pytest


def build_historical(engine, version):
    from backend.app.core.migrations import MIGRATIONS
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE novels (id INTEGER PRIMARY KEY, filename VARCHAR(255) NOT NULL, encoding VARCHAR(20) NOT NULL, chapter_count INTEGER NOT NULL, created_at DATETIME NOT NULL)")
        connection.exec_driver_sql("CREATE TABLE chapters (id INTEGER PRIMARY KEY, novel_id INTEGER NOT NULL REFERENCES novels(id) ON DELETE CASCADE, chapter_index INTEGER NOT NULL, title VARCHAR(255) NOT NULL, content TEXT NOT NULL, character_count INTEGER NOT NULL)")
        connection.exec_driver_sql("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        for number, name, migrate in MIGRATIONS[:version]:
            migrate(connection)
            connection.exec_driver_sql("INSERT INTO schema_migrations (version, name) VALUES (?, ?)", (number, name))


def seed_history(engine, version):
    """Two independent novels make accidental global deletion observable."""
    from backend.app.models.novel import Novel, Chapter
    from backend.app.models.analysis import ChapterSummary
    from backend.app.models.character import CharacterAnalysis, Character, CharacterAppearance
    from backend.app.models.rag import RagIndex, RagChunkRecord, RagQuestion
    from backend.app.models.report import NovelReport

    with engine.begin() as connection:
        for key in (1, 2):
            connection.execute(Novel.__table__.insert().values(id=key, filename=f"history-{key}.txt", encoding="utf-8", chapter_count=1))
            connection.execute(Chapter.__table__.insert().values(id=key, novel_id=key, chapter_index=1, title="来信", content="历史原创正文", character_count=6))
            if version >= 2:
                connection.execute(ChapterSummary.__table__.insert().values(id=key, chapter_id=key, summary="旧摘要", key_events=["来信"], plot_progression="旧进展", model="old-model", prompt_version="v1", source_hash="a"*64))
            if version >= 5:
                connection.execute(CharacterAnalysis.__table__.insert().values(id=key, novel_id=key, source_hash="a"*64, model="old-model", extraction_prompt_version="v1", merge_prompt_version="v1", relationship_prompt_version="v1", chapter_count=1))
                connection.execute(Character.__table__.insert().values(id=key, analysis_id=key, canonical_name=f"人物{key}", aliases=["主角"], short_description="旧人物", first_chapter_index=1, last_chapter_index=1, chapter_count=1, mention_count=2))
                connection.execute(CharacterAppearance.__table__.insert().values(id=key, analysis_id=key, novel_id=key, character_id=key, chapter_id=key, mention_count=2, summary="旧出场"))
            if version >= 6:
                connection.execute(RagIndex.__table__.insert().values(id=key, novel_id=key, source_hash="a"*64, collection_name=f"old-collection-{key}", embedding_provider="siliconflow", embedding_model="Qwen/Qwen3-Embedding-4B", embedding_dimensions=1024, chunk_version="v1", chunk_count=1))
                connection.execute(RagChunkRecord.__table__.insert().values(id=key, rag_index_id=key, chapter_id=key, chunk_id="chunk_"+str(key)*64, chapter_index=1, chunk_index=1, start_char=0, end_char=6, content_hash="a"*64))
            if version >= 7:
                connection.execute(RagQuestion.__table__.insert().values(id=f"old-question-{key}", novel_id=key, question="收到什么？", index_source_hash="a"*64, top_k=3, status="succeeded", answer="旧回答", generation_model="old-model", prompt_version="v1"))
            if version >= 8:
                connection.execute(NovelReport.__table__.insert().values(id=key, novel_id=key, source_hash="a"*64, model="old-model", prompt_version="v1", summary_count=1, character_count=0, generation_steps=1, report_data={"title":"旧报告"}, includes_character_analysis=False))


def rows(engine):
    from sqlalchemy import inspect
    with engine.connect() as connection:
        return {name: [tuple(row) for row in connection.exec_driver_sql(f'SELECT * FROM "{name}" ORDER BY rowid')]
                for name in inspect(connection).get_table_names() if name != "schema_migrations"}


def schema(engine):
    with engine.connect() as connection:
        return connection.exec_driver_sql("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").all()


@pytest.mark.parametrize("version", range(1, 10))
def test_v1_through_v9_upgrade_preserves_data_and_cascade(isolated_config, version):
    from backend.app.core.database import create_db_engine, init_db
    from backend.app.models.workspace import ReadingState, Workflow, WorkspaceSetting
    engine = create_db_engine(isolated_config.config.get_settings().database_url)
    try:
        build_historical(engine, version)
        seed_history(engine, version)
        before = rows(engine)
        init_db(engine)
        upgraded = rows(engine)
        for name, values in before.items():
            assert upgraded[name] == values, f"v{version}: changed historical {name}"
        first_schema = schema(engine)
        init_db(engine)
        assert rows(engine) == upgraded
        assert schema(engine) == first_schema
        with engine.begin() as connection:
            assert connection.exec_driver_sql("SELECT version FROM schema_migrations ORDER BY version").scalars().all() == list(range(1,13))
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='trigger' AND name='trg_character_appearances_fill_ownership'").all() == []
            for key in (1,2):
                connection.execute(ReadingState.__table__.insert().values(novel_id=key, value={"bookmark":"kept"}))
                connection.execute(Workflow.__table__.insert().values(id=f"workflow-{key}",novel_id=key,kind="report",created_at=__import__("datetime").datetime(2026, 9, 27)))
            connection.execute(WorkspaceSetting.__table__.insert().values(key="privacy",value={"external":False}))
            connection.exec_driver_sql("DELETE FROM novels WHERE id=1")
        after = rows(engine)
        for name, values in before.items():
            if values:
                assert len(after[name]) == 1, f"cascade lost or retained wrong {name}"
                assert after[name][0] == values[1], f"other novel changed in {name}"
        assert len(after["reading_states"]) == len(after["workflows"]) == 1
        assert len(after["workspace_settings"]) == 1
    finally:
        engine.dispose()


def test_new_v11_database_reinitializes_without_schema_changes(isolated_config):
    from sqlalchemy import inspect
    from backend.app.core.database import create_db_engine, init_db
    engine = create_db_engine(isolated_config.config.get_settings().database_url)
    try:
        init_db(engine)
        before = schema(engine)
        init_db(engine)
        assert schema(engine) == before
        columns = {item["name"]:item for item in inspect(engine).get_columns("character_appearances")}
        assert columns["analysis_id"]["default"] is None
        assert columns["novel_id"]["default"] is None
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT max(version),count(*) FROM schema_migrations").one() == (12,12)
    finally:
        engine.dispose()


@pytest.mark.parametrize("invalid", [False, True])
def test_v11_fills_legacy_zero_ownership_or_rolls_back_mismatch(isolated_config, invalid):
    from backend.app.core.database import create_db_engine
    from backend.app.core.migrations import run_migrations
    engine = create_db_engine(isolated_config.config.get_settings().database_url)
    try:
        build_historical(engine,9)
        seed_history(engine,9)
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            connection.exec_driver_sql("UPDATE character_appearances SET analysis_id=?,novel_id=? WHERE id=1", (2,2) if invalid else (0,0))
            connection.commit()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        old_rows,old_schema=rows(engine),schema(engine)
        if invalid:
            with pytest.raises(RuntimeError,match="不一致归属"):
                run_migrations(engine)
            assert rows(engine)==old_rows
            assert schema(engine)==old_schema
            with engine.connect() as connection:
                assert connection.exec_driver_sql("SELECT max(version),count(*) FROM schema_migrations").one()==(9,9)
        else:
            run_migrations(engine)
            with engine.connect() as connection:
                assert connection.exec_driver_sql("SELECT analysis_id,novel_id FROM character_appearances WHERE id=1").one()==(1,1)
                assert connection.exec_driver_sql("PRAGMA foreign_key_check").all()==[]
    finally:
        engine.dispose()
