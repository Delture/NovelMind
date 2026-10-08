(() => {
  const clamp = (v, min, max, fallback) => Number.isFinite(Number(v)) ? Math.max(min, Math.min(max, Number(v))) : fallback;
  const glyphs = {
    back: 'M19 12H5m7-7-7 7 7 7',
    next: 'M5 12h14m-7-7 7 7-7 7',
    menu: 'M9 3v18M3 3h18v18H3z',
    bookmark: 'M6 3h12v18l-6-4-6 4z',
    focus: 'M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5',
    search: 'M21 21l-5-5M18 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0',
    close: 'M6 6l12 12M18 6 6 18',
    help: 'M9 9a3 3 0 0 1 6 0c0 2-3 2-3 5m0 3h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
    check: 'M5 12l4 4L19 6'
  };
  const svg = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="${glyphs[name] || glyphs.help}"/></svg>`;
  function mount(ctx) {
    const {
      root,
      state,
      title,
      go,
      modal,
      toast,
      total,
      bookName
    } = ctx;
    const saved = ctx.saved;
    let prefs = saved.prefs,
      bookmarks = saved.bookmarks;
    let position = state.returnTo ? {
      chapter: state.chapter,
      paragraph: -1,
      offset: 0
    } : saved.position;
    let requestId = 0,
      loaded = false;
    let chapter = position.chapter,
      focus = false,
      compactDrawer = false,
      restoring = false,
      disposed = false,
      saveTimer,
      idleTimer;
    const abort = new AbortController(),
      on = (el, event, fn, opts = {}) => el.addEventListener(event, fn, {
        ...opts,
        signal: abort.signal
      });
    let requestedChapter = chapter, restoreRevision = 0;
    const $ = s => root.querySelector(s);
    const nativeButton = (action, label, icon, extra = '') => `<button type="button" class="r-button ${extra}" data-r-action="${action}" aria-label="${label}" title="${label}">${icon ? svg(icon) : ''}<span>${label}</span></button>`;
    root.dataset.readerRoot = 'true';
    root.classList.add('reader-app');
    root.style.transform = 'none';
    root.innerHTML = `<header class="r-header"><div class="r-header-left"><button class="r-button" data-r-action="library" aria-label="返回资料库">${svg('back')}<span class="r-back-label">资料库</span></button><button class="r-button r-icon" data-r-action="sidebar" aria-label="切换目录" title="显示或隐藏目录">${svg('menu')}</button></div><div class="r-book"><strong></strong><span>本机资料库</span></div><div class="r-header-right">${nativeButton('text-search', '搜索全文', 'search')}${nativeButton('analysis', '理解故事', null, 'r-analysis')}${nativeButton('tasks', '任务')}${nativeButton('focus', '专注', 'focus')}${nativeButton('help', '快捷键', 'help', 'r-icon')}</div></header><div class="r-workspace"><aside class="r-sidebar" aria-label="阅读侧栏"><div class="r-side-tabs" role="tablist" aria-label="目录与书签"><button role="tab" aria-selected="true" aria-controls="r-chapter-panel" data-tab="chapters">目录</button><button role="tab" aria-selected="false" aria-controls="r-bookmark-panel" data-tab="bookmarks">书签 <span data-bookmark-count></span></button></div><div class="r-search-wrap">${svg('search')}<input class="r-search" type="search" aria-label="搜索章节标题或序号" placeholder="搜索章节标题或序号"></div><div class="r-side-meta"><span data-chapter-count>共 ${total} 章</span><button class="r-button" data-r-action="locate">定位当前</button></div><nav id="r-chapter-panel" class="r-directory" aria-label="全部章节"></nav><input class="r-bookmark-search" type="search" placeholder="搜索书签名称或备注" aria-label="搜索书签" hidden><div id="r-bookmark-panel" class="r-bookmarks" role="tabpanel" aria-label="书签列表" hidden></div><div class="r-side-footer">位置与书签自动保存在本机资料库</div></aside><main class="r-paper"><div class="r-scroll" tabindex="0" role="region" aria-label="正文"><article class="r-prose"><header class="r-prose-header"><small data-chapter-label></small><h1 data-chapter-title></h1><div class="r-chapter-meta">原文 · 滚动阅读</div></header><div data-paragraphs></div><div class="r-end"><span data-end-label>本章已读完</span><button class="r-button" data-r-action="end-next">继续下一章 ${svg('next')}</button></div></article></div><nav class="r-dock" aria-label="阅读控制栏"><button class="r-button r-chapter-button" data-r-action="prev" aria-label="上一章">${svg('back')}<span>上一章</span></button><button class="r-button r-progress" data-r-action="progress" aria-label="阅读进度"><span data-pencil-name="chapter-counter"></span><small data-chapter-progress>本章 0%</small></button><span class="r-dock-separator"></span><button class="r-button" data-r-action="appearance" aria-label="Aa" title="阅读设置">Aa</button><button class="r-button r-icon" data-r-action="bookmark" aria-label="添加书签" title="添加书签">${svg('bookmark')}</button><button class="r-button r-chapter-button" data-r-action="next" aria-label="下一章"><span>下一章</span>${svg('next')}</button></nav></main></div><span class="r-save-status" role="status" aria-live="polite">阅读位置自动保存</span>`;
    $('.r-book strong').textContent = bookName;
    $('.r-side-footer').textContent = '位置与书签自动保存到本机资料库';
    const taskButton = $('[data-r-action="tasks"]');
    taskButton.dataset.pencilName = 'tasks';
    taskButton.querySelector('span').dataset.pencilName = 'tasks标签';
    const scroll = $('.r-scroll'),
      paras = $('[data-paragraphs]'),
      directory = $('.r-directory'),
      search = $('.r-search');
    const reduced = () => document.documentElement.dataset.reducedMotion === 'true' || matchMedia('(prefers-reduced-motion:reduce)').matches;
    function anchor() {
      if (scroll.scrollTop < 90) return {
        chapter,
        paragraph: -1,
        offset: 0
      };
      const top = scroll.getBoundingClientRect().top + 24;
      const ps = [...paras.children];
      let index = ps.findIndex(p => p.getBoundingClientRect().bottom > top);
      if (index < 0) index = ps.length - 1;
      const p = ps[index];
      if (!p) return {
        chapter,
        paragraph: -1,
        offset: 0
      };
      const rect = p.getBoundingClientRect();
      let offset = 0;
      try {
        const x = rect.left + 2,
          y = Math.max(top, rect.top + 3);
        const caret = document.caretPositionFromPoint?.(x, y);
        if (caret && p.contains(caret.offsetNode)) offset = caret.offset;else {
          const range = document.caretRangeFromPoint?.(x, y);
          if (range && p.contains(range.startContainer)) offset = range.startOffset;
        }
      } catch {}
      return {
        chapter,
        paragraph: index,
        offset
      };
    }
    function storePosition(options) {
      if (disposed || restoring || !loaded) return;
      position = anchor();
      persist(options);
    }
    function persist(options) {
      if (!loaded || disposed) return;
      ctx.save({
        prefs: {
          ...prefs
        },
        position: {
          ...position
        }
      }, options).then(() => {
        if (!disposed) $('.r-save-status').textContent = '阅读位置已保存到本机资料库';
      }).catch(() => {
        if (!disposed) $('.r-save-status').textContent = '保存失败，请检查连接；继续阅读时将重试';
      });
    }
    function restore(pos) {
      const revision = ++restoreRevision;
      restoring = true;
      const p = paras.children[pos.paragraph];
      if (pos.paragraph < 0 || !p) scroll.scrollTop = 0;else {
        const range = document.createRange();
        range.setStart(p.firstChild, Math.min(pos.offset || 0, p.textContent.length));
        range.collapse(true);
        const r = range.getBoundingClientRect();
        scroll.scrollTop += r.top - scroll.getBoundingClientRect().top - 24;
      }
      requestAnimationFrame(() => {
        if (disposed || revision !== restoreRevision) return;
        restoring = false;
        progress();
      });
    }
    function progress() {
      const range = scroll.scrollHeight - scroll.clientHeight;
      const percent = range > 1 ? Math.round(scroll.scrollTop / range * 100) : 100;
      $('[data-chapter-progress]').textContent = `本章 ${percent}%`;
      $('[data-pencil-name="chapter-counter"]').textContent = `${chapter} / ${total}`;
      const a = anchor();
      const exists = bookmarks.some(b => b.chapter === chapter && b.paragraph === a.paragraph);
      const mark = $('[data-r-action="bookmark"]');
      mark.setAttribute('aria-pressed', String(exists));
      mark.setAttribute('aria-label', exists ? '移除书签' : '添加书签');
      mark.title = exists ? '移除当前位置的书签' : '添加当前位置的书签';
    }
    const compactReader = () => innerWidth <= 760;
    function sidebarOpen() {
      return !focus && (compactReader() ? compactDrawer : prefs.sidebar);
    }
    function applyPrefs() {
      root.dataset.theme = prefs.theme;
      document.body.dataset.readerTheme = prefs.theme;
      root.style.setProperty('--reading-size', prefs.size + 'px');
      root.style.setProperty('--reading-line', prefs.line);
      root.style.setProperty('--reading-width', prefs.width + 'px');
      root.style.setProperty('--reading-gap', prefs.gap + 'px');
      const readingFonts = {serif: 'var(--font-serif)', sans: 'var(--font-sans)', pingfang: 'var(--font-pingfang)'};
      if (!readingFonts[prefs.font]) prefs.font = 'serif';
      root.style.setProperty('--font-reading', readingFonts[prefs.font]);
      const open = sidebarOpen();
      $('.r-sidebar').hidden = !open;
      $('[data-r-action="sidebar"]').setAttribute('aria-expanded', String(open));
      root.classList.toggle('is-focus', focus);
      $('[data-r-action="focus"]').setAttribute('aria-pressed', String(focus));
      $('[data-r-action="focus"] span').textContent = focus ? '退出专注' : '专注';
      $('.r-header [data-r-action="help"] span').hidden = true;
    }
    function reveal() {
      const b = directory.querySelector(`[data-chapter="${chapter}"]`);
      directory.scrollTop = Math.max(0, b.offsetTop - directory.offsetTop - directory.clientHeight / 2 + b.offsetHeight / 2);
    }
    function filter() {
      const term = search.value.trim();
      let count = 0;
      directory.querySelectorAll('[data-chapter]').forEach(b => {
        const n = Number(b.dataset.chapter);
        const match = !term || (/^\d+$/.test(term) ? n === Number(term) : `第${n}章 ${title(n)}`.includes(term));
        b.hidden = !match;
        if (match) count++;
      });
      $('[data-directory-empty]').hidden = count > 0;
      $('[data-chapter-count]').textContent = term ? `${count} 个结果 / ${total} 章` : `共 ${total} 章`;
      directory.scrollTop = 0;
    }
    async function showChapter(n, pos = {
      paragraph: -1,
      offset: 0
    }) {
      storePosition();
      clearTimeout(saveTimer);
      restoreRevision++;
      const token = ++requestId;
      loaded = false;
      restoring = true;
      const target = Math.max(1, Math.min(total, n));
      requestedChapter = target;
      window.CSS?.highlights?.delete('novelmind-search-hit');
      paras.textContent = '正在加载正文…';
      $('[data-r-action="bookmark"]').disabled = true;
      try {
        const data = await ctx.getChapter(target);
        if (disposed || token !== requestId) return false;
        chapter = target;
        state.chapter = chapter;
        position = {
          chapter,
          paragraph: pos.paragraph ?? -1,
          offset: pos.offset || 0
        };
        $('[data-chapter-label]').textContent = `第 ${chapter} 章`;
        $('[data-chapter-title]').textContent = data.title;
        $('.r-chapter-meta').textContent = `${data.character_count.toLocaleString()} 字 · 原文`;
        paras.replaceChildren();
        data.content.split(/\r?\n/).filter(s => s.trim()).forEach((s, i) => {
          const p = document.createElement('p');
          p.dataset.paragraph = i;
          p.textContent = s;
          paras.append(p);
        });
        directory.querySelectorAll('[data-chapter]').forEach(b => b.setAttribute('aria-current', String(Number(b.dataset.chapter) === chapter)));
        $('[data-r-action="prev"]').disabled = chapter === 1;
        $('[data-r-action="next"]').disabled = chapter === total;
        $('[data-r-action="end-next"]').hidden = chapter === total;
        $('[data-end-label]').textContent = chapter === total ? '全书已读完 · 可以回到资料库选择下一本' : '本章已读完';
        $('[data-r-action="bookmark"]').disabled = false;
        search.value = '';
        filter();
        loaded = true;
        persist();
        restore(position);
        reveal();
        const hit = paras.children[pos.paragraph];
        if (hit && pos.match_length) {
          hit.classList.add('search-match');
          if (window.CSS?.highlights && typeof Highlight !== 'undefined') {
            const range = document.createRange();
            range.setStart(hit.firstChild, Math.min(pos.offset || 0, hit.textContent.length));
            range.setEnd(hit.firstChild, Math.min((pos.offset || 0) + pos.match_length, hit.textContent.length));
            CSS.highlights.set('novelmind-search-hit', new Highlight(range));
          }
        }
        if (target < total) ctx.prefetchChapter?.(target + 1);
        return true;
      } catch (error) {
        if (disposed || token !== requestId) return false;
        paras.textContent = error.message;
        const retry = document.createElement('button');
        retry.className = 'r-button';
        retry.textContent = '重试加载';
        retry.onclick = () => showChapter(target, pos);
        paras.append(retry);
        return false;
      }
    }
    for (let n = 1; n <= total; n++) {
      const b = document.createElement('button');
      b.className = 'r-chapter';
      b.dataset.chapter = n;
      b.setAttribute('aria-label', `第 ${n} 章 ${title(n)}`);
      const number = document.createElement('span');
      number.textContent = String(n).padStart(2, '0');
      const text = document.createElement('span');
      text.textContent = title(n);
      b.append(number, text);
      on(b, 'click', () => {
        showChapter(n);
        if (compactReader()) {
          compactDrawer = false;
          applyPrefs();
        }
      });
      on(b, 'keydown', e => {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          e.preventDefault();
          const visible = [...directory.querySelectorAll('button:not([hidden])')];
          const index = visible.indexOf(b) + (e.key === 'ArrowDown' ? 1 : -1);
          visible[Math.max(0, Math.min(visible.length - 1, index))]?.focus();
        }
      });
      directory.append(b);
    }
    const empty = document.createElement('div');
    empty.className = 'r-empty';
    empty.dataset.directoryEmpty = '';
    empty.textContent = '没有找到匹配章节';
    empty.hidden = true;
    directory.append(empty);
    function setTab(tab) {
      root.querySelectorAll('[data-tab]').forEach(b => b.setAttribute('aria-selected', String(b.dataset.tab === tab)));
      directory.hidden = tab !== 'chapters';
      $('.r-bookmarks').hidden = tab !== 'bookmarks';
      $('.r-search-wrap').hidden = tab !== 'chapters';
      $('.r-bookmark-search').hidden = tab !== 'bookmarks';
      $('.r-side-meta').hidden = tab !== 'chapters';
      if (tab === 'bookmarks') renderBookmarks();
    }
    function renderBookmarks() {
      const panel = $('.r-bookmarks');
      panel.replaceChildren();
      $('[data-bookmark-count]').textContent = bookmarks.length || '';
      if (!bookmarks.length) {
        const empty = document.createElement('p');
        empty.className = 'r-empty';
        empty.textContent = '还没有书签。读到想留下的位置，点击底部的书签图标。';
        panel.append(empty);
        return;
      }
      const filter = $('.r-bookmark-search').value.trim().toLocaleLowerCase();
      const visible = bookmarks.filter(b => [b.label, b.note, b.snippet, title(b.chapter)].join(' ').toLocaleLowerCase().includes(filter));
      if (!visible.length) {
        const empty = document.createElement('p');empty.className = 'r-empty';
        empty.textContent = '没有匹配的书签。';panel.append(empty);
      }
      for (const b of [...visible].sort((a, b) => a.chapter - b.chapter || a.paragraph - b.paragraph)) {
        const row = document.createElement('div');
        row.className = 'r-bookmark-row';
        const link = document.createElement('button');
        link.className = 'r-bookmark-entry';
        const titleEl = document.createElement('strong');
        titleEl.textContent = b.label || `第 ${b.chapter} 章 · ${title(b.chapter)}`;
        const snippet = document.createElement('small');
        snippet.textContent = b.snippet || '章节开头';
        const note = document.createElement('small');
        note.className = 'r-bookmark-note';note.textContent = b.note || '';
        if (b.label) {
          const source = document.createElement('small');source.textContent = `第 ${b.chapter} 章 · ${title(b.chapter)}`;
          link.append(titleEl, source, snippet, note);
        } else link.append(titleEl, snippet, note);
        on(link, 'click', () => showChapter(b.chapter, b));
        const remove = document.createElement('button');
        remove.className = 'r-button r-icon';
        remove.setAttribute('aria-label', `删除第 ${b.chapter} 章书签`);
        remove.innerHTML = svg('close');
        on(remove, 'click', () => {
          bookmarks = bookmarks.filter(x => x.id !== b.id);
          ctx.saveBookmark(b, true).catch(error => toast(error.message));
          renderBookmarks();
          progress();
        });
        const edit = document.createElement('button');edit.className = 'r-button';edit.textContent = '编辑';
        edit.setAttribute('aria-label', `编辑第 ${b.chapter} 章书签`);
        on(edit, 'click', () => editBookmark(b));
        const actions = document.createElement('div');actions.className = 'r-bookmark-actions';actions.append(edit, remove);
        row.append(link, actions);
        panel.append(row);
      }
    }
    function editBookmark(bookmark) {
      const d = modal('编辑书签', '<label>书签名称<input aria-label="书签名称" data-bookmark-label maxlength="80" placeholder="留空使用章节标题"></label><label>备注<textarea aria-label="书签备注" data-bookmark-note maxlength="500" rows="4" placeholder="记录此处值得记住的内容"></textarea></label><p class="muted">只修改名称和备注，原文位置与摘录保持不变。</p><footer><button class="primary" data-bookmark-save>保存书签</button></footer>');
      d.querySelector('[data-bookmark-label]').value = bookmark.label || '';
      d.querySelector('[data-bookmark-note]').value = bookmark.note || '';
      d.querySelector('[data-bookmark-save]').onclick = () => {
        bookmark.label = d.querySelector('[data-bookmark-label]').value.trim();
        bookmark.note = d.querySelector('[data-bookmark-note]').value.trim();
        ctx.saveBookmark({...bookmark}).then(() => toast('书签已更新')).catch(error => toast(error.message));
        renderBookmarks();d.closeSheet();
      };
    }
    function toggleBookmark() {
      if (!loaded) return;
      const a = anchor();
      const existing = bookmarks.find(b => b.chapter === chapter && b.paragraph === a.paragraph);
      if (existing) {
        bookmarks = bookmarks.filter(b => b !== existing);
        ctx.saveBookmark(existing, true).catch(error => toast(error.message));
        toast('已移除当前位置的书签');
      } else {
        if (bookmarks.length >= 500) {toast('最多保存500个书签，请先整理现有书签。');return;}
        bookmarks.push({
          ...a,
          id: `${chapter}:${a.paragraph}`,
          label: '', note: '',
          snippet: paras.children[a.paragraph] ? [...paras.children[a.paragraph].textContent.slice(a.offset)].slice(0,58).join('') : '章节开头'
        });
        ctx.saveBookmark({...bookmarks.at(-1)}).catch(error => toast(error.message));
        toast('已添加书签，可在书签列表编辑名称和备注');
      }
      position = a;
      persist();
      renderBookmarks();
      progress();
    }
    function settings() {
      wake();
      const d = modal('阅读设置', '<p class="r-setting-label">主题</p><div class="r-theme-options"><button data-theme="paper">纸白</button><button data-theme="sepia">暖纸</button><button data-theme="dark">夜间</button></div><p class="r-setting-label">字体</p><div class="r-font-options"><button data-font="serif">思源宋体</button><button data-font="sans">黑体</button><button data-font="pingfang">苹方</button></div><div data-sliders></div><p class="muted">即时生效 · 自动保存到本机资料库</p>');
      d.classList.add('reader-settings');
      const appearance = document.createElement('section');d.querySelector('.sheet-content').append(appearance);ctx.appearanceControls?.(appearance);
      for (const [name, label, min, max, step, unit] of [['size', '字号', 16, 28, 1, ' px'], ['line', '行距', 1.6, 2.2, .1, ''], ['width', '正文宽度', 560, 880, 20, ' px'], ['gap', '段间距', 12, 36, 2, ' px']]) {
        const wrapper = document.createElement('div');
        wrapper.innerHTML = `<label class="range-label" for="reader-${name}">${label}<output></output></label><input id="reader-${name}" aria-label="${label}" type="range" min="${min}" max="${max}" step="${step}">`;
        const input = wrapper.querySelector('input'),
          output = wrapper.querySelector('output');
        input.value = prefs[name];
        output.textContent = prefs[name] + unit;
        input.oninput = () => {
          const a = anchor();
          prefs[name] = Number(input.value);
          output.textContent = input.value + unit;
          applyPrefs();
          restore(a);
          position = a;
          persist();
        };
        d.querySelector('[data-sliders]').append(wrapper);
      }
      const sync = () => {
        d.querySelectorAll('[data-theme]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.theme === prefs.theme)));
        d.querySelectorAll('[data-font]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.font === prefs.font)));
      };
      d.querySelectorAll('[data-theme]').forEach(b => b.onclick = () => {
        prefs.theme = b.dataset.theme;
        applyPrefs();
        persist();
        sync();
      });
      d.querySelectorAll('[data-font]').forEach(b => b.onclick = () => {
        const a = anchor();
        prefs.font = b.dataset.font;
        applyPrefs();
        restore(a);
        position = a;
        persist();
        sync();
      });
      sync();
    }
    function jump() {
      const d = modal('阅读进度', `<p class="muted">共 ${total} 章 · 通过章节跳转，不必逐章翻找</p><p class="r-jump-label"></p><input type="range" min="1" max="${total}" aria-label="选择章节"><label for="reader-chapter-input">章节序号</label><input id="reader-chapter-input" type="number" min="1" max="${total}" aria-label="章节序号"><footer><button class="primary" data-jump>跳转阅读</button></footer>`);
      const range = d.querySelector('[type=range]'),
        number = d.querySelector('[type=number]'),
        label = d.querySelector('.r-jump-label');
      range.value = number.value = chapter;
      const update = source => {
        const n = Math.round(clamp(source.value, 1, total, chapter));
        label.textContent = `第 ${n} 章 · ${title(n)}`;
        range.value = n;
        if (source !== number) number.value = n;
      };
      update(range);
      range.oninput = () => update(range);
      number.oninput = () => update(number);
      d.querySelector('[data-jump]').onclick = () => {
        showChapter(Math.round(clamp(number.value, 1, total, chapter)));
        d.closeSheet();
        scroll.focus({
          preventScroll: true
        });
      };
    }
    function wake() {
      root.classList.remove('is-quiet');
      clearTimeout(idleTimer);
      if (focus) idleTimer = setTimeout(() => {
        if (!document.querySelector('dialog') && document.activeElement?.closest('.r-header,.r-dock') === null) root.classList.add('is-quiet');
      }, 3200);
    }
    function toggleFocus(value = !focus) {
      const a = anchor();
      focus = value;
      applyPrefs();
      restore(a);
      wake();
      scroll.focus({
        preventScroll: true
      });
    }
    const textSearch = ctx.createSearch?.({root, getPosition:() => loaded ? anchor() : {...position}, navigate:showChapter});
    const actions = {
      'text-search': () => {if (loaded) textSearch?.open();},
      library: () => go('library'),
      analysis: () => go('summary'),
      tasks: ctx.tasks,
      sidebar: () => {
        const a = anchor();
        const wasFocus = focus;
        if (wasFocus) focus = false;
        if (compactReader()) compactDrawer = wasFocus || !compactDrawer;
        else prefs.sidebar = wasFocus || !prefs.sidebar;
        applyPrefs();
        restore(a);
        if (!compactReader()) persist();
        if (sidebarOpen()) reveal();
      },
      focus: () => toggleFocus(),
      prev: () => showChapter(requestedChapter - 1),
      next: () => showChapter(requestedChapter + 1),
      'end-next': () => showChapter(requestedChapter + 1),
      locate: () => {
        setTab('chapters');
        search.value = '';
        filter();
        reveal();
      },
      appearance: settings,
      progress: jump,
      bookmark: toggleBookmark,
      help: () => modal('阅读快捷键', '<div class="r-shortcuts"><span>上一章 / 下一章</span><kbd>← / →</kbd><span>向下 / 向上阅读</span><kbd>Page Down / Up</kbd><span>添加或移除书签</span><kbd>B</kbd><span>切换专注模式</span><kbd>F</kbd><span>搜索全书正文</span><kbd>Ctrl / ⌘ + Shift + F</kbd><span>退出专注 / 关闭面板</span><kbd>Esc</kbd></div><p class="muted">输入框内不会触发阅读快捷键。</p>')
    };
    root.querySelectorAll('[data-r-action]').forEach(b => on(b, 'click', () => actions[b.dataset.rAction]?.(b)));
    root.querySelectorAll('[data-tab]').forEach(b => on(b, 'click', () => setTab(b.dataset.tab)));
    on(search, 'input', filter);
    on($('.r-bookmark-search'), 'input', renderBookmarks);
    on(scroll, 'scroll', () => {
      progress();
      clearTimeout(saveTimer);
      saveTimer = setTimeout(storePosition, 180);
    }, {
      passive: true
    });
    on(root, 'pointermove', wake, {
      passive: true
    });
    on(root, 'pointerdown', wake, {
      passive: true
    });
    on(document, 'keydown', e => {
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'f' && !document.querySelector('dialog') && loaded) {e.preventDefault();textSearch?.open();return;}
      if (document.querySelector('dialog') || e.ctrlKey || e.altKey || e.metaKey || e.target.closest('input,textarea,select,[contenteditable=true]')) return;
      if (e.key === 'PageDown' || e.key === 'PageUp') {
        e.preventDefault();
        scroll.scrollBy({
          top: scroll.clientHeight * .85 * (e.key === 'PageDown' ? 1 : -1),
          behavior: reduced() ? 'instant' : 'smooth'
        });
      } else if (e.key === 'Escape' && focus) {
        e.preventDefault();
        toggleFocus(false);
      } else if (e.key.toLowerCase() === 'f') {
        e.preventDefault();
        toggleFocus();
      } else if (e.key.toLowerCase() === 'b') {
        e.preventDefault();
        toggleBookmark();
      } else if (e.key === 'ArrowRight' && requestedChapter < total) {
        e.preventDefault();
        showChapter(requestedChapter + 1);
      } else if (e.key === 'ArrowLeft' && requestedChapter > 1) {
        e.preventDefault();
        showChapter(requestedChapter - 1);
      }
    });
    on(window, 'pagehide', () => storePosition({keepalive:true}));
    on(document, 'visibilitychange', () => {
      if (document.hidden) storePosition();
    });
    on(window, 'resize', () => {
      const a = anchor();
      requestAnimationFrame(() => restore(a));
    });
    if (state.returnTo) {
      const back = document.createElement('button');
      back.className = 'r-button r-return';
      back.textContent = '返回刚才的' + (state.returnTo === 'ask' ? '对话' : '分析');
      on(back, 'click', () => go(state.returnTo));
      root.append(back);
    }
    applyPrefs();
    const initial = {
      ...position
    };
    restoring = true;
    showChapter(chapter, initial);
    renderBookmarks();
    return () => {
      if (disposed) return;
      clearTimeout(saveTimer);
      clearTimeout(idleTimer);
      storePosition();
      disposed = true;
      textSearch?.dispose();
      window.CSS?.highlights?.delete('novelmind-search-hit');
      abort.abort();
      delete document.body.dataset.readerTheme;
    };
  }
  window.NovelReader = {
    mount
  };
})();
