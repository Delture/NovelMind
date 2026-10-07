"""Run relocated design checks on a temporary copy, never replacing original artifacts."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from threading import Thread
import time
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen


def main():
    root=Path(__file__).resolve().parents[2]
    output=Path(tempfile.mkdtemp(prefix="novelmind-design-qa-"))
    print("ARTIFACT="+str(output),flush=True)
    copy=output/"design"
    shutil.copytree(root/"design",copy)
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self,*args):
            pass
    server=ThreadingHTTPServer(("127.0.0.1",0),partial(QuietHandler,directory=str(copy)))
    Thread(target=server.serve_forever,daemon=True).start()
    port=server.server_address[1]
    results=[]
    try:
        for route in ("/index.html","/current/library.html","/history/visual-v3/index.html","/history/visual-v3/preview.html"):
            with urlopen(f"http://127.0.0.1:{port}{route}",timeout=2) as response:
                assert response.status==200
        for relative in ("current/enhance.py","history/visual-v3/enhance-preview.py"):
            # Enhancers run only on disposable copies; check their relocated inputs.
            result=subprocess.run([str(root/".venv/Scripts/python.exe"),str(copy/relative)],capture_output=True,timeout=15)
            results.append(dict(script=relative,exit_code=result.returncode))
        scripts=sorted((copy/"current").glob("check*.cjs"))+sorted((copy/"history/visual-v3").glob("check*.cjs"))
        for script in scripts:
            original=script.read_text(encoding="utf-8")
            script.write_text(original.replace("http://127.0.0.1:8768",f"http://127.0.0.1:{port}"),encoding="utf-8")
            started=time.monotonic()
            with (output/(script.stem+"-"+script.parent.name+".log")).open("wb") as log:
                process=subprocess.Popen(["node",str(script)],cwd=copy,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    code=process.wait(timeout=90)
                except subprocess.TimeoutExpired:
                    subprocess.run(["taskkill","/PID",str(process.pid),"/T","/F"],capture_output=True,timeout=10)
                    code=-1
            item=dict(script=str(script.relative_to(copy)),exit_code=code,seconds=round(time.monotonic()-started,2))
            results.append(item)
            print(json.dumps(item),flush=True)
    finally:
        server.shutdown();server.server_close()
    broken=[]
    for path in [root/"README.md",root/"Codex.md",*(root/"docs").rglob("*.md"),*(root/"design").rglob("*.md"),root/"legacy/streamlit/README.md"]:
        if "archive" in path.parts or "history" in path.parts:
            continue
        text=path.read_text(encoding="utf-8")
        for label,target in re.findall(r'\[([^\]]+)\]\(([^)]+)\)',text):
            target=target.strip('<> ').split(' "',1)[0]
            url=urlsplit(target)
            if url.scheme or not url.path:
                continue
            resolved=(path.parent/unquote(url.path)).resolve()
            if not resolved.exists():
                broken.append(dict(source=str(path.relative_to(root)),target=target,label=label))
    report=dict(results=results,broken_current_markdown_links=broken)
    (output/"results.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False),flush=True)
    return int(bool(broken) or any(item["exit_code"] for item in results))


if __name__=="__main__":
    raise SystemExit(main())
