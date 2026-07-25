"""API endpoints for uploading novels."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

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
) -> NovelUploadResponse:
    """Upload a TXT novel and return its parsed chapters."""
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

    chapter_responses = [
        ChapterResponse(
            index=chapter.index,
            title=chapter.title,
            content=chapter.content,
            character_count=len(chapter.content),
        )
        for chapter in chapters
    ]

    return NovelUploadResponse(
        filename=filename,
        encoding=encoding,
        chapter_count=len(chapter_responses),
        chapters=chapter_responses,
    )
