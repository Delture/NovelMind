import {openProviderSettings} from './provider-settings.js';
import { api } from './api.js';
import { modal, captureModalFocus, escape as e, bind } from './ui.js';
export async function privacy(trigger) {
  const returnFocus = captureModalFocus(trigger);
  const saved = await api('/workspace/privacy');
  const settings = await api('/system/model-settings');
  const d = modal('隐私与外部处理', `<p>阅读、导入和书签在本机完成。使用 AI 分析时，以下内容会发送给所选服务，也可能产生费用。</p>
    <p>生成服务：${e(settings.generation.provider_label)} · ${e(settings.generation.model)}<br>${e(settings.generation.base_url)}</p>
    <p>向量服务：${e(settings.embedding.provider_label)} · ${e(settings.embedding.model)}<br>${e(settings.embedding.base_url)}</p>
    <p class="muted">摘要和人物分析发送章节标题及正文；索引发送原文片段；问答发送问题和检索片段；报告发送标题、派生摘要及可选人物结果。具体供应商条款和本次范围会在开始处理前展示。</p>
    <label class="check-label"><input type="checkbox" name="external" ${saved.external ? 'checked' : ''}>我允许主动发起 AI 操作时使用上述外部服务处理内容。</label>
    <label class="check-label"><input type="checkbox" name="rights" ${saved.rights ? 'checked' : ''}>我确认对所处理内容具有相应权利。</label>
    <p class="muted">选择保存到本机。你可以随时在设置中修改；保存选择不会发起分析。</p>
    <footer><button data-action="local">仅本地阅读</button><button class="primary" data-action="save">保存选择</button></footer>`, false, {returnFocus});
  return new Promise(resolve => {
    let resolved = false;
    const close = d.closeSheet;
    d.closeSheet = () => {
      close();
      if (!resolved) resolve(false);
    };
    const save = async local => {
      const value = {
        decided: true,
        external: !local && d.querySelector('[name=external]').checked,
        rights: !local && d.querySelector('[name=rights]').checked
      };
      await api('/workspace/privacy', {
        method: 'PUT',
        json: value
      });
      resolved = true;
      d.closeSheet();
      resolve(value.external && value.rights);
    };
    bind(d, {
      local: () => save(true),
      save: () => save(false)
    });
  });
}
export async function consent(trigger) {
  const returnFocus = captureModalFocus(trigger);
  const choice = await api('/workspace/privacy');
  if (choice.external && choice.rights) return true;
  return privacy(returnFocus);
}
export const consentFields = () => ({
  confirm_external_processing: true,
  confirm_content_rights: true
});
export async function openSettings(trigger) {
  return openProviderSettings({onPrivacy:privacy, trigger});
}
