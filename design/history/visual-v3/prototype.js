(() => {
  const routes={'资料库':'bi8Au','正文阅读':'GXXh7','章节摘要':'lX21L','人物':'qpClT','原文问答':'V9YZY','全书报告':'b4HLy','导入小说':'ULIfr','模型与密钥':'ElfMF'};
  const q=(name,root=document)=>root.querySelector(`[data-pencil-name="${name}"]`);
  let busy=false;
  const readingState={chapter:12};
  function chapterDirectory(){
    const directory=q('连续章节目录');if(!directory)return;
    const titles={7:'雾中的脚步',8:'迟来的约定',9:'灯塔来客',10:'旧日的回声',11:'未寄出的信',12:'潮水退去之后',13:'无人的站台',14:'沿海公路',15:'最后一盏灯',16:'雨落旧城',17:'另一封来信'};
    const chapters=Array.from({length:128},(_,i)=>({number:i+1,title:titles[i+1]||`雾港纪事 · ${i+1}`}));
    const paper=q('阅读正文'),heading=q('潮水退去之后',paper),label=q('第十二章',paper),counter=q('12 / 128');
    const paragraphs=[...paper.children].filter(n=>n.dataset.pencilName&&n.style.fontFamily.includes('Noto Serif')&&parseFloat(n.style.fontSize)<25);
    const original=paragraphs.map(p=>p.textContent);
    directory.replaceChildren();directory.classList.add('nm-chapter-list');directory.setAttribute('role','navigation');directory.setAttribute('aria-label','全部章节');
    const search=document.createElement('input');search.type='search';search.className='nm-chapter-search';search.placeholder='搜索章节标题或序号';search.setAttribute('aria-label','搜索章节');q('章节搜索框').replaceChildren(search);
    const tools=q('目录计数与定位');tools.textContent='';tools.classList.add('nm-chapter-tools');const total=document.createElement('span');total.textContent='共 128 章';const locate=document.createElement('button');locate.textContent='定位当前';tools.append(total,locate);
    const buttons=chapters.map(ch=>{const b=document.createElement('button');b.type='button';b.className='nm-chapter';b.dataset.chapter=ch.number;b.setAttribute('aria-label',`第 ${ch.number} 章 ${ch.title}`);const n=document.createElement('span');n.className='nm-chapter-number';n.textContent=String(ch.number).padStart(2,'0');const t=document.createElement('span');t.textContent=ch.title;b.append(n,t);b.onclick=()=>select(ch.number);b.onkeydown=e=>{if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();const visible=buttons.filter(x=>!x.hidden);const i=visible.indexOf(b)+(e.key==='ArrowDown'?1:-1);visible[Math.max(0,Math.min(visible.length-1,i))]?.focus()}};directory.append(b);return b});
    const empty=document.createElement('p');empty.className='nm-chapter-empty';empty.textContent='没有找到匹配章节';empty.hidden=true;directory.append(empty);
    function reveal(smooth=false){const b=buttons[readingState.chapter-1];directory.scrollTo({top:Math.max(0,b.offsetTop-directory.clientHeight/2+b.offsetHeight/2),behavior:smooth&&!matchMedia('(prefers-reduced-motion: reduce)').matches?'smooth':'instant'})}
    function filter(){const term=search.value.trim().toLowerCase();let count=0;for(const [i,b]of buttons.entries()){const ch=chapters[i];const match=!term||(/^\d+$/.test(term)?ch.number===Number(term):(`第${ch.number}章 ${ch.title}`).toLowerCase().includes(term));b.hidden=!match;if(match)count++}empty.hidden=count>0;total.textContent=term?`${count} 个结果 / 128 章`:'共 128 章';directory.scrollTop=0}
    function select(number,scroll=false){readingState.chapter=number;const ch=chapters[number-1];for(const [i,b]of buttons.entries()){b.setAttribute('aria-current',i===number-1?'true':'false')};heading.textContent=ch.title;label.textContent=`第 ${number} 章`;counter.textContent=`${number} / 128`;paragraphs.forEach((p,i)=>{p.hidden=number!==12&&i>1;p.textContent=number===12?original[i]:(i===0?`第 ${number} 章 · ${ch.title}`:'这是章节导航交互示例，尚未接入该章节的真实正文。正式版本会在选中章节时按需读取内容。')});q('← 上一章').setAttribute('aria-disabled',String(number===1));q('下一章 →').setAttribute('aria-disabled',String(number===128));if(scroll){search.value='';filter();reveal(true)}}
    search.oninput=filter;locate.onclick=()=>{search.value='';filter();reveal(true)};
    action(q('← 上一章'),()=>select(Math.max(1,readingState.chapter-1),true));action(q('下一章 →'),()=>select(Math.min(128,readingState.chapter+1),true));
    select(readingState.chapter);requestAnimationFrame(()=>reveal());
  }
  const privacyKey='novelmind.preview.privacy.v1';
  let sessionChoice=null;
  function privacyChoice(){try{return JSON.parse(localStorage.getItem(privacyKey))||sessionChoice}catch{return sessionChoice}}
  function choosePrivacy(enabled){sessionChoice={external:enabled,rights:enabled,decided:true};try{localStorage.setItem(privacyKey,JSON.stringify(sessionChoice))}catch{}updateConsent()}
  function privacyDialog(manage=false){
    if(document.querySelector('.nm-privacy'))return;
    const previous=document.activeElement;
    const modal=document.createElement('dialog');modal.className='nm-privacy';modal.setAttribute('aria-labelledby','privacy-title');
    modal.innerHTML=`<h2 id="privacy-title">${manage?'隐私与外部处理':'欢迎来到 NovelMind'}</h2><p class="nm-privacy-sub">${manage?'随时修改你的选择。':'开始前，了解一下你的内容会如何使用。'}</p><h3>导入与阅读保存在本机。</h3><p>使用摘要、人物分析、问答或报告时，相关正文、问题或派生资料会发送至你配置的文本生成与向量服务。目前为 DeepSeek 与硅基流动。</p><p>选择“同意并开始”，表示你同意上述外部处理，并确认拥有所处理内容的相应权利。</p><p class="nm-privacy-sub">只在首次使用时询问。以后可在设置中查看或修改。</p><div class="nm-privacy-actions"><button data-local>仅使用本地阅读</button><button data-agree>同意并开始</button></div>${manage?'<button class="nm-privacy-close">关闭</button>':''}`;
    document.body.append(modal);modal.showModal();
    function finish(value){choosePrivacy(value);modal.close();modal.remove();previous?.focus()}
    modal.querySelector('[data-local]').onclick=()=>finish(false);modal.querySelector('[data-agree]').onclick=()=>finish(true);
    modal.querySelector('.nm-privacy-close')?.addEventListener('click',()=>{modal.close();modal.remove();previous?.focus()});
    modal.addEventListener('cancel',e=>{e.preventDefault();if(manage){modal.close();modal.remove();previous?.focus()}else finish(false)});
  }
  function action(el,fn,label){if(!el)return;el.classList.add('nm-action');el.setAttribute('role','button');el.tabIndex=0;if(label)el.setAttribute('aria-label',label);el.onclick=()=>{if(el.getAttribute('aria-disabled')!=='true')fn(el)};el.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();el.click()}}}
  function toast(message){document.querySelector('.nm-toast')?.remove();const e=document.createElement('div');e.className='nm-toast';e.setAttribute('role','status');e.textContent=message;document.body.append(e);setTimeout(()=>e.remove(),3200)}
  async function go(id){if(busy)return;busy=true;document.querySelector('.nm-popover')?.remove();try{const response=await fetch(id+'.html');if(!response.ok)throw Error('page');const doc=new DOMParser().parseFromString(await response.text(),'text/html');const root=doc.body.firstElementChild;const update=()=>{document.body.replaceChildren(document.importNode(root,true));init();parent.postMessage({type:'novelmind-page',id},location.origin)};if(document.startViewTransition&&!matchMedia('(prefers-reduced-motion: reduce)').matches){await document.startViewTransition(update).finished}else update()}catch{location.href=id+'.html'}finally{busy=false}}
  function reader(){const paper=q('阅读正文'),anchor=q('字形  Aa');if(!paper||!anchor)return;action(anchor,()=>{const old=document.querySelector('.nm-popover');if(old){old.remove();return}const pop=document.createElement('div');pop.className='nm-popover';pop.setAttribute('role','dialog');pop.setAttribute('aria-label','阅读外观');const r=anchor.getBoundingClientRect();pop.style.left=Math.min(r.right-286,innerWidth-304)+'px';pop.style.top=(r.bottom+14)+'px';pop.innerHTML='<strong>阅读外观</strong><label for="reader-size">文字大小 <output>17 px</output></label><input id="reader-size" type="range" min="15" max="20" value="17"><p>调整正文大小，找到舒服的阅读节奏。</p><button type="button">完成</button>';document.body.append(pop);const slider=pop.querySelector('input');slider.oninput=()=>{pop.querySelector('output').textContent=slider.value+' px';for(const t of paper.children)if(t.dataset.pencilName&&t.style.fontFamily.includes('Noto Serif')&&parseFloat(t.style.fontSize)<25)t.style.fontSize=slider.value+'px'};pop.querySelector('button').onclick=()=>{pop.remove();anchor.focus()};slider.focus()},'调整阅读字号')}
  function init(){document.documentElement.lang='zh-CN';const nav=q('半透明分段导航');if(nav)for(const el of nav.children){const id=routes[el.dataset.pencilName];if(id)action(el,()=>go(id))}const side=q('全局侧栏');if(side)for(const [label,id]of Object.entries(routes)){const el=q(label,side);if(el)action(el,()=>go(id))}action(q('雾港来信',side||document),()=>go('GXXh7'));
    for(const [label,id]of [['＋ 导入小说','ULIfr'],['打开小说  →','GXXh7'],['在正文中打开 ↗','GXXh7'],['[1] 第 12 章 ↗','GXXh7'],['[2] 第 13 章 ↗','GXXh7']])action(q(label),()=>go(id));
    action(q('隐私与外部处理'),()=>privacyDialog(true));
    const inputBox=q('问题输入');if(inputBox){const label=inputBox.firstElementChild;const input=document.createElement('textarea');input.rows=1;input.maxLength=2000;input.className='nm-input';input.placeholder='再问一个问题，或顺着线索继续…';input.setAttribute('aria-label','问题');label.replaceWith(input);input.oninput=()=>{inputBox.lastElementChild.textContent=input.value.length+' / 2,000 字符';updateConsent()}}
    for(const label of ['提交问题 ↑','生成或更新报告']){const el=q(label);action(el,()=>toast('交互预览：确认已完成。此版本不发送模型请求。'));if(el)el.setAttribute('aria-disabled','true')}
    action(q('任务运行中'),()=>{});q('任务运行中')?.setAttribute('aria-disabled','true');
    for(const label of ['刷新任务状态','更新分析','历史记录','选择文件','解析并保存','保存设置','恢复环境配置','search','ellipsis'])action(q(label),()=>toast('这是设计预览，尚未连接业务功能。'));
    chapterDirectory();reader();updateConsent();if(!privacyChoice()?.decided)privacyDialog();
  }
  function updateConsent(){const ready=privacyChoice()?.external===true;for(const label of ['提交问题 ↑','生成或更新报告','更新分析']){const el=q(label);if(!el)continue;const input=document.querySelector('.nm-input');const enabled=ready&&(!input||input.value.trim().length>0);el.setAttribute('aria-disabled',String(!enabled));el.style.background=enabled?'#37342f':'#e8e3dc';el.style.borderColor=enabled?'#37342f':'#e8e3dc';if(el.firstElementChild){el.firstElementChild.style.color=enabled?'#ffffff':'#82786e';el.firstElementChild.textContent=ready?label:'AI 功能未启用'}}}
  document.addEventListener('keydown',e=>{if(e.key==='Escape'){document.querySelector('.nm-popover')?.remove();q('字形  Aa')?.focus()}});
  window.NovelMindPreview={go};init();
})();
