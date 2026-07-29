"""API endpoints for uploading novels."""

from datetime import datetime
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path as PathParameter,
    Query,
    Request,
    Response,
    status,
)
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.models.novel import Chapter as ChapterModel
from backend.app.models.novel import Novel
from backend.app.parser.chapter_splitter import split_chapters


router = APIRouter(prefix="/novels", tags=["novels"])

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_CHAPTERS = 10_000
MAX_FILENAME_CHARACTERS = 255


class ChapterResponse(BaseModel):
    """Chapter data returned after parsing an uploaded novel."""

    index: int
    title: str
    content: str
    character_count: int


class NovelUploadResponse(BaseModel):
    """Result of uploading and parsing one TXT novel."""

    novel_id: int
    filename: str
    encoding: str
    chapter_count: int
    chapters: list[ChapterResponse]


class NovelSummaryResponse(BaseModel):
    """Metadata for one novel in the saved library."""

    id: int
    filename: str
    encoding: str
    chapter_count: int
    character_count: int
    created_at: datetime


class NovelListResponse(BaseModel):
    """One page of saved novels."""

    items: list[NovelSummaryResponse]
    total: int
    offset: int
    limit: int


class ChapterSummaryResponse(BaseModel):
    """Chapter metadata returned without the full original text."""

    id: int
    index: int
    title: str
    character_count: int


class ChapterListResponse(BaseModel):
    """One page of chapter metadata."""

    items: list[ChapterSummaryResponse]
    total: int
    offset: int
    limit: int


class ChapterDetailResponse(ChapterSummaryResponse):
    """One complete chapter loaded on demand."""

    novel_id: int
    content: str


def get_novel_or_404(db: Session, novel_id: int) -> Novel:
    """Return a saved novel or a stable user-facing 404 error."""
    novel = db.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到指定小说。",
        )
    return novel


def decode_txt(content: bytes) -> tuple[str, str]:
    """Decode common TXT encodings and return the text and encoding name."""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = content.decode(encoding)
            display_encoding = "utf-8" if encoding == "utf-8-sig" else encoding
            return text, display_encoding
        except UnicodeDecodeError:
            continue

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="无法识别 TXT 文件编码，请使用 UTF-8 或 GB18030 编码。",
    )


def validate_decoded_text(text: str) -> None:
    """Reject binary control characters that should not appear in TXT input."""
    allowed_controls = {"\n", "\r", "\t", "\f"}
    if any(
        ord(character) < 32 and character not in allowed_controls
        for character in text
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TXT 文件包含无效的二进制控制字符。",
        )


@router.post(
    "/upload",
    response_model=NovelUploadResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        }
    },
)
async def upload_novel(
    request: Request,
    filename: str = Query(..., description="上传的 TXT 文件名"),
    db: Session = Depends(get_db),
) -> NovelUploadResponse:
    """Upload, parse, and save a TXT novel."""
    safe_filename = Path(filename).name
    if Path(safe_filename).suffix.lower() != ".txt":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="只支持上传 TXT 文件。",
        )
    if len(safe_filename) > MAX_FILENAME_CHARACTERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TXT 文件名不能超过 255 个字符。",
        )

    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="TXT 文件不能超过 20 MB。",
            )

    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TXT 文件内容为空。",
        )

    text, encoding = decode_txt(bytes(content))
    validate_decoded_text(text)
    chapters = split_chapters(text)
    if not chapters:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TXT 文件内容为空。",
        )
    if len(chapters) > MAX_CHAPTERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"单本小说不能超过 {MAX_CHAPTERS} 个章节。",
        )

    chapter_models = [
        ChapterModel(
            chapter_index=chapter.index,
            title=chapter.title,
            content=chapter.content,
            character_count=len(chapter.content),
        )
        for chapter in chapters
    ]

    novel = Novel(
        filename=safe_filename,
        encoding=encoding,
        chapter_count=len(chapter_models),
        chapters=chapter_models,
    )

    try:
        db.add(novel)
        db.commit()
        db.refresh(novel)
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="小说保存失败，请稍后重试。",
        ) from error

    return NovelUploadResponse(
        novel_id=novel.id,
        filename=novel.filename,
        encoding=novel.encoding,
        chapter_count=novel.chapter_count,
        chapters=[
            ChapterResponse(
                index=chapter.chapter_index,
                title=chapter.title,
                content=chapter.content,
                character_count=chapter.character_count,
            )
            for chapter in novel.chapters
        ],
    )


