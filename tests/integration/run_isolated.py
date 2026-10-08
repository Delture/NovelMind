"""Run pytest with temporary user assets and retain a compact reproducible audit."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET


def main():
    root = Path(__file__).resolve().parents[2]
    artifact = Path(tempfile.mkdtemp(prefix="novelmind-acceptance-"))
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith(("DEEPSEEK_", "EMBEDDING_", "SUMMARY_", "GENERATION_", "NOVELMIND_")) or name in ("CHROMA_PATH", "MODEL_SETTINGS_PATH", "DATABASE_URL", "VECTOR_STORE_PATH"):
            env.pop(name)
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHON_DOTENV_DISABLED="1", PYTHONIOENCODING="utf-8",
               DEEPSEEK_API_KEY="", EMBEDDING_API_KEY="",
               DATABASE_URL="sqlite:///"+(artifact/"fallback.db").as_posix(),
               VECTOR_STORE_PATH=str(artifact/"vectors"), MODEL_SETTINGS_PATH=str(artifact/"models.json"),
               NOVELMIND_RUNTIME_DIR=str(artifact/"runtime"), NOVELMIND_LOG_DIR=str(artifact/"logs"),
               PYTEST_ADDOPTS=f'-p no:cacheprovider --junitxml="{artifact / "pytest.xml"}" --basetemp="{artifact / "pytest-tmp"}"')

    def manifest():
        files = [root/"pytest.ini"]
        for folder in ("backend", "tests", "scripts", "frontend"):
            for pattern in ("*.py", "*.js", "*.cjs", "*.css", "*.html"):
                files.extend((root/folder).rglob(pattern))
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()}

    before = manifest()
    command = [sys.executable, "-m", "pytest", *(sys.argv[1:] or ["tests", "-q"])]
    print("ARTIFACT="+str(artifact), flush=True)
    print("COMMAND="+subprocess.list2cmdline(command), flush=True)
    started = time.monotonic()
    with (artifact/"pytest.log").open("wb") as log:
        result = subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
    after = manifest()
    summary = dict(command=command, exit_code=result.returncode, seconds=round(time.monotonic()-started,2),
                   source_files_changed_during_run=sorted(p for p in set(before)|set(after) if before.get(p)!=after.get(p)))
    if (artifact/"pytest.xml").exists():
        xml = ET.parse(artifact/"pytest.xml").getroot()
        summary["suites"] = [s.attrib for s in xml.iter("testsuite")]
        summary["failures"] = [dict(case=c.get("classname")+"::"+c.get("name"), message=f.get("message"))
                               for c in xml.iter("testcase") for f in list(c.findall("failure"))+list(c.findall("error"))]
        groups = {}
        for case in xml.iter("testcase"):
            parts = case.get("classname", "").split(".")
            group = parts[1] if len(parts) > 1 else "external"
            groups[group] = groups.get(group,0)+1
        summary["groups"] = groups
    for name, value in (("run",summary),("source-before",before),("source-after",after)):
        (artifact/(name+".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2),encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False),flush=True)
    if result.returncode:
        tail = (artifact/"pytest.log").read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
        print("\n".join(tail), flush=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
