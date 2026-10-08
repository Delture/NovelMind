from tests.support.character_fixtures import _add_current_extractions, _add_novel, _add_snapshot, _extraction_data
"""Tests for character-analysis API scheduling and result queries."""

from datetime import datetime
from typing import Callable, Generator
from unittest.mock import Mock

import pytest
from backend.app.services.errors import ServiceError as HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.analyzer.characters import (
    CHARACTER_EXTRACTION_PROMPT_VERSION,
    CHARACTER_MERGE_PROMPT_VERSION,
    CHARACTER_RELATIONSHIP_PROMPT_VERSION,
    character_analysis_source_hash,
    character_chapter_source_hash,
)
from backend.app.services.characters import (
    StartCharacterAnalysisRequest,
    _schedule_character_job,
    get_character,
    get_character_estimate,
    list_character_appearances,
    list_character_relationships,
    list_characters,
    start_character_analysis,
)
from backend.app.core.config import load_settings
from backend.app.core.database import Base, create_db_engine
from backend.app.main import app
from backend.app.models.analysis import AnalysisJob
from backend.app.models.character import (
    ChapterCharacterExtraction,
    Character,
    CharacterAnalysis,
    CharacterAppearance,
    CharacterRelationship,
)
from backend.app.models.novel import Chapter, Novel


@pytest.fixture()
def session_factory() -> Generator[Callable[[], Session], None, None]:
    engine = create_db_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
    )
    try:
        yield factory
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()




def test_character_routes_are_registered() -> None:
    paths = app.openapi()["paths"]

    assert "get" in paths["/api/novels/{novel_id}/characters/estimate"]
    assert {"get", "post"} <= set(
        paths["/api/novels/{novel_id}/characters"]
    )
    assert "get" in paths[
        "/api/novels/{novel_id}/characters/{character_id}"
    ]
    assert "get" in paths[
        "/api/novels/{novel_id}/characters/{character_id}/appearances"
    ]
    assert "get" in paths[
        "/api/novels/{novel_id}/characters/{character_id}/relationships"
    ]








@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("confirm_external_processing", "true"),
        ("confirm_external_processing", 1),
        ("confirm_content_rights", "true"),
        ("confirm_content_rights", 1),
        ("force", "false"),
        ("force", 0),
    ],
)
def test_character_start_request_rejects_coerced_booleans(
    field_name: str,
    value: object,
) -> None:
    payload: dict[str, object] = {
        "confirm_external_processing": True,
        "confirm_content_rights": True,
        "force": False,
    }
    payload[field_name] = value
    with pytest.raises(ValidationError):
        StartCharacterAnalysisRequest.model_validate(payload)


def test_character_estimate_reports_cache_and_force_upper_bound(
    session_factory: Callable[[], Session],
) -> None:
    settings = load_settings({})
    with session_factory() as session:
        novel = _add_novel(session)
        estimate = get_character_estimate(
            novel.id,
            settings=settings,
            db=session,
        )
        assert estimate.chapter_count == 2
        assert estimate.character_count == 15
        assert estimate.cached_chapter_count == 0
        assert estimate.pending_chapter_count == 2
        assert estimate.estimated_extraction_steps == 2
        assert estimate.estimated_generation_steps == 4
        assert estimate.estimated_force_generation_steps == 4
        assert estimate.maximum_request_attempts == 115_380
        assert estimate.maximum_force_request_attempts == 115_380
        assert estimate.up_to_date is False

        _add_snapshot(session, novel)
        cached = get_character_estimate(
            novel.id,
            settings=settings,
            db=session,
        )
        assert cached.cached_chapter_count == 2
        assert cached.pending_chapter_count == 0
        assert cached.estimated_generation_steps == 0
        assert cached.maximum_request_attempts == 0
        assert cached.estimated_force_generation_steps == 4
        assert cached.maximum_force_request_attempts == 115_380
        assert cached.up_to_date is True


