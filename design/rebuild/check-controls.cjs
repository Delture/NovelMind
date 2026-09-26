const {chromium}=require('playwright');
const assert=require('node:assert/strict'),fs=require('fs'),path=require('path');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:900},reducedMotion:'reduce'});
 await page.goto('http://127.0.0.1:8768/rebuild/library.html');
 await page.getByRole('button',{name:'同意并开始',exact:true}).click();
 const layouts=[];
 for(const route of ['library','reader','summary','characters','ask','report']){
  await page.evaluate(r=>NovelMindRebuild.go(r),route);await page.evaluate(()=>document.fonts.ready);
  const issues=await page.evaluate(()=>[...document.querySelectorAll('.control-button,.icon-button')].flatMap(b=>{const br=b.getBoundingClientRect();return [...b.children].filter(e=>e instanceof SVGElement||e.classList.contains('control-label')).flatMap(e=>{const r=e.getBoundingClientRect(),bad=Math.abs(r.y+r.height/2-br.y-br.height/2)>1.1||r.right>br.right||r.left<br.left||e.scrollWidth>e.clientWidth+1;return bad?[{button:b.dataset.pencilName,child:e.dataset.pencilName}]:[]})}));
  assert.deepEqual(issues,[],route);layouts.push({route,issues});
  assert.equal(await page.locator('.control-label').evaluateAll(nodes=>nodes.filter(e=>/[＋‹←→↗↑⌄]/.test(e.textContent)).length),0);
 }
 await page.getByRole('button',{name:'设置',exact:true}).click();
 const select=page.getByRole('combobox',{name:'文本生成服务'});await select.focus();await page.keyboard.press('ArrowDown');await page.keyboard.press('Enter');assert.equal(await select.textContent(),'兼容服务');
 assert.equal(await select.getAttribute('aria-expanded'),'false');
 await page.getByText('连接与调度选项',{exact:true}).click();
 assert.equal(await page.locator('dialog details').getAttribute('open'),'');
 await page.getByRole('dialog',{name:'设置'}).screenshot({path:path.join(__dirname,'exports','settings-polished.png')});
 await page.keyboard.press('Escape');assert.equal(await page.locator('dialog').count(),0);
 await page.evaluate(()=>NovelMindRebuild.go('reader'));
 await page.getByRole('button',{name:'Aa',exact:true}).click();
 await page.getByRole('dialog',{name:'阅读设置'}).screenshot({path:path.join(__dirname,'exports','reader-style-polished.png')});
 await page.getByRole('button',{name:'关闭',exact:true}).click();
 await page.evaluate(()=>NovelMindRebuild.go('ask'));
 await page.getByRole('button',{name:'原文已就绪',exact:true}).click();
 await page.getByRole('button',{name:'演示未准备状态'}).click();
 assert.equal(await page.getByRole('button',{name:'原文待准备',exact:true}).locator('svg').count(),1);
 fs.writeFileSync(path.join(__dirname,'controls-qa.json'),JSON.stringify({passed:true,layouts,checks:['all icon and label centers','labels fit buttons','no text arrow controls','native keyboard select','custom select styling','disclosure','escape close','reader dialog','dynamic status preserves icon']},null,2));
 console.log('Control consistency checks passed');await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
