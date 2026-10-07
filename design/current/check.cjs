const {chromium}=require('playwright');
const assert=require('node:assert/strict'),fs=require('fs'),path=require('path');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:900},reducedMotion:'reduce'});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 fs.mkdirSync(path.join(__dirname,'../artifacts/current','exports'),{recursive:true});
 await page.goto('http://127.0.0.1:8768/current/library.html');
 await page.getByRole('button',{name:'同意并开始',exact:true}).click();
 async function go(route){await page.evaluate(r=>window.NovelMindRebuild.go(r),route);await page.evaluate(()=>document.fonts.ready)}
 const layout=[];
 for(const route of ['library','reader','summary','characters','ask','report']){
  await go(route);
  const bad=await page.evaluate(()=>[...document.querySelectorAll('[data-pencil-name]')].filter(e=>{if(e.hidden)return false;let p=e.parentElement;while(p){if(p.hidden)return false;p=p.parentElement}const r=e.getBoundingClientRect();return r.right>1441||r.bottom>901||r.left<0||r.top<0}).map(e=>e.dataset.pencilName));
  layout.push({route,overflow:bad});
  await page.screenshot({path:path.join(__dirname,'../artifacts/current','exports',route+'.png')});
 }
 await go('reader');assert.equal(await page.locator('[data-pencil-name="书内导航"]').count(),0);
 const chapters=page.getByRole('navigation',{name:'全部章节'});assert.equal(await chapters.locator('button').count(),128);
 await page.getByRole('searchbox',{name:'搜索章节标题或序号'}).fill('50');await chapters.locator('[data-chapter="50"]').click();await page.getByRole('button',{name:'下一章',exact:true}).click();assert.equal(await page.locator('[data-pencil-name="chapter-counter"]').textContent(),'51 / 128');
 await go('summary');assert.equal(await page.getByRole('navigation',{name:'全部章节摘要'}).locator('button').count(),128);assert.equal(await page.getByText('第 1–50 章',{exact:true}).count(),0);
 await page.getByRole('searchbox',{name:'搜索章节标题或序号'}).fill('100');await page.getByRole('navigation',{name:'全部章节摘要'}).locator('[data-chapter="100"]').click();assert.match(await page.locator('[data-pencil-name="summary-copy"]').textContent(),/没有摘要/);
 await go('ask');await page.getByRole('button',{name:'第 12 章',exact:true}).click();await page.getByRole('dialog',{name:'原文依据'}).waitFor();await page.screenshot({path:path.join(__dirname,'../artifacts/current','exports','evidence.png')});await page.getByRole('button',{name:'阅读这一章'}).click();await page.getByRole('button',{name:'返回刚才的对话',exact:true}).waitFor();await page.getByRole('button',{name:'返回刚才的对话',exact:true}).click();await page.getByRole('textbox',{name:'问题',exact:true}).waitFor();
 await go('report');await page.getByRole('button',{name:'补齐摘要并更新报告',exact:true}).click();await page.getByRole('button',{name:'开始示例任务'}).click();await page.getByRole('dialog',{name:'任务'}).waitFor();await page.screenshot({path:path.join(__dirname,'../artifacts/current','exports','tasks.png')});await page.getByRole('button',{name:'关闭',exact:true}).click();await go('reader');await page.waitForFunction(()=>document.querySelector('[data-pencil-name="tasks标签"]').textContent==='任务',{timeout:10000});await go('report');assert.match(await page.locator('[data-pencil-name="页面说明"]').textContent(),/已更新报告/);
 await go('library');await page.getByRole('button',{name:'导入小说',exact:true}).click();await page.getByRole('dialog',{name:'导入小说'}).waitFor();await page.getByLabel('选择 TXT 文件').setInputFiles({name:'sample.txt',mimeType:'text/plain',buffer:Buffer.from('第一章 示例\n这是设计测试。')});assert.equal(await page.getByRole('button',{name:'导入',exact:true}).isEnabled(),true);await page.screenshot({path:path.join(__dirname,'../artifacts/current','exports','import.png')});await page.getByRole('button',{name:'关闭',exact:true}).click();
 await page.getByRole('button',{name:'设置',exact:true}).click();await page.getByRole('button',{name:'修改选择',exact:true}).click();await page.getByRole('button',{name:'仅使用本地阅读',exact:true}).click();await go('report');await page.getByRole('button',{name:'更新全书报告',exact:true}).click();await page.getByRole('dialog',{name:'AI 功能尚未开启'}).waitFor();
 fs.writeFileSync(path.join(__dirname,'../artifacts/current','qa.json'),JSON.stringify({layout,errors,checks:['continuous reader directory','50 to 51','continuous summaries','missing summary state','citation drawer to source and back','report prerequisite orchestration','background task survives navigation','modal TXT import','privacy controls AI action']},null,2));
 assert.deepEqual(errors,[]);assert.ok(layout.every(r=>r.overflow.length===0),JSON.stringify(layout));console.log('Rebuild checks passed');await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