def test_character_estimate_treats_invalid_cached_payload_as_pending(
    session_factory: Callable[[], Session],
) -> None:
    settings = load_settings({})
    with session_factory() as session:
        novel = _add_novel(session)
        _add_current_extractions(session, novel)
        invalid = (
            session.query(ChapterCharacterExtraction)
            .filter(
                ChapterCharacterExtraction.chapter_id
                == novel.chapters[0].id
            )
            .one()
        )
        invalid.extraction_data = {"observations": "invalid"}
        session.commit()

        estimate = get_character_estimate(
            novel.id,
            settings=settings,
            db=session,
        )

    assert estimate.cached_chapter_count == 1
    assert estimate.pending_chapter_count == 1
    assert estimate.estimated_extraction_steps == 1
    assert estimate.estimated_generation_steps == 3
    assert estimate.up_to_date is False


@pytest.mark.parametrize(
    ("external_consent", "rights_consent"),
    [(False, True), (True, False)],
)
def test_character_start_defers_provider_until_both_consents(
    session_factory: Callable[[], Session],
    external_consent: bool,
    rights_consent: bool,
) -> None:
    provider_factory = Mock()
    with session_factory() as session:
        novel = _add_novel(session)
        with pytest.raises(HTTPException) as error:
            start_character_analysis(
                novel_id=novel.id,
                request=StartCharacterAnalysisRequest(
                    confirm_external_processing=external_consent,
                    confirm_content_rights=rights_consent,
                ),
                background_tasks=Mock(),
                db=session,
                settings=load_settings({}),
                provider_factory=provider_factory,
                session_factory=session_factory,
            )
    assert error.value.status_code == 400
    provider_factory.assert_not_called()


def test_character_job_scheduling_is_idempotent_and_scope_safe(
    session_factory: Callable[[], Session],
) -> None:
    tasks = Mock()
    first_provider = Mock()
    second_provider = Mock()
    request = StartCharacterAnalysisRequest(
        confirm_external_processing=True,
        confirm_content_rights=True,
    )
    with session_factory() as session:
        novel = _add_novel(session)
        first = _schedule_character_job(
            novel_id=novel.id,
            progress_total=4,
            request=request,
            background_tasks=tasks,
            db=session,
            provider=first_provider,
            session_factory=session_factory,
        )
        second = _schedule_character_job(
            novel_id=novel.id,
            progress_total=4,
            request=request,
            background_tasks=tasks,
            db=session,
            provider=second_provider,
            session_factory=session_factory,
        )
        assert first.id == second.id
        assert session.query(AnalysisJob).count() == 1
    tasks.add_task.assert_called_once()
    first_provider.close.assert_not_called()
    second_provider.close.assert_called_once_with()


def test_character_start_reuses_active_job_before_provider_creation(
    session_factory: Callable[[], Session],
) -> None:
    provider_factory = Mock(side_effect=RuntimeError("must not run"))
    request = StartCharacterAnalysisRequest(
        confirm_external_processing=True,
        confirm_content_rights=True,
    )
    with session_factory() as session:
        novel = _add_novel(session)
        session.add(
            AnalysisJob(
                id="active-before-provider",
                novel_id=novel.id,
                job_type="characters",
                status="running",
                progress_current=1,
                progress_total=4,
                force_refresh=False,
            )
        )
        session.commit()

        response = start_character_analysis(
            novel_id=novel.id,
            request=request,
            background_tasks=Mock(),
            db=session,
            settings=load_settings({}),
            provider_factory=provider_factory,
            session_factory=session_factory,
        )

    assert response.id == "active-before-provider"
    provider_factory.assert_not_called()


def test_character_scheduler_closes_provider_on_early_query_error(
    session_factory: Callable[[], Session],
) -> None:
    provider = Mock()
    db = Mock()
    db.query.side_effect = OperationalError(
        "SELECT analysis_jobs",
        {},
        RuntimeError("private database error"),
    )

    with pytest.raises(OperationalError):
        _schedule_character_job(
            novel_id=1,
            progress_total=4,
            request=StartCharacterAnalysisRequest(
                confirm_external_processing=True,
                confirm_content_rights=True,
            ),
            background_tasks=Mock(),
            db=db,
            provider=provider,
            session_factory=session_factory,
        )

    provider.close.assert_called_once_with()


