import { api } from "./api.js";
import { modal, captureModalFocus, escape as e, bind, toast, safeLink } from "./ui.js";
import { consent, consentFields } from "./settings.js";

// 处理计划、持久任务轮询和任务浮层独立于当前页面生命周期。
export function createTasks({
  root,
  base,
  go,
  getPageRefresh
}) {
  const labels = {
    chapter_summaries: '章节摘要',
    characters: '人物分析',
    rag_index: '原文索引',
    report: '全书报告',
    question: '原文问答',
    ask: '准备原文并提问'
  };
  const statuses = {
    queued: '等待处理',
    running: '进行中',
    succeeded: '已完成',
    failed: '失败'
  };
  let taskSnapshot = '',
    taskBusy = false,
    taskError = '',
    lastTasks = [];
  function estimateHTML(estimate) {
    let text = '';
    if (estimate.pending_chapter_count != null) text += `<p>${estimate.chapter_count} 章 · ${estimate.pending_chapter_count} 章待处理 · ${estimate.cached_chapter_count} 章可复用</p>`;
    if (estimate.estimated_generation_steps != null) text += `<p>预计生成 ${estimate.estimated_generation_steps} 步；请求尝试上限 ${estimate.maximum_request_attempts} 次。</p>`;
    if (estimate.estimated_new_embedding_count != null) text += `<p>待生成 ${estimate.estimated_new_embedding_count} 个原文向量；索引请求尝试上限 ${estimate.maximum_index_embedding_attempts} 次。提问向量上限 ${estimate.maximum_question_embedding_attempts} 次，回答生成上限 ${estimate.maximum_answer_generation_attempts} 次。</p>`;
    text += `<p class="muted">${e(estimate.data_handling_notice)}</p><div class="notice-links">`;
    for (const key of ['terms_url', 'privacy_url', 'embedding_terms_url', 'embedding_privacy_url', 'generation_terms_url', 'generation_privacy_url']) if (estimate[key]) text += safeLink(estimate[key], {
      terms_url: '服务条款',
      privacy_url: '隐私政策',
      embedding_terms_url: '向量服务条款',
      embedding_privacy_url: '向量隐私政策',
      generation_terms_url: '生成服务条款',
      generation_privacy_url: '生成隐私政策'
    }[key]);
    return text + '</div>';
  }
  async function plan(kind, options) {
    const returnFocus = captureModalFocus(options.trigger);
    const {
      id,
      index,
      question,
      force = false,
      onStart
    } = options;
    if (!(await consent(returnFocus))) return;
    const endpoint = {
      summary: 'summaries',
      chapter: 'summaries',
      characters: 'characters',
      report: 'summaries',
      ask: 'rag',
      index: 'rag'
    }[kind];
    const estimate = await api(base(id) + `/${endpoint}/estimate`);
    const title = {
      summary: '补齐缺失摘要',
      chapter: '生成本章摘要',
      characters: '更新人物分析',
      report: '补齐摘要并更新报告',
      ask: estimate.index_current ? '提问' : '准备原文并提问',
      index: '准备原文索引'
    }[kind];
    let extra = '';
    if (kind === 'report') {
      try {
        extra = '<h3>报告阶段</h3>' + estimateHTML(await api(base(id) + '/report/estimate'));
      } catch (error) {
        if (error.status !== 409) throw error;
        extra = '<p>完成摘要后将继续生成报告，发送标题、摘要和可选人物结果。报告调用量需在摘要齐全后确定；本次操作包含这两个阶段。</p>';
      }
    }
    if (kind === 'chapter') extra = `<p>本次仅处理第 ${index} 章。以上估算为全书范围；${force ? '将重新生成本章，产生新的调用费用。' : '已有有效结果会被复用。'}</p>`;
    const d = modal(title, `${question ? `<p>${e(question)}</p>` : ''}${estimateHTML(estimate)}${extra}<p class="muted">点击开始会真实调用所配置的服务，可能产生费用。失败时保留已有结果；关闭页面后任务继续。</p><p data-error class="error" role="status"></p><footer><button data-action="start" class="primary">开始处理</button></footer>`, false, {returnFocus});
    bind(d, {
      start: async () => {
        // 再次读取授权，避免另一个设置窗口撤销后仍提交。
        const privacyState = await api('/workspace/privacy');
        if (!privacyState.external || !privacyState.rights) {
          d.querySelector('[data-error]').textContent = '外部处理选择已更改，请重新确认。';
          return;
        }
        let path,
          json = consentFields();
        if (kind === 'report' || kind === 'ask') {
          path = 'workflows';
          json = {
            ...json,
            kind,
            ...(question ? {
              question
            } : {})
          };
        } else if (kind === 'chapter') {
          path = `chapters/${index}/summary`;
          json.force = force;
        } else path = {
          summary: 'summaries',
          characters: 'characters',
          index: 'rag/index'
        }[kind];
        try {
          await api(base(id) + '/' + path, {
            method: 'POST',
            json
          });
          d.closeSheet();
          onStart?.();
          toast('任务已提交，进度会自动更新');
          await pollTasks();
          openTasks();
        } catch (error) {
          d.querySelector('[data-error]').textContent = error.message;
        }
      }
    });
  }
  function taskHTML() {
    if (taskError) return `<p role="alert">${e(taskError)}</p><p class="muted">正在自动重连…</p>`;
    if (!lastTasks.length) return '<p class="empty">目前没有后台任务。</p>';
    return lastTasks.map(t => {
      const page = t.kind === 'report' ? 'report' : t.kind === 'ask' || t.kind === 'question' || t.kind === 'rag_index' ? 'ask' : t.kind === 'characters' ? 'characters' : 'summary';
      const count = t.progress_total ? `<small>${t.progress_current} / ${t.progress_total}</small>` : '';
      const cancel = ['queued', 'running'].includes(t.status) ? `<button class="link" data-task-cancel="${e(t.id)}">取消任务</button>` : '';
      return `<section class="task"><div class="task-heading"><strong>${e(t.phase ? t.kind === 'report' ? '摘要与报告' : labels[t.kind] : labels[t.kind] || t.kind)}</strong><span>${statuses[t.status] || e(t.status)}</span></div><p>${e(t.filename)}</p>${t.phase ? `<p class="muted">${e(t.phase)}</p>` : ''}${t.progress_total ? `<progress value="${t.progress_current}" max="${t.progress_total}"></progress>` : ''}${t.error_message ? `<p class="error">${e(t.error_message)}</p>` : ''}<div class="task-actions">${count}${cancel}<button class="link" data-task-book="${t.novel_id}" data-task-page="${page}">查看结果${t.status === 'failed' ? ' / 重新发起' : ''}</button></div></section>`;
    }).join('');
  }
  function drawTasks() {
    const panel = document.querySelector('[data-tasks]');
    if (!panel) return;
    panel.innerHTML = taskHTML();
    panel.querySelectorAll('[data-task-cancel]').forEach(b => b.onclick = async () => {
      b.disabled = true;
      try {
        await api('/workspace/tasks/' + encodeURIComponent(b.dataset.taskCancel) + '/cancel', {method:'POST'});
        await pollTasks();
      } catch (error) {b.disabled = false;toast(error.message);}
    });
    panel.querySelectorAll('[data-task-book]').forEach(b => b.onclick = () => {
      document.querySelector('dialog')?.closeSheet();
      go(b.dataset.taskPage, Number(b.dataset.taskBook));
    });
  }
  async function openTasks(trigger) {
    modal('任务', '<p class="muted">后台进度自动更新。关闭页面不会中断任务；服务重启后可查看中断原因并重新发起。</p><div data-tasks></div>', true, {returnFocus:captureModalFocus(trigger)});
    drawTasks();
    await pollTasks();
  }
  async function pollTasks() {
    if (taskBusy) return;
    taskBusy = true;
    try {
      const result = await api('/workspace/tasks');
      taskError = '';
      const signature = result.map(t => `${t.id}:${t.status}:${t.progress_current}:${t.phase}`).join('|');
      const changed = result.some(t => {
        const old = lastTasks.find(x => x.id === t.id);
        return old?.status !== t.status && ['succeeded', 'failed'].includes(t.status) || !old && t.kind === 'question';
      });
      taskSnapshot = signature;
      lastTasks = result;
      drawTasks();
      const running = result.filter(t => ['queued', 'running'].includes(t.status)).length;
      root.querySelectorAll('[data-action=tasks]').forEach(b => b.querySelector('span').textContent = running ? `任务 · ${running}` : '任务');
      if (changed && getPageRefresh()) await getPageRefresh()();
    } catch (error) {
      taskError = error.message;
      drawTasks();
    } finally {
      taskBusy = false;
    }
  }
  return {
    plan,
    openTasks,
    pollTasks,
    get snapshot() {
      return taskSnapshot;
    }
  };
}
