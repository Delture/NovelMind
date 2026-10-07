import {motionReduced} from './appearance.js';
import { icon, customSelect } from './controls.js';
export { icon };
export const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;'
})[c]);
export const button = (label, action, primary = false) => `<button type="button" ${primary ? 'class="primary"' : ''} data-action="${escape(action)}">${escape(label)}</button>`;
export function toast(message) {
  document.querySelector('.toast')?.remove();
  const node = document.createElement('div');
  node.className = 'toast';
  node.setAttribute('role', 'status');
  node.textContent = message;
  document.body.append(node);
  setTimeout(() => node.remove(), 4500);
}
export function captureModalFocus(trigger = document.activeElement) {
  // A replacing dialog cannot return focus to a control inside the removed one.
  const parent = trigger?.closest?.('dialog');
  const target = parent ? parent.returnFocus : trigger;
  return target && typeof target.focus === 'function' ? target : null;
}
export function modal(title, body, drawer = false, {returnFocus} = {}) {
  // Capture before disposal or an await can turn the original focus into body.
  const before = captureModalFocus(returnFocus || document.activeElement);
  document.querySelectorAll('dialog').forEach(old => old.dispose?.({restoreFocus:false}));
  const dialog = document.createElement('dialog');
  const lifecycle = new AbortController();
  let timer, closing = false, removed = false;
  dialog.lifecycle = lifecycle.signal;
  dialog.returnFocus = before;
  dialog.className = 'sheet' + (drawer ? ' drawer' : '');
  dialog.setAttribute('aria-label', title);
  dialog.innerHTML = `<button class="close" aria-label="关闭">${icon('close')}</button><h2>${escape(title)}</h2><div class="sheet-content">${body}</div>`;
  const finish = ({restoreFocus=true}={}) => {
    if (removed) return;
    removed = true;clearTimeout(timer);lifecycle.abort();
    dialog.close();dialog.remove();
    const target = dialog.returnFocus;
    if (restoreFocus && target?.isConnected && !target.matches(':disabled,[hidden]') &&
        target.getClientRects().length && !document.querySelector('dialog[open]')) {
      target.focus({preventScroll:true});
    }
  };
  dialog.dispose = finish;
  dialog.closeSheet = ({force=false}={}) => {
    if (closing || removed) return true;
    if (!force && dialog.beforeClose?.() === false) return false;
    closing = true;lifecycle.abort();dialog.openSelect?.close();
    if (motionReduced()) finish();
    else {dialog.classList.add('is-closing');timer = setTimeout(finish, 150);}
    return true;
  };
  dialog.querySelector('.close').onclick = () => dialog.closeSheet();
  dialog.addEventListener('cancel', event => {
    event.preventDefault();
    if (dialog.openSelect) {dialog.openSelect.close();return;}
    dialog.closeSheet();
  });
  for (const select of dialog.querySelectorAll('select')) customSelect(select, dialog);
  document.body.append(dialog);dialog.showModal();
  return dialog;
}
const pendingActions = new WeakSet();
export function bind(root, actions) {
  root.querySelectorAll('[data-action]').forEach(node => {
    if (!actions[node.dataset.action]) return;
    node.onclick = async event => {
      if (pendingActions.has(node) || node.disabled || node.getAttribute('aria-disabled') === 'true') {
        event?.preventDefault();return;
      }
      // Native disabled blurs a focused opener. Keep it focusable, but reject
      // every repeated click/keyboard activation until this exact action settles.
      const disabled = node.getAttribute('aria-disabled'), busy = node.getAttribute('aria-busy');
      node.focus({preventScroll:true});
      pendingActions.add(node);
      node.setAttribute('aria-disabled', 'true');
      node.setAttribute('aria-busy', 'true');
      try {
        await actions[node.dataset.action](node);
      } catch (error) {
        toast(error.message);
      } finally {
        pendingActions.delete(node);
        disabled === null ? node.removeAttribute('aria-disabled') : node.setAttribute('aria-disabled', disabled);
        busy === null ? node.removeAttribute('aria-busy') : node.setAttribute('aria-busy', busy);
      }
    };
  });
}
export function safeLink(url, label) {
  try {
    if (!['http:', 'https:'].includes(new URL(url).protocol)) return '';
  } catch {
    return '';
  }
  return `<a href="${escape(url)}" target="_blank" rel="noopener noreferrer">${escape(label)}</a>`;
}
export const taskStatuses = {
  queued: '等待处理',
  running: '进行中',
  succeeded: '已完成',
  failed: '失败'
};
