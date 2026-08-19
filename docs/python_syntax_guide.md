# 在 NovelMind 中读懂 Python 与项目框架语法

本文不是通用 Python 教程，而是一张“看到项目代码时立即查阅”的地图。示例均
来自 NovelMind 的真实写法。先知道一句语法在表达什么，再回到业务函数理解流程。

## 1. 类型注解不是赋值

```python
def get_novel(db: Session, novel_id: int) -> Novel:
```

- `db: Session` 表示希望参数是 SQLAlchemy Session；
- `novel_id: int` 表示小说 ID 应是整数；
- `-> Novel` 表示正常返回一个 Novel ORM 对象；
- 注解主要帮助阅读器、IDE 和静态检查，不会自动把字符串转换成整数；FastAPI
  和 Pydantic 会主动读取部分注解并执行运行时校验。

常见组合：

- `str | None` 或 `Optional[str]`：字符串或空值 `None`；
- `list[Chapter]`：元素均为 Chapter 的列表；
- `tuple[int, int]`：恰好两个整数的元组；
- `Callable[[], Session]`：不接收参数、调用后返回 Session 的函数；
- `Sequence[str]`：只要求可按顺序读取字符串，不强制必须是 list；
- `Any`：此处暂时不限制类型，后续代码通常必须自行验证。

## 2. 默认参数与仅限关键字参数

```python
def start(*, development: bool, open_browser: bool = True) -> int:
```

单独的 `*` 表示它之后的参数必须写名字：

```python
start(development=False, open_browser=True)  # 清楚且正确
start(False, True)                           # TypeError
```

这适合多个布尔值。如果只写 `start(False, True)`，读者很难看出两个真假分别代表
什么。`= True` 是调用者省略该参数时使用的默认值。

函数签名里的 `**kwargs` 表示收集额外的“名字=值”参数为字典；`*args` 则把额外
位置参数收集为元组。项目只在需要把选项继续转交给第三方库时使用它们。

## 3. `@dataclass` 与不可变值对象

```python
@dataclass(frozen=True)
class RagChunk:
    chunk_id: str
    content: str
```

`@...` 是装饰器：类创建后会先交给装饰器处理。`dataclass` 根据字段自动生成
`__init__`、比较和调试显示；`frozen=True` 禁止对象创建后随意改字段。分块、
配置快照等“已经确定的值”适合不可变对象，可减少执行中被意外修改。

## 4. Pydantic `BaseModel` 与 SQLAlchemy `Base` 不相同

```python
class StartReportRequest(BaseModel): ...
class NovelReport(Base): ...
```

- Pydantic BaseModel 描述进入/离开 API 或模型调用的临时数据合同，负责验证；
- SQLAlchemy Base 描述需要长期保存的数据表，负责 ORM 映射。

两者都用 class，但生命周期不同。API 请求模型不会自动建数据库表，ORM 模型也
不应未经筛选直接作为公开响应。

## 5. Pydantic 字段、验证器和 `@classmethod`

```python
@field_validator("question")
@classmethod
def question_cannot_be_whitespace(cls, value: str) -> str:
    ...
```

Pydantic 在构造对象时调用这个函数验证 `question`。`classmethod` 的第一个参数
是类 `cls`，不是实例 `self`；验证发生在完整业务对象创建之前。字段验证器适合
检查单字段，`@model_validator` 适合比较多个字段，例如关系起点不能等于终点。

验证器应返回清洗/确认后的值；抛 `ValueError` 会被 Pydantic 汇总成可读的
ValidationError。它不是 HTTPException，因为同一模型也可能在后台任务中使用。

## 6. FastAPI 路由装饰器

```python
@router.get("/{novel_id}", response_model=NovelSummaryResponse)
def get_novel(...):
```

导入模块时，装饰器把函数登记为 HTTP GET 接口。花括号变量来自 URL 路径；
`response_model` 会校验并过滤返回数据。函数仍是普通 Python 函数，测试可以直接
调用，但通过 HTTP 时 FastAPI 会先解析参数和执行依赖。

