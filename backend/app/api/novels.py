
import logging
from datetime import datetime
from pathlib import Path

from charset_normalizer import from_bytes

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
from starlette.concurrency import run_in_threadpool

from backend.app.core.database import get_db
from backend.app.core.config import Settings, get_settings
from backend.app.models.novel import Chapter as ChapterModel
from backend.app.models.novel import Novel
from backend.app.models.rag import RagIndex
from backend.app.parser.chapter_splitter import split_chapters
from backend.app.rag.vector_store import LocalVectorStore


router = APIRouter(prefix="/novels", tags=["novels"])
logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_CHAPTERS = 10_000
MAX_FILENAME_CHARACTERS = 255
MAX_UPLOAD_RESPONSE_CHAPTERS = 200


# API 响应模型是对外合同。显式列出字段，避免数据库新增内部字段后意外泄露。
class ChapterResponse(BaseModel):
    index: int
    title: str
    character_count: int


class NovelUploadResponse(BaseModel):
    novel_id: int
    filename: str
    encoding: str
    chapter_count: int
    chapters: list[ChapterResponse]
    chapters_truncated: bool


class NovelSummaryResponse(BaseModel):
    id: int
    filename: str
    encoding: str
    chapter_count: int
    character_count: int
    created_at: datetime


class NovelListResponse(BaseModel):
    items: list[NovelSummaryResponse]
    total: int
    offset: int
    limit: int


class ChapterSummaryResponse(BaseModel):
    id: int
    index: int
    title: str
    character_count: int


class ChapterListResponse(BaseModel):
    items: list[ChapterSummaryResponse]
    total: int
    offset: int
    limit: int


class ChapterDetailResponse(ChapterSummaryResponse):
    novel_id: int
    content: str


def get_novel_or_404(db: Session, novel_id: int) -> Novel:
    novel = db.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到指定小说。",
        )
    return novel


def decode_txt(content: bytes) -> tuple[str, str]:
    try:
        return content.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        pass

    allowed_gb_encodings = {"gb18030", "gbk", "gb2312", "cp936"}
    detected_gb = next(
        (
            candidate
            for candidate in from_bytes(content)
            if candidate.encoding
            and candidate.encoding.lower().replace("-", "_")
            in allowed_gb_encodings
            and float(candidate.chaos) < 0.1
        ),
        None,
    )
    if detected_gb is not None:
        try:
            return content.decode("gb18030"), "gb18030"
        except UnicodeDecodeError:
            pass

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="无法识别 TXT 文件编码，请使用 UTF-8 或 GB18030 编码。",
    )


def validate_decoded_text(text: str) -> None:
    """拒绝虽能解码却明显像乱码或二进制的数据，避免污染永久存储。"""
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
    """任何数据库错误 rollback 后返回 500；小说与全部章节在同一事务提交。"""
    content_type = request.headers.get("content-type", "")
    media_type = content_type.partition(";")[0].strip().lower()
    if media_type != "application/octet-stream":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="上传请求必须使用 application/octet-stream。",
        )
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

    return await run_in_threadpool(_save_uploaded_novel, bytes(content), safe_filename, db)


def _save_uploaded_novel(content: bytes, safe_filename: str, db: Session) -> NovelUploadResponse:
    """在线程内完成，不占用 ASGI 事件循环。"""
    text, encoding = decode_txt(content)
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

    # 小说和全部章节必须一起成功；失败便 rollback，不能留下半部小说。
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
                character_count=chapter.character_count,
            )
            for chapter in novel.chapters[:MAX_UPLOAD_RESPONSE_CHAPTERS]
        ],
        chapters_truncated=(
            novel.chapter_count > MAX_UPLOAD_RESPONSE_CHAPTERS
        ),
    )


@router.get("", response_model=NovelListResponse)
def list_novels(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> NovelListResponse:
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
    settings: Settings = Depends(get_settings),
) -> Response:
    novel = get_novel_or_404(db, novel_id)
    collection_name = db.query(RagIndex.collection_name).filter(
        RagIndex.novel_id == novel_id
    ).scalar()
    try:
        db.delete(novel)
        db.commit()
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="小说删除失败，请稍后重试。",
        ) from error
    if collection_name:
        try:
            LocalVectorStore(
                settings.vector_store_path
            ).delete_collection(
                str(collection_name)
            )
        except Exception:
            logger.exception(
                "Novel %s was deleted but its derived RAG collection "
                "%s could not be cleaned immediately.",
                novel_id,
                collection_name,
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{novel_id}/search")
def search_novel_text(novel_id: int = PathParameter(..., ge=1),
                      q: str = Query(..., min_length=1, max_length=200),
                      offset: int = Query(0, ge=0, le=10000),
                      limit: int = Query(20, ge=1, le=50),
                      db: Session = Depends(get_db)) -> dict:
    """不调用外部模型或写阅读状态。"""
    from backend.app.services.text_search import search_text
    get_novel_or_404(db, novel_id)
    query = q.strip()
    if not query or any(character in q for character in ("\r", "\n", "\x00")):
        raise HTTPException(422, "请输入1至200个字符的单行搜索内容。")
    return search_text(db, novel_id, query, offset, limit)
