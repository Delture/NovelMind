const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('fs'),path=require('path');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:900},reducedMotion:'reduce'});
 await page.goto('http://127.0.0.1:8768/history/visual-v3/GXXh7.html');
 await page.getByRole('button',{name:'同意并开始',exact:true}).click();
 const list=page.getByRole('navigation',{name:'全部章节'}),search=page.getByRole('searchbox',{name:'搜索章节'});
 assert.equal(await list.locator('button').count(),128);
 assert.ok(await list.evaluate(e=>e.scrollHeight>e.clientHeight));
 async function chapter(n){await search.fill(String(n));await list.locator(`button[data-chapter="${n}"]`).click();assert.equal(await page.locator('[data-pencil-name="12 / 128"]').textContent(),`${n} / 128`)}
 await chapter(50);await page.getByRole('button',{name:'下一章 →',exact:true}).click();assert.equal(await page.locator('[data-pencil-name="12 / 128"]').textContent(),'51 / 128');
 await chapter(100);await page.getByRole('button',{name:'下一章 →',exact:true}).click();assert.equal(await page.locator('[data-pencil-name="12 / 128"]').textContent(),'101 / 128');
 await chapter(128);assert.equal(await page.getByRole('button',{name:'下一章 →',exact:true}).getAttribute('aria-disabled'),'true');
 await chapter(1);assert.equal(await page.getByRole('button',{name:'← 上一章',exact:true}).getAttribute('aria-disabled'),'true');
 await search.fill('不存在的章节');assert.equal(await list.locator('button:visible').count(),0);await list.getByText('没有找到匹配章节').waitFor();
 await search.fill('潮水退去');assert.equal(await list.locator('button:visible').count(),1);await list.locator('button[data-chapter="12"]').click();
 await page.getByRole('button',{name:'定位当前',exact:true}).click();assert.equal(await search.inputValue(),'');
 assert.ok(await list.locator('button[aria-current=true]').evaluate(e=>{const r=e.getBoundingClientRect(),p=e.parentElement.getBoundingClientRect();return r.top>=p.top&&r.bottom<=p.bottom}));
 await list.evaluate(e=>e.scrollTop=e.scrollHeight);assert.equal(await page.evaluate(()=>scrollY),0);
 await page.getByRole('button',{name:'定位当前',exact:true}).click();
 await page.screenshot({path:path.join(__dirname,'../../artifacts/history','exports','continuous-chapters.png')});
 fs.writeFileSync(path.join(__dirname,'../../artifacts/history','chapter-qa.json'),JSON.stringify({passed:true,chapters:128,checks:['independent scroll','50 to 51','100 to 101','first and last boundaries','empty search','title search','locate current clears filter','current chapter visible','page stays still']},null,2));
 console.log('Chapter navigation checks passed');await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
