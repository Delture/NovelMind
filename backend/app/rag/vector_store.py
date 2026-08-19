"""基于独立 SQLite 文件和 NumPy 的本地精确向量存储。

外部服务只生成向量；持久化和检索在本机完成。集合使用版本化名称，建新索引
不会修改当前集合。搜索读取 float32 向量并计算余弦相似度，是线性精确扫描，
适合当前单机规模。本库不存小说原文，原文必须由主数据库按范围重新读取。
"""

import heapq
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Sequence

import numpy as np


_COLLECTION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SQLITE_PARAMETER_BATCH_SIZE = 500
_QUERY_FETCH_SIZE = 256


class VectorCollectionNotFoundError(LookupError):
    """索引指向的真实集合不存在，调用层通常应提示重建索引。"""


@dataclass(frozen=True)
class VectorSearchHit:
    """一次向量命中；相似度用于排序，并不单独构成事实证据。"""

    chunk_id: str
    distance: float
    metadata: Mapping[str, object]


class LocalVectorStore:
    """管理版本化集合，并在本机执行精确余弦检索。"""

    def __init__(self, path: str) -> None:
        """解析/创建存储目录，固定数据库名 ``vectors.sqlite3`` 并初始化表结构。"""
        resolved = Path(path).expanduser().resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        self._database_path = resolved / "vectors.sqlite3"
        self._initialize()

    @staticmethod
    def _validate_collection_name(name: str) -> str:
        """仅允许受控 ASCII 集合名，非法类型或格式抛 ValueError，成功返回原名。"""
        if not isinstance(name, str) or not _COLLECTION_NAME_PATTERN.fullmatch(
            name
        ):
            raise ValueError("invalid vector collection name")
        return name

    def _connect(self) -> sqlite3.Connection:
        """创建启用外键、busy timeout 和行对象访问的短生命周期 sqlite3 连接。"""
        connection = sqlite3.connect(
            self._database_path,
            timeout=30.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        """幂等创建本地向量库的集合表、向量表和查询索引。

        使用 ``with self._connect()`` 让 DDL 在成功时提交、失败时回滚。函数只补齐
        缺失结构，不删除或重建已有集合，因此每次 LocalVectorStore 构造都可调用。
        """
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS vector_collections (
                    name TEXT NOT NULL PRIMARY KEY,
                    dimensions INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK (
                        dimensions IS NULL OR dimensions > 0
                    )
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS vector_entries (
                    collection_name TEXT NOT NULL,
                    chunk_id TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    embedding BLOB NOT NULL,
                    norm REAL NOT NULL,
                    metadata TEXT NOT NULL,
                    PRIMARY KEY (collection_name, chunk_id),
                    CHECK (
                        length(trim(chunk_id)) BETWEEN 1 AND 256
                    ),
                    CHECK (dimensions > 0),
                    CHECK (norm > 0),
                    CHECK (
                        json_valid(metadata)
                        AND json_type(metadata) = 'object'
                    ),
                    FOREIGN KEY(collection_name)
                        REFERENCES vector_collections(name)
                        ON DELETE CASCADE
                )
                """
            )

    def create_collection(self, name: str) -> None:
        """Create one empty, versioned logical collection."""
        name = self._validate_collection_name(name)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO vector_collections (name) VALUES (?)",
                (name,),
            )

    def delete_collection(self, name: str) -> None:
        """Delete one logical collection; missing names are ignored."""
        name = self._validate_collection_name(name)
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM vector_collections WHERE name = ?",
                (name,),
            )

    def _collection_dimensions(
        self,
        connection: sqlite3.Connection,
        name: str,
    ) -> int | None:
        """读取集合已固定的向量维度；空集合尚无维度时返回 None。"""
        row = connection.execute(
            "SELECT dimensions FROM vector_collections WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            raise VectorCollectionNotFoundError(
                f"vector collection {name!r} does not exist"
            )
        value = row["dimensions"]
        return int(value) if value is not None else None

    def collection_count(self, name: str) -> int:
        """Return the number of vectors in one logical collection."""
        name = self._validate_collection_name(name)
        with self._connect() as connection:
            self._collection_dimensions(connection, name)
            row = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM vector_entries
                WHERE collection_name = ?
                """,
                (name,),
            ).fetchone()
        return int(row["total"])

    def list_collection_names(self) -> list[str]:
        """Return collection names without loading vector blobs."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT name FROM vector_collections ORDER BY name"
            ).fetchall()
        return [str(row["name"]) for row in rows]

    @staticmethod
    def _prepare_vector(
        vector: Sequence[float],
    ) -> tuple[np.ndarray, float]:
        """把输入转为一维 float32 数组并返回 ``(数组, L2 范数)``。

        拒绝空、非一维、非有限值或零向量，因为余弦相似度将无法定义。
        """
        try:
            array = np.asarray(vector, dtype="<f4")
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("vector must contain finite numbers") from error
        if array.ndim != 1 or array.size == 0:
            raise ValueError("vector must be one-dimensional and non-empty")
        if not bool(np.isfinite(array).all()):
            raise ValueError("vector must contain finite numbers")
        norm = float(np.linalg.vector_norm(array))
        if not math.isfinite(norm) or norm <= 0.0:
            raise ValueError("vector must have a finite non-zero norm")
        return array, norm

    def upsert(
        self,
        *,
        collection_name: str,
        ids: Sequence[str],
        embeddings: Sequence[Sequence[float]],
        metadatas: Sequence[Mapping[str, object]],
    ) -> None:
        """Write external vectors and metadata, never source documents."""
        collection_name = self._validate_collection_name(collection_name)
        if not ids:
            return
        if not (len(ids) == len(embeddings) == len(metadatas)):
            raise ValueError(
                "ids, embeddings, and metadatas must have equal lengths"
            )
        if len(set(ids)) != len(ids):
            raise ValueError("vector ids cannot be duplicated in one upsert")

        rows: list[tuple[str, str, int, bytes, float, str]] = []
        expected_dimensions: int | None = None
        for chunk_id, vector, metadata in zip(
            ids,
            embeddings,
            metadatas,
            strict=True,
        ):
            if (
                not isinstance(chunk_id, str)
                or not 1 <= len(chunk_id.strip()) <= 256
            ):
                raise ValueError("vector id must be a non-empty string")
            array, norm = self._prepare_vector(vector)
            dimensions = int(array.size)
            if expected_dimensions is None:
                expected_dimensions = dimensions
            elif expected_dimensions != dimensions:
                raise ValueError("all vectors must have equal dimensions")
            try:
                encoded_metadata = json.dumps(
                    dict(metadata),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                    allow_nan=False,
                )
            except (TypeError, ValueError, OverflowError) as error:
                raise ValueError(
                    "vector metadata must be a finite JSON object"
                ) from error
            rows.append(
                (
                    collection_name,
                    chunk_id,
                    dimensions,
                    array.tobytes(order="C"),
                    norm,
                    encoded_metadata,
                )
            )
        assert expected_dimensions is not None

        with self._connect() as connection:
            stored_dimensions = self._collection_dimensions(
                connection,
                collection_name,
            )
            if stored_dimensions is None:
                connection.execute(
                    """
                    UPDATE vector_collections
                    SET dimensions = ?
                    WHERE name = ? AND dimensions IS NULL
                    """,
                    (expected_dimensions, collection_name),
                )
                stored_dimensions = self._collection_dimensions(
                    connection,
                    collection_name,
                )
            if stored_dimensions != expected_dimensions:
                raise ValueError(
                    "vector dimensions do not match the collection"
                )
            connection.executemany(
                """
                INSERT INTO vector_entries (
                    collection_name,
                    chunk_id,
                    dimensions,
                    embedding,
                    norm,
                    metadata
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(collection_name, chunk_id) DO UPDATE SET
                    dimensions = excluded.dimensions,
                    embedding = excluded.embedding,
                    norm = excluded.norm,
                    metadata = excluded.metadata
                """,
                rows,
            )

    def get_embeddings(
        self,
        *,
        collection_name: str,
        ids: Sequence[str],
    ) -> dict[str, tuple[float, ...]]:
        """按稳定分块 ID 批量读取缓存向量。

        ``collection_name`` 先经名称校验，``ids`` 去重后用于参数化 SQL。返回仅包含
        实际存在且 blob 长度符合集合维度的 ``id -> tuple[float, ...]``；未找到 ID
        不出现在字典中，调用者据此决定重新 embedding。
        """
        collection_name = self._validate_collection_name(collection_name)
        if not ids:
            return {}
        result: dict[str, tuple[float, ...]] = {}
        with self._connect() as connection:
            self._collection_dimensions(connection, collection_name)
            for offset in range(
                0,
                len(ids),
                _SQLITE_PARAMETER_BATCH_SIZE,
            ):
                batch = list(
                    ids[offset:offset + _SQLITE_PARAMETER_BATCH_SIZE]
                )
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"""
                    SELECT chunk_id, dimensions, embedding
                    FROM vector_entries
                    WHERE collection_name = ?
                      AND chunk_id IN ({placeholders})
                    """,
                    (collection_name, *batch),
                ).fetchall()
                for row in rows:
                    dimensions = int(row["dimensions"])
                    vector = np.frombuffer(
                        row["embedding"],
                        dtype="<f4",
                    )
                    if vector.size != dimensions:
                        raise RuntimeError(
                            "stored vector has an invalid byte length"
                        )
                    result[str(row["chunk_id"])] = tuple(
                        float(value) for value in vector
                    )
        return result

    def _iter_query_hits(
        self,
        connection: sqlite3.Connection,
        *,
        collection_name: str,
        query_vector: np.ndarray,
        query_norm: float,
        dimensions: int,
    ) -> Iterator[VectorSearchHit]:
        """逐行解码集合向量并 yield 余弦距离命中，避免一次复制所有元数据。

        参数 query_vector/query_norm 已预校验，dimensions 用于检查磁盘 blob 长度。
        ``Iterator`` 表示调用方按需拉取结果；本函数本身含 yield，是生成器。
        """
        cursor = connection.execute(
            """
            SELECT chunk_id, embedding, norm, metadata
            FROM vector_entries
            WHERE collection_name = ?
            """,
            (collection_name,),
        )
        while True:
            rows = cursor.fetchmany(_QUERY_FETCH_SIZE)
            if not rows:
                break
            for row in rows:
                vector = np.frombuffer(row["embedding"], dtype="<f4")
                if vector.size != dimensions:
                    raise RuntimeError(
                        "stored vector has an invalid byte length"
                    )
                norm = float(row["norm"])
                similarity = float(
                    np.dot(query_vector, vector) / (query_norm * norm)
                )
                if not math.isfinite(similarity):
                    raise RuntimeError(
                        "stored vector produced an invalid similarity"
                    )
                distance = max(0.0, min(2.0, 1.0 - similarity))
                metadata = json.loads(str(row["metadata"]))
                if not isinstance(metadata, dict):
                    raise RuntimeError(
                        "stored vector metadata is not an object"
                    )
                yield VectorSearchHit(
                    chunk_id=str(row["chunk_id"]),
                    distance=distance,
                    metadata=metadata,
                )

    def query(
        self,
        *,
        collection_name: str,
        embedding: Sequence[float],
        limit: int,
    ) -> list[VectorSearchHit]:
        """Return exact nearest neighbors for an external query vector."""
        collection_name = self._validate_collection_name(collection_name)
        if isinstance(limit, bool) or not 1 <= int(limit) <= 100:
            raise ValueError("query limit must be between 1 and 100")
        query_vector, query_norm = self._prepare_vector(embedding)
        with self._connect() as connection:
            dimensions = self._collection_dimensions(
                connection,
                collection_name,
            )
            if dimensions is None:
                return []
            if int(query_vector.size) != dimensions:
                raise ValueError(
                    "query dimensions do not match the collection"
                )
            return heapq.nsmallest(
                int(limit),
                self._iter_query_hits(
                    connection,
                    collection_name=collection_name,
                    query_vector=query_vector,
                    query_norm=query_norm,
                    dimensions=dimensions,
                ),
                key=lambda hit: (hit.distance, hit.chunk_id),
            )
