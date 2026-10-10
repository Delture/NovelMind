import {createChapterLoader} from './reader-data.js';
import {createReaderSearch} from './reader-search.js';
import {appearanceControls} from './appearance.js';
import { api, allPages, optional } from './api.js';
import { modal, captureModalFocus, escape as e, button, icon, bind, toast, taskStatuses as statuses } from "./ui.js";

const toneCount = 6;
// Cover title: split at a separator when there is one, otherwise in half; each line stays short enough to typeset.
function coverLines(title) {
  const chars = [...title], half = Math.ceil(chars.length / 2);
  const parts = title.split(/\s*[-—–·:：_]\s*/).filter(Boolean);
  const lines = parts.length > 1 ? parts.slice(0, 2) : chars.length > 4 ? [chars.slice(0, half).join(''), chars.slice(half).join('')] : [title];
  return lines.map(line => [...line].length > 5 ? [...line].slice(0, 5).join('') + '…' : line);
}
const coverHTML = (book, title) => `<div class="book-cover" data-tone="${book.id % toneCount + 1}" aria-hidden="true">${coverLines(title).map(line => `<span>${e(line)}</span>`).join('')}</div>`;
// The list already shows the chapter number, so drop a leading “第N章”; a full-width space between paired headings becomes a plain one.
const chapterName = title => title.replace(/^第[\d零〇一二三四五六七八九十百千两]+[章回节卷]\s*/, '').replace(/\u3000+/g, ' ') || title;
const pad = n => String(n).padStart(2, '0');
export function createPages({
  root,
  state,
  base,
  bookTitle,
  shell,
  go,
  render,
  openTasks,
  plan
}) {
  let readerCleanup, pageRefresh;
  const chapterCache = new Map(),
    readingWrites = new Map();
  function searchBox(placeholder) {
    return `<div class="search">${icon('search')}<input type="search" placeholder="${e(placeholder)}" aria-label="${e(placeholder)}"></div>`;
  }
  async function library(fresh) {
    const books = await allPages('/novels', 100);
    if (!fresh()) return;
    let lastId;
    try {
      lastId = Number(localStorage.getItem('novelmind.last-book'));
    } catch {}
    const recent = books.find(b => b.id === lastId);
    let resume = '';
    if (recent) {
      const [list, saved] = await Promise.all([chapters(recent.id), api(base(recent.id) + '/reading')]);
      if (!fresh()) return;
      const at = Math.min(list.length, saved.position.chapter);
      resume = `<section class="continue">${coverHTML(recent, bookTitle(recent))}<div class="continue-body"><small class="eyebrow">继续阅读</small><h2>${e(bookTitle(recent))}</h2><p>第 ${at} 章 · ${e(chapterName(list[at - 1].title))}</p><div class="progress-line"><progress value="${at}" max="${list.length}" aria-label="阅读进度"></progress><span>${at} / ${list.length} 章 · ${Math.round(at / list.length * 100)}%</span></div></div><button type="button" class="primary" data-action="continue">继续阅读${icon('forward')}</button></section>`;
    }
    shell('你的资料库', '让故事的每一条线索，都有迹可循。', `
    ${resume}
    <div class="section-bar"><div class="section-label">全部小说 · ${books.length} 本</div>${searchBox('搜索书名')}</div><div class="books"></div><p data-no-match class="empty" hidden>没有找到这本书。</p>
    ${books.length ? '' : '<div class="empty"><h2>从一本小说开始</h2><p>导入 TXT 后即可阅读，章节和阅读记录保存在本机。</p></div>'}`, button('导入小说', 'import', true));
    const container = root.querySelector('.books');
    for (const book of books) {
      const article = document.createElement('article');
      article.className = 'book-card';
      article.dataset.title = book.filename;
      article.innerHTML = `${coverHTML(book, bookTitle(book))}<div class="book-info"><h2>${e(bookTitle(book))}</h2><p>${book.chapter_count} 章 · ${book.character_count.toLocaleString()} 字</p><footer>${button('开始阅读', 'read', true)}<button type="button" class="quiet" data-action="analysis">理解故事</button></footer></div><button class="manage" aria-label="管理 ${e(bookTitle(book))}">${icon('more')}</button>`;
      bind(article, {
        read: () => go('reader', book.id),
        analysis: () => go('summary', book.id)
      });
      article.querySelector('.manage').onclick = () => manageBook(book);
      container.append(article);
    }
    root.querySelector('input[type=search]').oninput = event => {
      let visible = 0;
      container.querySelectorAll('.book-card').forEach(card => {
        card.hidden = !card.dataset.title.toLowerCase().includes(event.target.value.trim().toLowerCase());
        if (!card.hidden) visible++;
      });
      root.querySelector('[data-no-match]').hidden = visible > 0 || !books.length;
    };
    bind(root, {
      import: importBook,
      continue: () => go('reader', recent.id)
    });
  }
  function importBook() {
    const d = modal('导入小说', `<div class="drop-zone"><p data-file-title>将 TXT 文件拖到这里</p><p class="muted">或从电脑中选择一本小说</p><input type="file" accept=".txt,text/plain" hidden aria-label="选择 TXT 文件"><button data-action="choose">选择文件</button></div><p class="muted">最大 20 MB · UTF-8 / GB18030 · 在本机解析</p><p data-status role="status"></p><footer><button class="primary" data-action="upload" disabled>导入</button></footer>`);
    let file;
    const input = d.querySelector('input'),
      zone = d.querySelector('.drop-zone'),
      status = d.querySelector('[data-status]'),
      submit = d.querySelector('[data-action=upload]');
    const pick = selected => {
      file = selected;
      const valid = file && /\.txt$/i.test(file.name) && file.size > 0 && file.size <= 20 * 1024 * 1024;
      submit.disabled = !valid;
      d.querySelector('[data-file-title]').textContent = valid ? file.name : '将 TXT 文件拖到这里';
      status.textContent = valid ? '' : '请选择非空且不超过 20 MB 的 TXT 文件。';
    };
    input.onchange = () => pick(input.files[0]);
    zone.ondragover = event => {
      event.preventDefault();
      zone.classList.add('over');
    };
    zone.ondragleave = () => zone.classList.remove('over');
    zone.ondrop = event => {
      event.preventDefault();
      zone.classList.remove('over');
      pick(event.dataTransfer.files[0]);
    };
    bind(d, {
      choose: () => input.click(),
      upload: async () => {
        status.textContent = '正在导入并解析章节…';
        try {
          const result = await api('/novels/upload?filename=' + encodeURIComponent(file.name), {
            method: 'POST',
            headers: {
              'Content-Type': 'application/octet-stream'
            },
            body: file
          });
          d.closeSheet();
          toast(`已导入 ${result.chapter_count} 章`);
          go('reader', result.novel_id);
        } catch (error) {
          status.textContent = error.message;
        }
      }
    });
  }
  function manageBook(book) {
    const d = modal('管理小说', `<p>删除《${e(bookTitle(book))}》及其章节、摘要、人物、问答、报告和阅读记录？此操作无法撤销。</p><footer><button data-action="cancel">取消</button><button data-action="delete">删除这本书</button></footer>`);
    bind(d, {
      cancel: () => d.closeSheet(),
      delete: async () => {
        await api(base(book.id), {
          method: 'DELETE'
        });
        chapterCache.delete(book.id);
        d.closeSheet();
        toast('小说已删除');
        render();
      }
    });
  }
  async function chapters(id) {
    if (!chapterCache.has(id)) chapterCache.set(id, await allPages(base(id) + '/chapters'));
    return chapterCache.get(id);
  }
  function queueReading(id, write) {
    const previous = readingWrites.get(id) || Promise.resolve();
    const next = previous.catch(() => {}).then(write);
    readingWrites.set(id, next);
    return next;
  }
  function saveReading(id, value, {keepalive = false} = {}) {
    const write = () => api(base(id) + '/reading/position', {method:'PUT', json:value, keepalive});
    // pagehide must start the request immediately, before the document is discarded.
    return keepalive ? write() : queueReading(id, write);
  }
  function saveBookmark(id, bookmark, remove = false) {
    return queueReading(id, () => api(base(id) + '/reading/bookmarks/' + encodeURIComponent(bookmark.id), {
      method: remove ? 'DELETE' : 'PUT', ...(remove ? {} : {json:bookmark})
    }));
  }
  async function reader(fresh, query) {
    const book = state.novel,
      id = book.id;
    await readingWrites.get(id)?.catch(() => {});
    const [list, saved] = await Promise.all([chapters(id), api(base(id) + '/reading')]);
    if (!fresh()) return;
    const target = Number(query.get('chapter'));
    state.returnTo = query.get('from') || null;
    if (target >= 1 && target <= list.length) {
      saved.position = {
        chapter: target,
        paragraph: -1,
        offset: 0
      };
      state.chapter = target;
    }
    try {
      localStorage.setItem('novelmind.last-book', id);
    } catch {}
    const loader = createChapterLoader((n, options) => api(base(id) + `/chapters/${n}`, options));
    const cleanup = window.NovelReader.mount({
      root,
      state,
      title: n => list[n - 1]?.title || '',
      go,
      modal,
      toast,
      tasks: openTasks,
      total: list.length,
      bookName: bookTitle(book),
      saved,
      getChapter: n => loader.get(n),
      prefetchChapter: n => loader.prefetch(n),
      appearanceControls,
      createSearch: options => createReaderSearch({...options, modal,
        request:(q, offset, signal) => api(base(id) + '/search?' + new URLSearchParams({q,offset,limit:20}), {signal})}),
      save: (value, options) => saveReading(id, value, options),
      saveBookmark: (bookmark, remove) => saveBookmark(id, bookmark, remove)
    });
    readerCleanup = () => {cleanup();loader.dispose();};
  }
  function citationLinks(indices) {
    return `<div class="citation-links">${[...new Set(indices)].map(n => `<button class="link" data-cite="${n}">第 ${n} 章 · 查看证据</button>`).join('')}</div>`;
  }
  function bindCitations(container, snippets = new Map()) {
    container.querySelectorAll('[data-cite]').forEach(node => node.onclick = () => evidence(Number(node.dataset.cite), snippets.get(Number(node.dataset.cite))).catch(error => toast(error.message)));
  }
  async function evidence(index, excerpt) {
    const returnFocus = captureModalFocus();
    const id = state.novel.id,
      from = state.page;
    const chapter = await api(base(id) + `/chapters/${index}`);
    const d = modal(chapter.title, `<p class="muted">${excerpt ? '本次回答引用的原文片段' : '章节原文（前 2,000 字），完整内容可进入阅读器查看'}</p><div class="evidence">${e(excerpt || chapter.content.slice(0, 2000))}</div><footer>${button('阅读这一章', 'read', true)}</footer>`, true, {returnFocus});
    bind(d, {
      read: () => {
        d.closeSheet();
        go('reader', id, {
          chapter: index,
          from
        });
      }
    });
  }
  async function summaries(fresh) {
    const id = state.novel.id;
    const [list, saved] = await Promise.all([chapters(id), allPages(base(id) + '/summaries')]);
    if (!fresh()) return;
    const map = new Map(saved.map(s => [s.chapter_index, s]));
    shell('章节梳理', `${saved.length} / ${list.length} 章已有摘要`, `<div class="split"><aside class="list-panel">${searchBox('搜索章节标题或序号')}<nav class="results-list" aria-label="摘要章节列表"></nav></aside><article class="detail" aria-live="polite"></article></div>`, button('补齐缺失摘要', 'generate', true));
    const nav = root.querySelector('.results-list'),
      detail = root.querySelector('.detail');
    let selected = Math.min(list.length, state.chapter || 1);
    const show = index => {
      selected = index;
      state.chapter = index;
      const summary = map.get(index),
        chapter = list[index - 1];
      nav.querySelectorAll('button').forEach(b => b.setAttribute('aria-current', String(Number(b.dataset.index) === index)));
      const action = button(summary ? '重新生成本章' : '生成本章摘要', 'single');
      detail.innerHTML = `<small class="eyebrow">第 ${index} 章</small><h2>${e(chapterName(chapter.title))}</h2>` + (summary ? `<section class="lead"><h3>这一章发生了什么</h3><p>${e(summary.summary)}</p></section><section><h3>关键事件</h3><ol class="events">${summary.key_events.map(s => `<li>${e(s)}</li>`).join('')}</ol></section><section><h3>故事推进</h3><p>${e(summary.plot_progression)}</p>${summary.cliffhanger ? `<p>${e(summary.cliffhanger)}</p>` : ''}</section><footer><div class="detail-meta"><small>${e(summary.model)} · ${e(summary.updated_at.slice(0, 10))}</small>${citationLinks([index])}</div>${action}</footer>` : `<div class="empty"><p>这一章还没有摘要。</p></div><footer><div class="detail-meta">${citationLinks([index])}</div>${action}</footer>`);
      bindCitations(detail);
      bind(detail, {
        single: () => plan('chapter', {
          id,
          index,
          force: !!summary
        })
      });
    };
    for (const chapter of list) {
      const row = document.createElement('button');
      row.className = 'row numbered';
      row.dataset.index = chapter.index;
      row.title = chapter.title;
      row.innerHTML = `<span class="row-index">${pad(chapter.index)}</span><span class="row-title">${e(chapterName(chapter.title))}</span><small>${map.has(chapter.index) ? '已生成' : '待生成'}</small>`;
      row.onclick = () => show(chapter.index);
      nav.append(row);
    }
    show(selected);
    root.querySelector('input').oninput = event => {
      const term = event.target.value.trim();
      nav.querySelectorAll('button').forEach(b => b.hidden = term && !(/^\d+$/.test(term) ? Number(b.dataset.index) === Number(term) : b.title.includes(term)));
    };
    if (saved.length === list.length) root.querySelector('[data-action=generate]').textContent = '检查并补齐摘要';
    bind(root, {
      generate: () => plan('summary', {
        id
      })
    });
    pageRefresh = () => summaries(fresh);
  }
  async function characters(fresh) {
    const id = state.novel.id;
    const people = await allPages(base(id) + '/characters');
    if (!fresh()) return;
    shell('人物', `${people.length} 位人物 · 简介、出场与关系`, `<div class="split"><aside class="list-panel">${searchBox('搜索人物或别名')}<nav class="results-list" aria-label="人物列表"></nav></aside><article class="detail"></article></div>`, button('更新人物分析', 'generate', true));
    const nav = root.querySelector('.results-list'),
      detail = root.querySelector('.detail');
    let selection = 0;
    const show = async person => {
      const token = ++selection;
      nav.querySelectorAll('button').forEach(b => b.setAttribute('aria-current', String(Number(b.dataset.id) === person.id)));
      detail.innerHTML = '<p class="skeleton">正在加载人物…</p>';
      try {
        const [info, relationships, appearances] = await Promise.all([api(base(id) + `/characters/${person.id}`), allPages(base(id) + `/characters/${person.id}/relationships`), allPages(base(id) + `/characters/${person.id}/appearances`)]);
        if (!fresh() || token !== selection) return;
        detail.innerHTML = `<h2>${e(info.canonical_name)}</h2><small>${e(info.aliases.join(' · '))}</small><p>${e(info.short_description)}</p><div class="stats"><div><strong>${info.chapter_count}</strong><span>出场章节</span></div><div><strong>${info.mention_count}</strong><span>提及次数</span></div><div><strong>${info.relationship_count}</strong><span>人物关系</span></div></div>
        <section><h3>人物关系</h3>${relationships.length ? relationships.map(r => `<div class="relation"><strong>${e(r.source_character_name)} 与 ${e(r.target_character_name)} · ${e(r.relation_type)}</strong><p>${e(r.description)}</p>${citationLinks(r.evidence_chapter_indices)}</div>`).join('') : '<p class="muted">暂无已保存的关系记录。</p>'}</section>
        <section><h3>出场记录</h3>${appearances.map(a => `<div class="relation"><strong>${e(a.chapter_title)}</strong><p>${e(a.summary)}</p>${citationLinks([a.chapter_index])}</div>`).join('')}</section>`;
        bindCitations(detail);
      } catch (error) {
        if (fresh() && token === selection) {
          detail.innerHTML = `<p>${e(error.message)}</p>${button('重试', 'retry')}`;
          bind(detail, {
            retry: () => show(person)
          });
        }
      }
    };
    for (const person of people) {
      const row = document.createElement('button');
      row.className = 'row';
      row.dataset.id = person.id;
      row.dataset.search = person.canonical_name + ' ' + person.aliases.join(' ');
      row.innerHTML = `<span>${e(person.canonical_name)}</span><small>${person.chapter_count} 章 · ${person.mention_count} 次提及</small>`;
      row.onclick = () => show(person);
      nav.append(row);
    }
    if (people.length) await show(people[0]);else detail.innerHTML = '<div class="empty"><h2>等待故事中的人物登场</h2><p>开始人物分析后，简介、出场与关系会显示在这里。</p></div>';
    if (!fresh()) return;
    root.querySelector('input').oninput = event => nav.querySelectorAll('button').forEach(b => b.hidden = !b.dataset.search.includes(event.target.value.trim()));
    bind(root, {
      generate: () => plan('characters', {
        id
      })
    });
    pageRefresh = () => characters(fresh);
  }
  function questionHTML(question) {
    return `<div class="question">${e(question.question)}</div><section class="answer"><small>NovelMind · ${e(statuses[question.status])}</small><p>${e(question.answer || question.error_message || '正在根据原文整理回答…')}</p>${question.insufficient_evidence ? '<p class="muted">现有证据不足以完整回答这个问题。</p>' : ''}<div class="citation-links">${question.citations.map((c, i) => `<button class="link" data-question="${e(question.id)}" data-citation="${i}">第 ${c.chapter_index} 章 · ${e(c.chapter_title)}</button>`).join('')}</div></section>`;
  }
  async function ask(fresh) {
    const id = state.novel.id;
    const [index, history] = await Promise.all([api(base(id) + '/rag/index'), allPages(base(id) + '/rag/questions', 100)]);
    if (!fresh()) return;
    shell('与故事对话', '答案来自原文，引用按需展开。', `<div class="conversation" aria-live="polite"></div><form class="composer"><textarea aria-label="你的问题" placeholder="想了解故事中的什么？" maxlength="2000" required></textarea><footer><small>${index.current ? '原文已就绪' : '首次提问会先准备原文索引'}</small><button class="primary" type="submit">${index.current ? '提问' : '准备原文并提问'}</button></footer></form>`, button(index.current ? '原文已就绪' : '原文待准备', 'index'));
    const conversation = root.querySelector('.conversation');
    const draw = questions => {
      conversation.innerHTML = questions.length ? [...questions].reverse().map(questionHTML).join('') : '<div class="empty"><h2>从一个问题开始</h2><p>人物的选择、情节的伏笔，或一条贯穿全书的线索。</p></div>';
      conversation.querySelectorAll('[data-citation]').forEach(b => b.onclick = () => {
        const q = questions.find(q => q.id === b.dataset.question),
          c = q.citations[Number(b.dataset.citation)];
        evidence(c.chapter_index, c.excerpt).catch(error => toast(error.message));
      });
    };
    draw(history);
    const form = root.querySelector('form');
    form.onsubmit = async event => {
      event.preventDefault();
      const question = form.querySelector('textarea').value.trim();
      if (!question) return;
      try {
        await plan('ask', {
          id,
          question,
          onStart: () => {
            form.querySelector('textarea').value = '';
          }
        });
      } catch (error) {
        toast(error.message);
      }
    };
    bind(root, {
      index: () => plan('index', {
        id
      })
    });
    pageRefresh = async () => {
      const [questions, current] = await Promise.all([allPages(base(id) + '/rag/questions', 100), api(base(id) + '/rag/index')]);
      if (!fresh()) return;
      draw(questions);
      root.querySelector('[data-action=index]').textContent = current.current ? '原文已就绪' : '原文待准备';
      form.querySelector('small').textContent = current.current ? '原文已就绪' : '首次提问会先准备原文索引';
      form.querySelector('[type=submit]').textContent = current.current ? '提问' : '准备原文并提问';
    };
  }
  async function report(fresh) {
    const id = state.novel.id;
    const saved = await optional(base(id) + '/report');
    if (!fresh()) return;
    const fields = {
      executive_summary: '全书概览',
      plot_structure: '情节结构',
      character_dynamics: '人物变化',
      pacing: '节奏',
      themes: '主题',
      strengths: '亮点',
      risks: '待完善之处',
      recommendations: '修改建议'
    };
    let body = '<div class="empty"><h2>看见故事的全貌</h2><p>生成前会先补齐缺失或过期的章节摘要，再整理全书报告。</p></div>';
    if (saved) {
      body = `${saved.up_to_date ? '' : '<p class="status-note">这份报告的来源已更新。你可以继续阅读旧结果，或生成新版。</p>'}<div class="report-meta"><h2>${e(saved.report.title)}</h2><p class="muted">${e(saved.model)} · ${saved.summary_count} 章摘要 · ${e(saved.updated_at.slice(0, 10))}</p></div><div class="report-sections">`;
      for (const [key, label] of Object.entries(fields)) {
        const sections = Array.isArray(saved.report[key]) ? saved.report[key] : [saved.report[key]];
        for (const section of sections) if (section) body += `<section class="report-section" data-kind="${key}"><small class="eyebrow">${label}</small><h3>${e(section.title)}</h3><p>${e(section.analysis)}</p>${citationLinks(section.evidence_chapter_indices)}</section>`;
      }
      body += '</div>';
    }
    shell('全书报告', '从章节到全书，整理结构、人物与节奏。', body, button('补齐摘要并更新报告', 'generate', true));
    bindCitations(root);
    bind(root, {
      generate: () => plan('report', {
        id
      })
    });
    pageRefresh = () => report(fresh);
  }
  return {
    library,
    reader,
    summaries,
    characters,
    ask,
    report,
    get refresh() {
      return pageRefresh;
    },
    cleanup() {
      readerCleanup?.();
      readerCleanup = null;
      pageRefresh = null;
    }
  };
}
