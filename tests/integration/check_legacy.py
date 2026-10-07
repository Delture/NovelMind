"""Smoke the optional Streamlit entry against a disposable real API and fresh browser."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen


def main():
    root=Path(__file__).resolve().parents[2]
    output=Path(tempfile.mkdtemp(prefix="novelmind-legacy-qa-"))
    print("ARTIFACT="+str(output),flush=True)
    def free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1",0))
            return sock.getsockname()[1]
    backend,frontend=free_port(),free_port()
    env=os.environ.copy()
    for key in tuple(env):
        if key.startswith(("DEEPSEEK_","EMBEDDING_","GENERATION_","SUMMARY_")):
            env.pop(key)
    env.update(PYTHON_DOTENV_DISABLED="1",PYTHONDONTWRITEBYTECODE="1",PYTHONIOENCODING="utf-8",
               DATABASE_URL="sqlite:///"+(output/"test.db").as_posix(),MODEL_SETTINGS_PATH=str(output/"models.json"),
               VECTOR_STORE_PATH=str(output/"vectors"),DEEPSEEK_API_KEY="",EMBEDDING_API_KEY="",
               BACKEND_URL=f"http://127.0.0.1:{backend}",QA_URL=f"http://127.0.0.1:{frontend}",QA_OUTPUT=str(output))
    commands=[("backend",[sys.executable,"-m","uvicorn","backend.app.main:app","--host","127.0.0.1","--port",str(backend)],f"http://127.0.0.1:{backend}/health"),
              ("legacy",[sys.executable,"-m","streamlit","run",str(root/"legacy/streamlit/streamlit_app.py"),"--server.address","127.0.0.1","--server.port",str(frontend),"--server.headless","true","--browser.gatherUsageStats","false"],f"http://127.0.0.1:{frontend}/_stcore/health")]
    processes=[]; logs=[]
    try:
        for name,command,url in commands:
            log=(output/(name+".log")).open("wb");logs.append(log)
            process=subprocess.Popen(command,cwd=root,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
            processes.append(process)
            for attempt in range(100):
                assert process.poll() is None,f"{name} exited; see {output}"
                try:
                    with urlopen(url,timeout=1) as response:
                        if response.status==200:
                            break
                except OSError:
                    time.sleep(.1)
            else:
                raise AssertionError(f"{name} startup timed out")
        script=output/"legacy.cjs"
        script.write_text(r'''
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const context=await browser.newContext();
 const page=await context.newPage(); const errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 try {
  await page.goto(process.env.QA_URL);
  await page.getByText('后端已连接',{exact:true}).waitFor({timeout:30000});
  await page.getByText('我的书架',{exact:true}).waitFor();
  await page.getByRole('button',{name:'模型与密钥',exact:true}).click();
  await page.locator('input[type=password]').first().waitFor();
  for(const input of await page.locator('input[type=password]').all()) assert.equal(await input.inputValue(),'');
  await page.getByRole('button',{name:'导入小说',exact:true}).click();
  await page.locator('input[type=file]').waitFor({state:'attached'});
  assert.equal(await page.locator('[data-testid=stException]').count(),0);
  await page.screenshot({path:path.join(process.env.QA_OUTPUT,'legacy.png'),fullPage:true});
  assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(process.env.QA_OUTPUT,'results.json'),JSON.stringify({checks:['real backend connected','library renders','model settings renders with empty secret inputs','upload entry available','no Streamlit exception'],errors},null,2));
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
''',encoding="utf-8")
        result=subprocess.run(["node",str(script)],cwd=root,env=env,capture_output=True,text=True,encoding="utf-8",timeout=70)
        (output/"browser.log").write_text(result.stdout+result.stderr,encoding="utf-8")
        print(json.dumps(dict(exit_code=result.returncode,output=str(output))),flush=True)
        return result.returncode
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                subprocess.run(["taskkill","/PID",str(process.pid),"/T","/F"],capture_output=True,timeout=10)
                process.wait(timeout=10)
        for log in logs:
            log.close()


if __name__=="__main__":
    raise SystemExit(main())