def test_character_job_rejects_conflicting_force_scope(
    session_factory: Callable[[], Session],
) -> None:
    provider = Mock()
    with session_factory() as session:
        novel = _add_novel(session)
        session.add(
            AnalysisJob(
                id="active-characters",
                novel_id=novel.id,
                job_type="characters",
                status="running",
                progress_current=0,
                progress_total=4,
                force_refresh=False,
            )
        )
        session.commit()
        with pytest.raises(HTTPException) as error:
            _schedule_character_job(
                novel_id=novel.id,
                progress_total=4,
                request=StartCharacterAnalysisRequest(
                    confirm_external_processing=True,
                    confirm_content_rights=True,
                    force=True,
                ),
                background_tasks=Mock(),
                db=session,
                provider=provider,
                session_factory=session_factory,
            )
    assert error.value.status_code == 409
    provider.close.assert_called_once_with()


def test_dispatch_failure_terminalizes_committed_character_job(
    session_factory: Callable[[], Session],
) -> None:
    tasks = Mock()
    tasks.add_task.side_effect = RuntimeError(
        "private dispatch details"
    )
    provider = Mock()
    with session_factory() as session:
        novel = _add_novel(session)
        with pytest.raises(RuntimeError, match="private dispatch"):
            _schedule_character_job(
                novel_id=novel.id,
                progress_total=4,
                request=StartCharacterAnalysisRequest(
                    confirm_external_processing=True,
                    confirm_content_rights=True,
                ),
                background_tasks=tasks,
                db=session,
                provider=provider,
                session_factory=session_factory,
            )
    with session_factory() as session:
        job = session.query(AnalysisJob).one()
        assert job.status == "failed"
        assert job.completed_at is not None
        assert job.error_message == (
            "人物分析任务未能加入后台队列，请重试。"
        )
        assert "private" not in job.error_message
    provider.close.assert_called_once_with()


def test_character_results_search_sort_and_totals(
    session_factory: Callable[[], Session],
) -> None:
    with session_factory() as session:
        novel = _add_novel(session)
        analysis, lin, _ = _add_snapshot(session, novel)
        response = list_characters(
            novel.id,
            offset=0,
            limit=20,
            search="阿舟",
            sort="mention_count",
            db=session,
        )
        assert response.total == 1
        assert response.character_count == 2
        assert response.relationship_count == 1
        assert response.analyzed_chapter_count == 2
        assert response.analysis_updated_at == analysis.updated_at
        assert response.items[0].id == lin.id
        assert response.items[0].relationship_count == 1


def test_character_detail_appearances_and_relationships_are_scoped(
    session_factory: Callable[[], Session],
) -> None:
    with session_factory() as session:
        novel = _add_novel(session)
        analysis, lin, su = _add_snapshot(session, novel)
        detail = get_character(novel.id, lin.id, db=session)
        appearances = list_character_appearances(
            novel.id,
            lin.id,
            offset=0,
            limit=100,
            db=session,
        )
        relationships = list_character_relationships(
            novel.id,
            lin.id,
            offset=0,
            limit=100,
            db=session,
        )
        assert detail.canonical_name == "林舟"
        assert detail.model == analysis.model
        assert appearances.total == 2
        assert [item.chapter_index for item in appearances.items] == [1, 2]
        assert relationships.total == 1
        assert relationships.items[0].source_character_id == lin.id
        assert relationships.items[0].target_character_id == su.id

        other_novel = _add_novel(session, filename="other.txt")
        with pytest.raises(HTTPException) as error:
            get_character(other_novel.id, lin.id, db=session)
        assert error.value.status_code == 404


def test_character_list_is_empty_before_first_analysis(
    session_factory: Callable[[], Session],
) -> None:
    with session_factory() as session:
        novel = _add_novel(session)
        response = list_characters(
            novel.id,
            offset=0,
            limit=20,
            search=None,
            sort="mention_count",
            db=session,
        )
    assert response.items == []
    assert response.total == 0
    assert response.analysis_updated_at is None