`GET` 用于读取，`POST` 常用于创建/启动，`PUT` 用于整体更新，`DELETE` 用于删除。
HTTP 状态码是接口合同的一部分：404 表示资源不存在，409 表示当前状态冲突，
422 通常表示请求结构无法通过验证。

## 7. `Depends` 依赖注入

```python
def list_novels(db: Session = Depends(get_db)):
```

不要把 `Depends(get_db)` 当成真正的 Session 默认值。FastAPI 收到请求时会调用
`get_db`，把它 `yield` 的 Session 传给 `db`，请求结束后继续执行清理部分。
测试可以覆盖依赖，换成内存数据库或假 provider，而无需修改业务函数。

## 8. `yield`：暂停并把值交出去

```python
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

含 `yield` 的函数调用后返回生成器。执行到 yield 时暂停并交出 db；使用者结束后
恢复执行 finally。它特别适合“获取资源—使用—必定释放”。若这里写 `return db`，
函数结束后便没有机会统一关闭会话。

## 9. 上下文管理器与 `with`

```python
with session_factory() as db:
    ...
```

`with` 调用对象的进入/退出协议。离开缩进块时，即使抛异常，也会关闭 Session。
`with engine.begin() as connection` 还会在成功时提交、异常时回滚。它解决资源释放，
但不能自动替业务决定应该在哪个时刻提交。

## 10. `try / except / else / finally`

- `try`：可能失败的操作；
- `except SomeError`：只处理预期类别；
- `else`：try 没抛异常时执行；
- `finally`：无论成功失败都执行，适合关闭资源。

`raise NewError(...) from error` 会保留原异常为原因，日志中能看到完整链条。裸
`except Exception` 只应位于后台任务或启动器的最后防线，并且必须记录或转换，
不能静默吞掉。

## 11. SQLAlchemy 查询不是立即执行的普通列表

```python
query = db.query(Novel).filter(Novel.id == novel_id)
novel = query.first()
```

`query` 先描述 SQL；`.first()`、`.all()`、`.count()` 等终结操作才访问数据库。
`Novel.id == novel_id` 被 SQLAlchemy 重载成 SQL 条件，不会立即得到 Python 布尔值。

`db.add(obj)` 把对象加入工作单元，`flush()` 把 SQL 发给数据库并取得自动 ID，但
事务仍未提交；`commit()` 才让其他会话可见；`rollback()` 撤销当前事务并清除
Session 的失败状态；`refresh(obj)` 从数据库重新载入默认值等字段。

## 12. ORM `relationship` 与数据库 `ForeignKey`

`ForeignKey` 是数据库约束，保证引用行存在；`relationship` 是 Python 使用体验，
允许 `novel.chapters`。只有 relationship 而没有外键，数据库无法保护一致性。

`cascade="all, delete-orphan"` 表示从父对象移除且不再属于其他父对象的子记录可
一并删除；`ondelete="CASCADE"` 是数据库执行的级联。两层常常同时存在。

## 13. `async def`、`await` 与并发

```python
async def generate(...):
    response = await client.request(...)
```

async 函数调用后先返回协程；事件循环执行到 await 网络等待时，可切换处理其他
协程。它适合大量网络等待，不会让 CPU 计算自动变快。不能忘记 await，否则拿到
的是协程对象而非结果。

摘要按章并发请求，但数据库写入仍串行。并发不是越大越好：会受供应商限流、
连接数、内存和 SQLite 写锁约束。

## 14. `Protocol`：只约定需要的方法

```python
class EmbeddingProvider(Protocol):
    def embed(self, texts: Sequence[str]) -> EmbeddingBatchResult: ...
```

Protocol 表示“只要对象有这些方法和属性，就可使用”，实现类无需显式继承。
这让业务层依赖最小能力，测试可以传入简单 Fake。函数体中的 `...` 是 Ellipsis，
在协议中表示这里只声明形状、不提供实现。

## 15. 泛型 `TypeVar` 与 `Generic`

```python
T = TypeVar("T", bound=BaseModel)
class JsonGenerationResult(Generic[T]):
    value: T
