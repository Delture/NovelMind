// 全书正文搜索浮层。请求只返回片段，锚点使用与 DOM Range 相同的 UTF-16 单位。
export function createReaderSearch({root, getPosition, navigate, request, modal}) {
  let dialog, controller, timer, revision = 0, disposed = false;
  let query = '', items = [], nextOffset = null, origin = null, jumped = false;
  const back = document.createElement('button');
  back.className = 'r-button r-search-return';
  back.textContent = '返回搜索前位置';
  back.hidden = true;
  root.append(back);
  back.onclick = async () => {
    if (!origin) return;
    back.disabled = true;
    try {
      if (await navigate(origin.chapter, origin) !== false) {
        origin = null;jumped = false;back.hidden = true;
      }
    } finally {back.disabled = false;}
  };

  function cancel() {
    clearTimeout(timer);controller?.abort();revision++;
  }

  function render(d) {
    const list = d.querySelector('[data-search-results]');
    list.replaceChildren();
    for (const item of items) {
      const button = document.createElement('button');
      button.className = 'search-hit';
      const heading = document.createElement('strong');
      heading.textContent = `第 ${item.chapter_index} 章 · ${item.chapter_title}`;
      const snippet = document.createElement('span'), mark = document.createElement('mark');
      const start = item.snippet_match_start, end = start + item.snippet_match_length;
      snippet.append(document.createTextNode(item.snippet.slice(0, start)));
      mark.textContent = item.snippet.slice(start, end);
      snippet.append(mark, document.createTextNode(item.snippet.slice(end)));
      const action = document.createElement('small');action.textContent = '打开章节中的此处';
      button.append(heading, snippet, action);
      button.onclick = async () => {
        jumped = true;d.closeSheet();
        if (await navigate(item.chapter_index, item) !== false && !disposed) back.hidden = false;
      };
      list.append(button);
    }
    d.querySelector('[data-search-more]').hidden = nextOffset === null;
  }

  async function search(d, append=false) {
    cancel();
    const token = revision;
    const input = d.querySelector('[data-text-query]');
    query = input.value.trim();
    const status = d.querySelector('[data-search-status]');
    const more = d.querySelector('[data-search-more]');
    const retry = d.querySelector('[data-search-retry]');
    retry.hidden = true;
    if (!append) {items = [];nextOffset = null;render(d);}
    if (!query) {status.textContent = '输入文字，在这本书的正文中查找。';return;}
    if ([...query].length > 200 || /[\r\n\0]/.test(query)) {
      status.textContent = '请输入最多200个字符的单行文字。';return;
    }
    controller = new AbortController();
    more.disabled = true;status.textContent = append ? '正在加载更多结果…' : '正在搜索…';
    try {
      const result = await request(query, append ? nextOffset : 0, controller.signal);
      if (disposed || token !== revision || d.lifecycle.aborted || !d.isConnected) return;
      items = append ? [...items, ...result.items] : result.items;
      nextOffset = result.next_offset;
      render(d);
      status.textContent = items.length ? `已显示 ${items.length} 条片段 · 每章前 ${result.per_chapter_limit} 处匹配${result.has_more && nextOffset === null ? '，已到分页上限，请缩小查询' : ''}` : '没有找到匹配文字，试试更短的词。';
    } catch (error) {
      if (error.name === 'AbortError' || disposed || token !== revision || d.lifecycle.aborted) return;
      status.textContent = error.message;
      retry.hidden = false;
      retry.onclick = () => search(d, append);
    } finally {
      if (token === revision && !d.lifecycle.aborted) more.disabled = false;
    }
  }

  return {
    open() {
      if (disposed) return;
      if (!origin) origin = {...getPosition()};
      const d = modal('搜索全文', `<p class="muted">仅在本机正文中查找，符号按字面匹配并区分大小写。每章展示前3处非重叠匹配；片段数不是全书出现总次数。点击结果进入正文继续阅读，不会连接外部模型。</p>
        <label>搜索文字<input type="search" data-text-query aria-label="在全书正文中搜索" maxlength="400" autocomplete="off"></label>
        <p role="status" data-search-status>输入文字，在这本书的正文中查找。</p>
        <div class="text-search-results" data-search-results></div>
        <footer><button data-search-retry hidden>重试搜索</button><button data-search-more hidden>更多结果</button></footer>`, true);
      dialog = d;d.classList.add('text-search-sheet');
      const input = d.querySelector('[data-text-query]');input.value = query;
      render(d);
      if (query) d.querySelector('[data-search-status]').textContent = items.length ? `已显示 ${items.length} 条片段 · 每章前3处匹配` : '修改搜索文字，或按 Enter 重新搜索。';
      input.oninput = () => {
        cancel();items = [];nextOffset = null;render(d);
        d.querySelector('[data-search-status]').textContent = input.value.trim() ? '等待输入…' : '输入文字，在这本书的正文中查找。';
        timer = setTimeout(() => search(d), 250);
      };
      input.onkeydown = event => {if (event.key === 'Enter') {event.preventDefault();search(d);}};
      d.querySelector('[data-search-more]').onclick = () => search(d, true);
      d.lifecycle.addEventListener('abort', () => {
        cancel();if (dialog === d) dialog = null;
        if (!jumped) origin = null;
      }, {once:true});
      input.focus();
    },
    dispose() {disposed = true;cancel();dialog?.dispose({restoreFocus:false});back.remove();},
  };
}
