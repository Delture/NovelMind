"""Prove workflows run with the HTTP routing layer unavailable in a fresh process."""

from pathlib import Path
import subprocess
import sys

import pytest


PROGRAM = r'''
import asyncio
import importlib.abc
import json
import sys
from unittest.mock import patch

class NoRoutingLayer(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "backend.app.main" or fullname == "backend.app.api" or fullname.startswith("backend.app.api."):
            raise AssertionError("Business service imported HTTP assembly: " + fullname)
sys.meta_path.insert(0, NoRoutingLayer())

from sqlalchemy.orm import sessionmaker
from backend.app.core.config import get_settings
from backend.app.core.database import create_db_engine, init_db
from backend.app.models.novel import Novel, Chapter
from backend.app.models.workspace import Workflow
from backend.app.services import tasks, rag, workflows, dependencies
from tests.integration.workflow_fakes import ProviderAudit

settings = get_settings()
engine = create_db_engine(settings.database_url)
init_db(engine)
factory = sessionmaker(bind=engine, expire_on_commit=False)
kind = sys.argv[1]
with factory() as db:
    novel = Novel(filename="independent.txt", encoding="utf-8", chapter_count=1,
                  chapters=[Chapter(chapter_index=1,title="来信",content="主角收到来信。",character_count=7)])
    db.add(novel)
    db.flush()
    db.add(Workflow(id="independent-workflow", novel_id=novel.id, kind=kind,
                    question="主角收到什么？", created_at=__import__("datetime").datetime(2026, 9, 27)))
    db.commit()
audit = ProviderAudit(factory)
audit.release.set()
with patch.object(dependencies, "AsyncDeepSeekProvider", lambda s: audit.generation("summary", s)), \
     patch.object(dependencies, "get_llm_provider", lambda s: audit.generation("report", s)), \
     patch.object(dependencies, "DeepSeekProvider", lambda s: audit.generation("answer", s)), \
     patch.object(dependencies, "OpenAIEmbeddingProvider", audit.embedding):
    execution = workflows.get_workflow_execution(settings,
        tasks.get_llm_provider_factory(settings), tasks.get_sync_llm_provider_factory(settings),
        rag.get_embedding_factory(settings), rag.get_generation_factory(settings), rag.get_vector_store_factory(settings))
    asyncio.run(workflows.run_workflow("independent-workflow", factory, execution, True, True))
with factory() as db:
    result = db.get(Workflow, "independent-workflow")
    assert result.status == "succeeded", result.error_message
assert not any(name == "backend.app.main" or name.startswith("backend.app.api") for name in sys.modules)
roles = sorted({role for role, _ in audit.calls})
assert roles == (["report", "summary"] if kind == "report" else ["answer", "embedding"])
print(json.dumps({"kind":kind,"status":"succeeded","roles":roles,"routing_imported":False}))
engine.dispose()
'''


@pytest.mark.parametrize("kind", ["report", "ask"])
def test_services_execute_without_main_app_or_api_imports(isolated_config, kind):
    """Imports are denied, not merely absent by accident; both real chains finish."""
    result = subprocess.run([sys.executable, "-c", PROGRAM, kind],
                            cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"routing_imported": false' in result.stdout