```

T 是“某一种具体 Pydantic 模型”的占位符。若调用方请求 ChapterSummaryPayload，
类型工具便知道结果 `.value` 也是 ChapterSummaryPayload，而不是模糊的 BaseModel。
运行时核心行为仍由 Pydantic 校验完成。

## 16. 列表/集合/字典推导式

```python
ids = {item.id for item in rows if item.enabled}
```

等价于创建空 set，循环 rows，只把 enabled 项的 id 加入。`[]` 是列表推导，
`{key: value for ...}` 是字典推导。推导式适合简单映射/过滤；若包含多层条件或
副作用，普通 for 循环通常更易读。

## 17. 生成器表达式与 `any` / `all`

```python
if any(value < 0 for value in numbers): ...
```

生成器逐个产生结果，`any` 遇到第一个 True 就停止；`all` 遇到第一个 False
停止。这叫短路求值。不要为了“高级”把复杂业务条件压成难读的一行。

## 18. 解包、星号与字典合并

```python
first, second = pair
new = {**old, "status": "failed"}
```

第一行按位置解包，元素数量必须匹配。`**old` 把旧字典键值展开，新 status 会
覆盖同名旧值。调用函数时 `func(**payload)` 则把字典转换成关键字参数。

## 19. `lambda`、`partial` 与回调

lambda 是只能写一个表达式的匿名函数；`functools.partial` 预先固定某些参数，
得到一个稍后调用的新函数。后台派发和重试回调中会见到它们。若逻辑超过一行，
应定义有名字的函数，否则调试堆栈和初学者阅读都很困难。

## 20. `Path` 与原子文件替换

`Path` 是面向对象路径 API：`path.parent` 取目录，`path.exists()` 判断存在，
`path.read_text(encoding="utf-8")` 读文本。配置和状态写入采用：

```text
写同目录临时文件 → 刷新并关闭 → os.replace(临时, 正式)
```

同一磁盘上的 replace 是原子切换，读取者看到旧完整文件或新完整文件，不会看到
半截。异常时还要尽力清理临时文件。

## 21. 前导下划线与双下划线

- `_helper`：约定仅模块内部使用，Python 不会强制私有；
- `__init__`：对象创建时初始化实例；
- `__tablename__`：框架读取的特殊类属性；
- `__future__` 导入：为当前文件启用较新的语言行为。

不要因函数名前有 `_` 就认为它不重要。复杂流水线通常由许多内部步骤组成。

## 22. `if __name__ == "__main__"`

文件被直接运行时 `__name__` 是 `"__main__"`；作为模块导入时则是包路径。启动器
用这个判断只在直接执行时调用 main，测试导入函数时不会意外启动服务。

## 23. 哈希不是加密

`sha256(text).hexdigest()` 生成固定长度摘要。项目用它判断输入是否变化、定位
缓存，不用它隐藏正文。哈希不可替代权限控制；相同输入会产生相同哈希。

## 24. 读复杂函数的固定方法

遇到几十行函数，按下面顺序标记：

1. 看签名：输入来自谁，返回什么；
2. 看文档字符串：副作用、异常和状态变化；
3. 找 guard clauses：哪些条件会提前返回/抛错；
4. 找网络和数据库边界；
5. 找 commit/rollback，确认事务范围；
6. 找循环是否有次数/长度上限；
7. 找 finally，确认资源怎样关闭；
8. 找调用者和被调用者，不孤立猜测函数用途；
9. 对照测试阅读正常、边界和失败示例。

掌握以上语法后，再按 `code_reading_guide.md` 的调用链阅读。语法指南回答“这句
Python 怎么读”，代码内每个函数的文档字符串回答“这个函数在 NovelMind 中为何
存在、参数从哪里来、会改变什么”。
