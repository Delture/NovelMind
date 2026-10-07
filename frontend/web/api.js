// 所有业务请求同源；API Key 只在用户保存设置时发送，绝不写入浏览器存储。
export async function api(path, options = {}) {
  const {
    json,
    ...rest
  } = options;
  const response = await fetch('/api' + path, {
    ...rest,
    ...(json !== undefined ? {
      body: JSON.stringify(json),
      headers: {
        'Content-Type': 'application/json'
      }
    } : {})
  });
  if (!response.ok) {
    let detail;
    try {
      detail = (await response.json()).detail;
    } catch {}
    const error = new Error(typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(x => x.msg).join('；') : `请求失败（${response.status}），请重试。`);
    error.status = response.status;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

// 分页是传输细节，界面始终显示连续且可搜索的完整目录。
export async function allPages(path, limit = 200) {
  const items = [];
  for (let offset = 0;;) {
    const result = await api(`${path}${path.includes('?') ? '&' : '?'}offset=${offset}&limit=${limit}`);
    items.push(...result.items);
    offset += result.items.length;
    if (offset >= result.total || !result.items.length) return items;
  }
}
export async function optional(path) {
  try {
    return await api(path);
  } catch (error) {
    if (error.status === 404) return null;
    throw error;
  }
}
