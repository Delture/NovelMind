"""API endpoints for uploading novels."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.models.novel import Chapter as ChapterModel
from backend.app.models.novel import Novel
from backend.app.parser.chapter_splitter import split_chapters


router = APIRouter(prefix="/novels", tags=["novels"])

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


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
    if Path(filename).suffix.lower() != ".txt":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="只支持上传 TXT 文件。",
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
    chapters = split_chapters(text)
    if not chapters:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TXT 文件内容为空。",
        )

    filename = Path(filename).name
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
        filename=filename,
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
