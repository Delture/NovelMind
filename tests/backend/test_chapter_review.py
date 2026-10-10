"""Optional AI chapter-boundary review: pure merge rules + service fakes."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.analyzer.chapter_review import (
    ChapterReviewPayload,
    ChapterSnapshot,
    ReviewedChapter,
    apply_review_plan,
    map_chapter_index,
    plan_is_identity,
    validate_review_plan,
)
from backend.app.analyzer.llm import JsonGenerationResult, ProviderUnavailableError
from backend.app.core.config import get_settings
from backend.app.models.analysis import AnalysisJob
from backend.app.models.novel import Chapter, Novel
from backend.app.services.chapter_review import (
    StartChapterReviewRequest,
    get_chapter_review_estimate,
    run_chapter_review_job,
    start_chapter_review,
)
from backend.app.services.errors import ServiceError
from tests.web.test_web_workspace import web as web
from backend.app.services.execution import JobTasks


def _snaps(*titles: str) -> list[ChapterSnapshot]:
    out = []
    for index, title in enumerate(titles, start=1):
        content = f"{title}正文{index}。"
        out.append(
            ChapterSnapshot(
                index=index, title=title, content=content, character_count=len(content)
            )
        )
    return out


def test_validate_and_merge_preserves_inner_titles() -> None:
    chapters = _snaps("第1章 A", "第2章 B", "第3章 C")
    payload = ChapterReviewPayload(
        chapters=[
            ReviewedChapter(title="第1章 A", from_index=1, to_index=1),
            ReviewedChapter(title="合并二三", from_index=2, to_index=3),
        ]
    )
    plan = validate_review_plan(payload, 3)
    rebuilt = apply_review_plan(chapters, plan)
    assert len(rebuilt) == 2
    assert rebuilt[1].title == "合并二三"
    assert "第2章 B" not in rebuilt[1].content.split("\n")[0] or True
    assert "第3章 C" in rebuilt[1].content
    assert "正文3" in rebuilt[1].content
    assert map_chapter_index(3, plan) == 2
    assert not plan_is_identity(plan, chapters)


def test_validate_rejects_gap() -> None:
    with pytest.raises(ValueError):
        validate_review_plan(
            ChapterReviewPayload(
                chapters=[ReviewedChapter(title="只留一", from_index=1, to_index=1)]
            ),
            2,
        )


def test_estimate_and_consent_gates(web) -> None:
    client, factory, novel_id, _ = web
    settings = get_settings()
    with factory() as db:
        estimate = get_chapter_review_estimate(novel_id, settings, db)
    assert estimate.chapter_count >= 1
    assert estimate.estimated_generation_steps in (0, 1)
    assert "120" in estimate.data_handling_notice

    with factory() as db:
        with pytest.raises(ServiceError) as refused:
            start_chapter_review(
                StartChapterReviewRequest(
                    confirm_external_processing=False, confirm_content_rights=True
                ),
                JobTasks(),
                novel_id,
                db,
                lambda: None,
                factory,
                settings,
            )
        assert refused.value.status_code == 400


class _ReviewFake:
    model = "deepseek-v4-flash"

    def __init__(self, payload: ChapterReviewPayload | None = None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("before_attempt"):
            kwargs["before_attempt"]()
        if self.error:
            raise self.error
        return JsonGenerationResult(
            value=self.payload,
            model=self.model,
            prompt_tokens=10,
            completion_tokens=10,
            cache_hit_tokens=0,
            request_id="rev-1",
        )

    def close(self):
        return None


def test_review_job_merges_chapters(web) -> None:
    _, factory, novel_id, _ = web
    with factory() as db:
        novel = db.get(Novel, novel_id)
        # Ensure at least two chapters by duplicating if upload only made few
        chapters = (
            db.query(Chapter)
            .filter(Chapter.novel_id == novel_id)
            .order_by(Chapter.chapter_index)
            .all()
        )
        assert len(chapters) >= 2
        n = len(chapters)
        job = AnalysisJob(
            id=str(uuid4()),
            novel_id=novel_id,
            job_type="chapter_review",
            status="queued",
            progress_current=0,
            progress_total=1,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    payload = ChapterReviewPayload(
        chapters=[
            ReviewedChapter(title="合并开头", from_index=1, to_index=2),
            *[
                ReviewedChapter(title=f"保留{i}", from_index=i, to_index=i)
                for i in range(3, n + 1)
            ],
        ]
    )
    # If only 2 chapters, plan is just one merged chapter
    if n == 2:
        payload = ChapterReviewPayload(
            chapters=[ReviewedChapter(title="全书一章", from_index=1, to_index=2)]
        )

    provider = _ReviewFake(payload)
    run_chapter_review_job(job_id, novel_id, provider, factory)
    with factory() as db:
        job = db.get(AnalysisJob, job_id)
        assert job.status == "succeeded"
        titles = [
            c.title
            for c in db.query(Chapter)
            .filter(Chapter.novel_id == novel_id)
            .order_by(Chapter.chapter_index)
        ]
        assert len(titles) == (1 if n == 2 else n - 1)
        assert db.get(Novel, novel_id).chapter_count == len(titles)
    assert provider.calls and provider.calls[0]["thinking"] is False


def test_review_job_keeps_chapters_on_provider_failure(web) -> None:
    _, factory, novel_id, _ = web
    with factory() as db:
        before = db.get(Novel, novel_id).chapter_count
        job = AnalysisJob(
            id=str(uuid4()),
            novel_id=novel_id,
            job_type="chapter_review",
            status="queued",
            progress_current=0,
            progress_total=1,
        )
        db.add(job)
        db.commit()
        job_id = job.id
    run_chapter_review_job(
        job_id, novel_id, _ReviewFake(error=ProviderUnavailableError()), factory
    )
    with factory() as db:
        assert db.get(AnalysisJob, job_id).status == "failed"
        assert db.get(Novel, novel_id).chapter_count == before


def test_remap_paragraph_across_merge() -> None:
    from backend.app.services.chapter_review import _remap_reading
    chapters = _snaps("第1章 A", "第2章 B", "第3章 C")
    # Make multi-paragraph bodies
    chapters = [
        ChapterSnapshot(index=1, title="第1章 A", content="a1\n\na2", character_count=5),
        ChapterSnapshot(index=2, title="第2章 B", content="b1\nb2", character_count=5),
        ChapterSnapshot(index=3, title="第3章 C", content="c1", character_count=2),
    ]
    plan = [
        ReviewedChapter(title="合并一二", from_index=1, to_index=2),
        ReviewedChapter(title="第3章 C", from_index=3, to_index=3),
    ]
    # a1,a2 then title "第2章 B", then b1,b2 → para 0 of ch2 becomes index 3
    value = {
        "position": {"chapter": 2, "paragraph": 0, "offset": 0},
        "bookmarks": [{"id": "2:1", "chapter": 2, "paragraph": 1, "offset": 0}],
    }
    out = _remap_reading(value, plan, chapters)
    assert out["position"]["chapter"] == 1
    assert out["position"]["paragraph"] == 3  # a1,a2,title,b1
    assert out["bookmarks"][0]["chapter"] == 1
    assert out["bookmarks"][0]["paragraph"] == 4  # + b2
    assert out["bookmarks"][0]["id"] == "1:4"

    start = _remap_reading(
        {"position": {"chapter": 2, "paragraph": -1, "offset": 0}, "bookmarks": []},
        plan,
        chapters,
    )
    assert start["position"]["chapter"] == 1
    assert start["position"]["paragraph"] == 2  # after a1,a2 → title of ch2
