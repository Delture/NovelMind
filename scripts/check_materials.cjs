const {runSuite} = require('../tests/support/browser_qa.cjs');

runSuite('materials',async ({page,context,api,url,upload,select,shot,check,eventually,assert,details}) => {
  await context.addInitScript(() => {
    const add=EventTarget.prototype.addEventListener, remove=EventTarget.prototype.removeEventListener;
    const registrations=[];
    EventTarget.prototype.addEventListener=function(type,callback,options){
      if(this===window||this===document) registrations.push({target:this,type,callback,signal:options?.signal,removed:false});
      return add.call(this,type,callback,options);
    };
    EventTarget.prototype.removeEventListener=function(type,callback,options){
      for(const item of registrations) if(item.target===this&&item.type===type&&item.callback===callback)item.removed=true;
      return remove.call(this,type,callback,options);
    };
    const frames={requested:0,fired:0};const raf=requestAnimationFrame;
    window.requestAnimationFrame=callback=>{frames.requested++;return raf(time=>{frames.fired++;callback(time);});};
    window.__qaMaterial={frames,events:[],active:()=>registrations.filter(item=>!item.removed&&!item.signal?.aborted).length};
    add.call(document,'animationstart',event=>window.__qaMaterial.events.push({kind:'start',name:event.animationName,tag:event.target.tagName}));
    add.call(document,'animationend',event=>window.__qaMaterial.events.push({kind:'end',name:event.animationName,tag:event.target.tagName}));
  });
  const book=await upload('雨夜河岸的来信与一次温柔的重逢.txt',Array.from({length:12},(_,i)=>
    `第${i+1}章 ${i%3===0?'沿着旧城河岸寻找一封被风吹散的来信':'雨夜来信'+(i+1)}\n`+
    Array.from({length:40},()=> '雨停以后，河岸的灯一盏盏亮了起来。她将信放在桌边，读到熟悉的名字时，想起多年前那个安静的清晨。').join('\n\n')).join('\n\n'));
  details.findings=[];details.contrast=[];details.material=[];
  const stable=async()=>{
    await eventually(()=>page.evaluate(()=>document.getAnimations().every(a=>a.playState!=='running'&&!a.pending)),'animations finish');
    await page.locator('.toast').waitFor({state:'detached',timeout:6000});
    await page.evaluate(()=>document.fonts.ready);
    await page.waitForTimeout(80);
  };
  const stableShot=async label=>{await stable();await shot(label);};
  const material=selector=>page.locator(selector).first().evaluate(node=>{
    const s=getComputedStyle(node),b=node.getBoundingClientRect();
    return {selector:node.className,background:s.backgroundColor,blur:s.backdropFilter,border:s.borderTopColor,
      shadow:s.boxShadow,opacity:s.opacity,width:b.width,right:b.right};
  });
  const contrast=async(label,selector)=>{
    const values=await page.locator(selector).evaluateAll(nodes=>{
      const parse=color=>{
        const numbers=color.match(/[\d.]+/g)?.map(Number)||[];
        if(color.startsWith('color('))return [numbers[0]*255,numbers[1]*255,numbers[2]*255,numbers[3]??1];
        return [...numbers.slice(0,3),numbers[3]??1];
      };
      const blend=(front,back)=>[0,1,2].map(i=>front[i]*(front[3]??1)+back[i]*(1-(front[3]??1)));
      const luminance=rgb=>rgb.map(n=>n/255).map(n=>n<=.04045?n/12.92:((n+.055)/1.055)**2.4)
        .reduce((sum,n,i)=>sum+n*[.2126,.7152,.0722][i],0);
      return nodes.slice(0,12).map(node=>{
        let opacity=1;const ancestors=[];
        for(let current=node;current;current=current.parentElement){ancestors.unshift(current);opacity*=Number(getComputedStyle(current).opacity);}
        let background=[255,255,255];
        for(const item of ancestors)background=blend(parse(getComputedStyle(item).backgroundColor),background);
        const s=getComputedStyle(node),color=parse(s.color);color[3]*=opacity;
        const foreground=blend(color,background),a=luminance(foreground),b=luminance(background);
        return {text:(node.textContent.trim()||node.getAttribute('aria-label')||node.name||node.tagName).slice(0,40),color:s.color,background,opacity,
          fontSize:s.fontSize,ratio:(Math.max(a,b)+.05)/(Math.min(a,b)+.05)};
      });
    });
    details.contrast.push({label,selector,values});
    for(const value of values) {
      if(value.opacity<.999)details.findings.push(`${label}: final text opacity ${value.opacity}`);
      if(value.ratio<4.5)details.findings.push(`${label}: final contrast ${value.ratio.toFixed(2)} (${value.text})`);
    }
  };
  await page.goto(url);
  await page.getByRole('heading',{name:'你的资料库',exact:true}).waitFor();
  await stableShot('library-warm-stable');
  await contrast('light library actions','.book-card footer button');
  await contrast('light library auxiliary','.page-header p, .book-card p, .continue p');
  // Only true overlays are glass; the edge-attached sidebar is flat and opaque.
  details.material.push(await material('.sidebar'));
  assert.equal(details.material[0].blur,'none');
  assert.equal(details.material[0].shadow,'none');
  assert.equal((await material('.book-card')).blur,'none');
  const openSettings=async()=>{
    await page.getByRole('button',{name:'设置',exact:true}).click();
    const d=page.getByRole('dialog',{name:'模型与设置'});await d.waitFor();return d;
  };
  let d=await openSettings();await stable();
  details.material.push(await material('dialog'));
  assert.match(details.material.at(-1).blur,/blur\(/);
  assert.notEqual(details.material.at(-1).shadow,'none');
  const box=await d.boundingBox();
  await page.mouse.move(box.x+50,box.y+50);await page.waitForTimeout(60);
  const first=await d.evaluate(node=>getComputedStyle(node).getPropertyValue('--light-x'));
  await page.mouse.move(box.x+110,box.y+75);await page.waitForTimeout(60);
  const second=await d.evaluate(node=>getComputedStyle(node).getPropertyValue('--light-x'));
  assert(first&&second&&first!==second);
  const idle=await page.evaluate(()=>__qaMaterial.frames.fired);await page.waitForTimeout(350);
  assert.equal(await page.evaluate(()=>__qaMaterial.frames.fired),idle);
  check('侧栏实色无blur；浮层有玻璃blur/阴影，动态光随指针变化但无持续RAF循环');
  assert(await page.evaluate(()=>__qaMaterial.events.some(e=>e.tag==='DIALOG'&&e.kind==='end')));
  await stableShot('settings-warm-stable');
  await d.getByRole('button',{name:'关闭',exact:true}).click();await d.waitFor({state:'detached'});
  if(!await page.getByRole('button',{name:'设置',exact:true}).evaluate(node=>node===document.activeElement))details.findings.push('Settings exit does not restore focus to its trigger');
  const active=await page.evaluate(()=>__qaMaterial.active());
  for(let i=0;i<4;i++){
    d=await openSettings();await d.getByRole('button',{name:'关闭',exact:true}).click();await d.waitFor({state:'detached'});
  }
  assert.equal(await page.evaluate(()=>__qaMaterial.active()),active);
  assert.equal(await page.locator('dialog').count(),0);
  check('浮层真实进入/退出动画结束，重复开关无遗留遮罩或活动全局监听增加');

  await page.goto(`${url}/#page=reader&novel=${book.novel_id}`);
  await page.locator('[data-chapter-title]').waitFor();await page.mouse.move(0,0);
  await stableShot('reader-paper-stable');
  await contrast('paper chapter numbers','.r-chapter > span:first-child');
  await contrast('paper chapter titles','.r-chapter > span:nth-child(2)');
  await contrast('paper helper text','.r-side-footer');
  details.material.push(await material('.r-sidebar'),await material('.r-paper'));
  const reading=await api(`/novels/${book.novel_id}/reading`);
  await page.getByRole('button',{name:'Aa',exact:true}).click();d=page.getByRole('dialog',{name:'阅读设置'});
  const readerTheme=d.getByRole('combobox',{name:'界面主题',exact:true});
  await readerTheme.click();await readerTheme.press('Home');await readerTheme.press('ArrowDown');
  await readerTheme.press('End');await readerTheme.press('Enter');
  assert.equal(await page.locator('html').getAttribute('data-ui-theme'),'dark');
  assert.equal(await page.locator('body').getAttribute('data-reader-theme'),'paper');
  await d.getByRole('button',{name:'关闭',exact:true}).click();await d.waitFor({state:'detached'});
  await stableShot('reader-paper-ui-dark-stable');
  await contrast('dark UI paper reading text','.r-chapter > span, .r-side-footer');
  await contrast('dark UI paper controls','.r-header button, .r-dock button:not(:disabled)');
  await page.getByRole('button',{name:'Aa',exact:true}).click();d=page.getByRole('dialog',{name:'阅读设置'});
  await d.getByRole('button',{name:'暖纸',exact:true}).click();
  await d.getByRole('button',{name:'关闭',exact:true}).click();await d.waitFor({state:'detached'});
  assert.equal(await page.locator('html').getAttribute('data-ui-theme'),'dark');
  assert.equal(await page.locator('body').getAttribute('data-reader-theme'),'sepia');
  await stableShot('reader-sepia-ui-dark-stable');
  await contrast('dark UI sepia reading text','.r-chapter > span, .r-side-footer');
  await contrast('dark UI sepia controls','.r-header button, .r-dock button:not(:disabled)');
  check('深色界面配纸白/暖纸时阅读面、目录和控件维持各自可读色配');
  await page.getByRole('button',{name:'Aa',exact:true}).click();d=page.getByRole('dialog',{name:'阅读设置'});
  await d.getByRole('button',{name:'夜间',exact:true}).click();
  await d.getByRole('button',{name:'关闭',exact:true}).click();await d.waitFor({state:'detached'});
  await stableShot('reader-dark-stable');
  await contrast('dark chapter numbers','.r-chapter > span:first-child');
  await contrast('dark chapter titles','.r-chapter > span:nth-child(2)');
  await contrast('dark helper text','.r-side-footer');
  assert.equal(await page.locator('.r-prose').evaluate(node=>getComputedStyle(node).backdropFilter),'none');
  check('阅读纸面主题优先，浅深稳定帧正文无玻璃滤镜，记录目录与辅助字实际对比');

  await page.getByRole('button',{name:'返回资料库',exact:true}).click();
  await page.getByRole('heading',{name:'你的资料库',exact:true}).waitFor();
  assert.equal(await page.locator('body').getAttribute('data-reader-theme'),null);
  assert.equal(await page.locator('html').getAttribute('data-ui-theme'),'dark');
  await stableShot('library-dark-stable');
  await contrast('dark library actions','.book-card footer button');
  await contrast('dark library auxiliary','.page-header p, .book-card p, .continue p');
  d=await openSettings();await stable();
  await contrast('dark settings inputs','[data-setting-panel="generation"] input:not([type=hidden]):not([type=checkbox])');
  await contrast('dark settings ordinary actions','[data-setting-panel="generation"] [data-probe], [data-setting-panel="generation"] [data-models]');
  await d.getByRole('combobox',{name:'文本生成服务预设',exact:true}).click();await stable();
  await contrast('dark settings dropdown','.select-menu:not([hidden]) [role=option]');
  await stableShot('settings-dark-dropdown-stable');
  await page.keyboard.press('Escape');
  assert(await d.isVisible());
  await d.getByRole('tab',{name:'外观',exact:true}).click();
  await stableShot('appearance-dark-stable');
  await d.getByRole('checkbox',{name:'降低动态效果',exact:true}).check();
  await d.getByRole('checkbox',{name:'降低透明度',exact:true}).check();
  await stable();
  assert.equal(await page.locator('html').getAttribute('data-reduced-motion'),'true');
  assert.equal(await page.locator('html').getAttribute('data-reduced-transparency'),'true');
  assert.equal((await material('dialog')).blur,'none');
  const afterReduce=await page.evaluate(()=>__qaMaterial.frames.fired);
  await page.mouse.move(600,300);await page.mouse.move(650,330);await page.waitForTimeout(100);
  assert.equal(await page.evaluate(()=>__qaMaterial.frames.fired),afterReduce);
  assert.equal(await page.locator('.glass-lit').count(),0);
  assert(await page.evaluate(()=>document.getAnimations().every(a=>a.playState!=='running'&&!a.pending)));
  check('降低效果真正停止动画/指针RAF并取消玻璃blur');

  await d.getByRole('checkbox',{name:'降低动态效果',exact:true}).uncheck();
  await d.getByRole('checkbox',{name:'降低透明度',exact:true}).uncheck();
  await select(d,'界面主题','跟随系统');
  const cdp=await context.newCDPSession(page);
  await cdp.send('Emulation.setEmulatedMedia',{features:[{name:'prefers-color-scheme',value:'dark'},
    {name:'prefers-reduced-motion',value:'reduce'},{name:'prefers-reduced-transparency',value:'reduce'},
    {name:'prefers-contrast',value:'more'}]});
  await eventually(()=>page.evaluate(()=>matchMedia('(prefers-contrast: more)').matches&&
    document.documentElement.dataset.reducedMotion==='true'&&document.documentElement.dataset.reducedTransparency==='true'),'native preference priority');
  assert.equal(await d.getByRole('checkbox',{name:'降低动态效果',exact:true}).isChecked(),false);
  assert.equal(await d.getByRole('checkbox',{name:'降低透明度',exact:true}).isChecked(),false);
  details.nativeMedia=await page.evaluate(()=>({motion:matchMedia('(prefers-reduced-motion: reduce)').matches,
    transparency:matchMedia('(prefers-reduced-transparency: reduce)').matches,contrast:matchMedia('(prefers-contrast: more)').matches}));
  assert(Object.values(details.nativeMedia).every(Boolean));
  check('原生系统减少动态/透明度/高对比优先于未勾选本地设置');
  await page.setViewportSize({width:390,height:844});await stableShot('mobile-settings-reduced');
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  const dialogBox=await d.boundingBox();assert(dialogBox.x>=-1&&dialogBox.x+dialogBox.width<=391);
  await d.getByRole('button',{name:'关闭',exact:true}).click();await d.waitFor({state:'detached'});
  await page.goto(`${url}/#page=reader&novel=${book.novel_id}`);await page.locator('[data-chapter-title]').waitFor();
  await stableShot('mobile-reader-reduced');
  assert.equal((await material('.r-sidebar')).blur,'none');
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  // Narrow screens start with the directory closed so it does not cover the text.
  assert(await page.locator('.r-sidebar').isHidden());
  await page.getByRole('button',{name:'切换目录',exact:true}).click();
  await page.locator('.r-sidebar').waitFor({state:'visible'});
  await page.getByRole('button',{name:'切换目录',exact:true}).click();
  await page.locator('.r-sidebar').waitFor({state:'hidden'});
  await stableShot('mobile-reader-reduced-body');
  await page.getByRole('button',{name:'Aa',exact:true}).click();
  d=page.getByRole('dialog',{name:'阅读设置'});await d.getByRole('button',{name:'关闭',exact:true}).click();
  await d.waitFor({state:'detached'});
  assert(await page.getByRole('button',{name:'Aa',exact:true}).evaluate(node=>node===document.activeElement));
  check('窄屏减少效果下浮层不越界、阅读可用且同步入口焦点返回');
  assert.deepEqual((await api(`/novels/${book.novel_id}/reading`)).bookmarks,reading.bookmarks);
  assert.deepEqual(await api('/workspace/tasks'),[]);
  assert.deepEqual(details.findings,[],'Stable-state visual/focus findings; see metrics and screenshots');
}).catch(error=>{console.error(error);process.exitCode=1;});
