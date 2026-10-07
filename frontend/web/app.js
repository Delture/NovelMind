import {animateEnter} from './appearance.js';
import { api } from "./api.js";
import { escape as e, button, icon, bind, toast } from "./ui.js";
import { openSettings, privacy } from "./settings.js";
import { createPages } from './pages.js';
import { createTasks } from './tasks.js';
const root = document.querySelector('#app');
const routes = {
  library: '资料库',
  reader: '阅读',
  summary: '章节梳理',
  characters: '人物',
  ask: '原文问答',
  report: '全书报告'
};
const state = {
  page: 'library',
  novel: null,
  chapter: 1,
  returnTo: null
};
let revision = 0,
  taskTimer;
const base = (id = state.novel?.id) => `/novels/${id}`;
const bookTitle = book => book.filename.replace(/\.txt$/i, '');
function errorPanel(error, retry) {
  root.innerHTML = `<div class="workspace"><div class="error-panel" role="alert"><h2>暂时无法加载</h2><p>${e(error.message)}</p>${button('重试', 'retry')}${button('返回资料库', 'library')}</div></div>`;
  bind(root, {
    retry,
    library: () => go('library')
  });
}
function shell(title, description, body, actions = '') {
  const book = state.novel;
  root.className = '';
  root.innerHTML = `<div class="shell"><aside class="sidebar"><div class="brand">NovelMind<small>阅读工作室</small></div>
    ${book ? `<div class="book-context"><button class="link" data-action="library">${icon('back')}资料库</button><strong>${e(bookTitle(book))}</strong><small>${book.chapter_count} 章 · ${book.character_count.toLocaleString()} 字</small></div>` : ''}
    <nav aria-label="主导航">${(book ? ['reader', 'summary', 'characters', 'ask', 'report'] : ['library']).map(page => `<button data-action="nav-${page}" aria-current="${state.page === page ? 'page' : 'false'}">${routes[page]}</button>`).join('')}</nav>
    <div class="sidebar-footer">${button('任务', 'tasks')}${button('设置', 'settings')}</div></aside>
    <main class="workspace"><header class="page-header"><div><h1>${e(title)}</h1><p>${e(description)}</p></div><div class="header-actions">${actions}</div></header>${body}</main></div>`;
  const handlers = {
    library: () => go('library'),
    tasks: tasks.openTasks,
    settings: openSettings
  };
  Object.keys(routes).forEach(page => handlers['nav-' + page] = () => {
    state.returnTo = null;
    go(page);
  });
  bind(root, handlers);
}
function go(page, novelId = state.novel?.id, extra = {}) {
  const query = new URLSearchParams({
    page
  });
  if (novelId && page !== 'library') query.set('novel', novelId);
  for (const [key, value] of Object.entries(extra)) if (value != null) query.set(key, value);
  if (location.hash.slice(1) === query.toString()) render();else location.hash = query.toString();
}
async function render() {
  const token = ++revision;
  const observedTasks = tasks.snapshot;
  document.querySelectorAll('dialog').forEach(d => d.dispose?.({restoreFocus:false}));
  pages.cleanup();
  root.className = '';
  delete root.dataset.readerRoot;
  root.removeAttribute('style');
  const query = new URLSearchParams(location.hash.slice(1));
  state.page = routes[query.get('page')] ? query.get('page') : 'library';
  const novelId = Number(query.get('novel'));
  root.setAttribute('aria-busy', 'true');
  if (!root.firstElementChild) root.innerHTML = '<p class="skeleton" role="status">正在加载…</p>';
  const loadingTimer = setTimeout(() => {if (token === revision) root.classList.add('page-loading');}, 120);
  try {
    if (state.page !== 'library') {
      if (!novelId) {
        go('library');
        return;
      }
      const book = await api(base(novelId));
      if (token !== revision) return;
      state.novel = book;
    } else state.novel = null;
    const fresh = () => token === revision;
    if (state.page === 'library') await pages.library(fresh);
    if (state.page === 'reader') await pages.reader(fresh, query);
    if (state.page === 'summary') await pages.summaries(fresh);
    if (state.page === 'characters') await pages.characters(fresh);
    if (state.page === 'ask') await pages.ask(fresh);
    if (state.page === 'report') await pages.report(fresh);
    // 任务可能在页面请求期间完成，不能把那次更新丢在尚未注册的刷新回调里。
    if (fresh() && observedTasks !== tasks.snapshot && pages.refresh) await pages.refresh();
    if (fresh()) animateEnter(root);
  } catch (error) {
    if (token === revision) errorPanel(error, render);
  } finally {
    clearTimeout(loadingTimer);
    if (token === revision) {root.classList.remove('page-loading');root.removeAttribute('aria-busy');}
  }
}
const tasks = createTasks({
  root,
  base,
  go,
  getPageRefresh: () => pages.refresh
});
const pages = createPages({
  root,
  state,
  base,
  bookTitle,
  shell,
  go,
  render,
  openTasks: tasks.openTasks,
  plan: tasks.plan
});
addEventListener('hashchange', render);
addEventListener('pagehide', () => {
  pages.cleanup();
  clearInterval(taskTimer);
});
await render();
tasks.pollTasks();
taskTimer = setInterval(tasks.pollTasks, 2500);
// 首次进入只介绍并保存选择，绝不在加载页面时发起模型任务。
api('/workspace/privacy').then(value => {
  if (!value.decided && !document.querySelector('dialog')) privacy().catch(error => toast(error.message));
}).catch(() => {});
