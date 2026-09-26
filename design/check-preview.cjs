const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');
(async () => {
  const browser = await chromium.launch({channel:'msedge',headless:true});
  const page = await browser.newPage({viewport:{width:1440,height:960},deviceScaleFactor:1});
  const ids = ['bi8Au','GXXh7','lX21L','qpClT','V9YZY','b4HLy','ULIfr','ElfMF'];
  const results=[];
  for (const id of ids) {
    await page.goto('file:///'+path.join(__dirname,id+'.html').replaceAll('\\','/'));
    const consent=page.getByRole('button',{name:'同意并开始',exact:true});
    if(await consent.count()) await consent.click();
    await page.evaluate(()=>document.fonts.ready);
    results.push({id, ...await page.evaluate(()=>({width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight,overflow:[...document.querySelectorAll('[data-pencil-name]')].filter(e=>{const r=e.getBoundingClientRect();return r.right>1441||r.bottom>901}).map(e=>({name:e.dataset.pencilName,bottom:Math.round(e.getBoundingClientRect().bottom),right:Math.round(e.getBoundingClientRect().right)}))}))});
    await page.screenshot({path:path.join(__dirname,'exports',id+'.png'),fullPage:true});
  }
  fs.writeFileSync(path.join(__dirname,'qa.json'),JSON.stringify(results,null,2));
  console.log(JSON.stringify(results));
  await browser.close();
})();
