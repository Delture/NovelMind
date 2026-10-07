// 缓存属于一次阅读挂载；保存的正文数和字符总量都有上限。
export function createChapterLoader(fetchChapter, {maxEntries=4, maxCharacters=300000}={}) {
  const cache = new Map(), pending = new Map();
  let characters = 0, disposed = false;

  function remember(index, chapter) {
    const size = chapter.content.length;
    if (size > maxCharacters) return;
    if (cache.has(index)) characters -= cache.get(index).content.length;
    cache.delete(index);
    cache.set(index, chapter);
    characters += size;
    while (cache.size > maxEntries || characters > maxCharacters) {
      const oldest = cache.keys().next().value;
      characters -= cache.get(oldest).content.length;
      cache.delete(oldest);
    }
  }

  function request(index) {
    if (disposed) return Promise.reject(new DOMException('阅读器已关闭', 'AbortError'));
    if (cache.has(index)) {
      const value = cache.get(index);
      cache.delete(index);
      cache.set(index, value);
      return Promise.resolve(value);
    }
    if (pending.has(index)) return pending.get(index).promise;
    const controller = new AbortController();
    const entry = {controller};
    entry.promise = Promise.resolve().then(() => fetchChapter(index, {signal:controller.signal})).then(chapter => {
      if (disposed || controller.signal.aborted || pending.get(index) !== entry)
        throw new DOMException('章节请求已过时', 'AbortError');
      remember(index, chapter);
      return chapter;
    }).finally(() => {if (pending.get(index) === entry) pending.delete(index);});
    pending.set(index, entry);
    return entry.promise;
  }

  return {
    get(index) {
      for (const [other, entry] of pending) {
        if (other !== index) {entry.controller.abort(); pending.delete(other);}
      }
      return request(index);
    },
    prefetch(index) {
      if (disposed || pending.size || cache.has(index)) return Promise.resolve();
      return request(index).catch(() => undefined);
    },
    dispose() {
      disposed = true;
      for (const entry of pending.values()) entry.controller.abort();
      pending.clear(); cache.clear(); characters = 0;
    },
  };
}