@router.get("", response_model=NovelListResponse)
def list_novels(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> NovelListResponse:
    """Return saved novels without loading their chapter text."""
    chapter_totals = (
        db.query(
            ChapterModel.novel_id.label("novel_id"),
            func.sum(ChapterModel.character_count).label("character_count"),
        )
        .group_by(ChapterModel.novel_id)
        .subquery()
    )
    rows = (
        db.query(
            Novel,
            func.coalesce(chapter_totals.c.character_count, 0),
        )
        .outerjoin(chapter_totals, chapter_totals.c.novel_id == Novel.id)
        .order_by(Novel.created_at.desc(), Novel.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    total = db.query(func.count(Novel.id)).scalar() or 0

    return NovelListResponse(
        items=[
            NovelSummaryResponse(
                id=novel.id,
                filename=novel.filename,
                encoding=novel.encoding,
                chapter_count=novel.chapter_count,
                character_count=int(character_count),
                created_at=novel.created_at,
            )
            for novel, character_count in rows
        ],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/{novel_id}", response_model=NovelSummaryResponse)
def get_novel(
    novel_id: int = PathParameter(..., ge=1),
    db: Session = Depends(get_db),
) -> NovelSummaryResponse:
    """Return aggregate metadata for one saved novel."""
    novel = get_novel_or_404(db, novel_id)
    character_count = (
        db.query(func.coalesce(func.sum(ChapterModel.character_count), 0))
        .filter(ChapterModel.novel_id == novel_id)
        .scalar()
    )
    return NovelSummaryResponse(
        id=novel.id,
        filename=novel.filename,
        encoding=novel.encoding,
        chapter_count=novel.chapter_count,
        character_count=int(character_count or 0),
        created_at=novel.created_at,
    )


@router.get(
    "/{novel_id}/chapters",
    response_model=ChapterListResponse,
)
def list_chapters(
    novel_id: int = PathParameter(..., ge=1),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> ChapterListResponse:
    """Return chapter metadata without loading the full body text."""
    get_novel_or_404(db, novel_id)
    total = (
        db.query(func.count(ChapterModel.id))
        .filter(ChapterModel.novel_id == novel_id)
        .scalar()
        or 0
    )
    chapters = (
        db.query(
            ChapterModel.id,
            ChapterModel.chapter_index,
            ChapterModel.title,
            ChapterModel.character_count,
        )
        .filter(ChapterModel.novel_id == novel_id)
        .order_by(ChapterModel.chapter_index)
        .offset(offset)
        .limit(limit)
        .all()
    )
    return ChapterListResponse(
        items=[
            ChapterSummaryResponse(
                id=chapter.id,
                index=chapter.chapter_index,
                title=chapter.title,
                character_count=chapter.character_count,
            )
            for chapter in chapters
        ],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/{novel_id}/chapters/{chapter_index}",
    response_model=ChapterDetailResponse,
)
def get_chapter(
    novel_id: int = PathParameter(..., ge=1),
    chapter_index: int = PathParameter(..., ge=1),
    db: Session = Depends(get_db),
) -> ChapterDetailResponse:
    """Return one full chapter by its display index."""
    get_novel_or_404(db, novel_id)
    chapter = (
        db.query(ChapterModel)
        .filter(
            ChapterModel.novel_id == novel_id,
            ChapterModel.chapter_index == chapter_index,
        )
        .one_or_none()
    )
    if chapter is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到指定章节。",
        )
    return ChapterDetailResponse(
        id=chapter.id,
        novel_id=chapter.novel_id,
        index=chapter.chapter_index,
        title=chapter.title,
        content=chapter.content,
        character_count=chapter.character_count,
    )


@router.delete(
    "/{novel_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_novel(
    novel_id: int = PathParameter(..., ge=1),
    db: Session = Depends(get_db),
) -> Response:
    """Delete one novel and all of its dependent records."""
    novel = get_novel_or_404(db, novel_id)
    try:
        db.delete(novel)
        db.commit()
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="小说删除失败，请稍后重试。",
        ) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)
