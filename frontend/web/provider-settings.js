import {api} from './api.js';
import {modal, captureModalFocus, escape as e, toast} from './ui.js';
import {appearanceControls} from './appearance.js';

const roles = ['generation','embedding'];
const labels = {generation:'文本生成', embedding:'文本向量'};
const canonical = value => {try {return new URL(value).href.replace(/\/+$/, '');} catch {return value.trim();}};

export async function openProviderSettings({onPrivacy, trigger}={}) {
  const returnFocus = captureModalFocus(trigger);
  const [initial, presets] = await Promise.all([api('/system/model-settings'), api('/system/provider-presets')]);
  if (trigger && !trigger.isConnected) return;
  let saved = initial, baseline, saving = false, hydrating = false, activeTab = 'generation';
  const probes = new Map();
  const field = role => `<section role="tabpanel" aria-labelledby="settings-tab-${role}" id="settings-${role}" data-setting-panel="${role}" ${role === 'embedding' ? 'hidden' : ''}>
    <div class="provider-heading"><div><h3>${labels[role]}</h3><p class="muted">${role === 'generation' ? '用于章节梳理、人物、回答与报告。' : '用于原文索引和问题向量。'}</p></div><span class="status-chip" data-saved-status="${role}"></span></div>
    <p class="saved-endpoint" data-saved-endpoint="${role}"></p>
    <fieldset data-provider-fields="${role}"><div class="settings-field-grid">
      <label class="wide">服务预设<select name="${role}.preset" aria-label="${labels[role]}服务预设">${presets[role].map(p => `<option value="${e(p.id)}">${e(p.label)}</option>`).join('')}</select></label>
      <input type="hidden" name="${role}.provider" hidden>
      <label class="wide">服务地址<input name="${role}.base_url" aria-label="${labels[role]}服务地址" type="url" maxlength="500" autocomplete="off" spellcheck="false"></label>
      <label class="wide">模型名称<input name="${role}.model" aria-label="${labels[role]}模型名称" maxlength="100" autocomplete="off" spellcheck="false" placeholder="手动输入，或从主动获取的列表选择"></label>
    </div><p class="muted" data-preset-note="${role}"></p>
    <label>API Key<div class="key-input"><input name="${role}.api_key" aria-label="${labels[role]} API Key" type="password" autocomplete="new-password" maxlength="4096" placeholder="已保存密钥不会回填"><button type="button" data-show-key="${role}" aria-pressed="false">显示当前输入</button></div></label>
    <label class="check-label"><input name="${role}.clear_api_key" type="checkbox">清除已保存的密钥</label>
    <p class="key-status" role="status" data-key-status="${role}"></p>
    ${role === 'generation' ? `<div class="settings-field-grid"><label>摘要并发<select name="mode" aria-label="摘要并发"><option value="auto">自动调整</option><option value="manual">手动设置</option></select></label><label>最大并发<input name="concurrency" aria-label="最大并发" type="number" min="1" max="2500"></label></div>` : `<label>向量维度<input name="dimensions" aria-label="向量维度" type="number" min="1" max="8192"></label>`}
    </fieldset>
    <section class="provider-probe"><h4>连接与模型目录</h4><p class="muted">点击下方按钮将连接所填地址，只读取模型目录；不发送小说，不执行生成或向量请求，不保存草稿。</p>
      <div class="settings-actions"><button data-probe="${role}">测试连接</button><button data-models="${role}">获取模型列表</button></div>
      <p role="status" data-probe-status="${role}">尚未连接。服务不提供目录时，仍可手动填写模型。</p><div class="model-choices" data-model-list="${role}"></div></section>
    </section>`;
  const d = modal('模型与设置', `<div class="settings-scroll"><p class="muted">每项服务分别管理。预设只填写接口和地址，模型及密钥保持你的选择。</p>
    <nav class="settings-tabs" role="tablist" aria-label="设置分类">${[...roles,'appearance'].map((role,i) => `<button role="tab" id="settings-tab-${role}" data-setting-tab="${role}" aria-controls="settings-${role}" aria-selected="${i === 0}">${labels[role] || '外观'}</button>`).join('')}</nav>
    <div class="settings-panels">${roles.map(field).join('')}<section role="tabpanel" aria-labelledby="settings-tab-appearance" id="settings-appearance" data-setting-panel="appearance" hidden><div data-appearance></div></section></div>
    <section class="settings-confirm" data-settings-confirm hidden></section></div>
    <p class="error" role="alert" data-settings-error></p>
    <footer class="settings-footer"><span role="status" data-draft-status>已保存配置</span><button data-reset-settings>恢复 .env 配置</button><button data-privacy-settings>隐私与外部处理</button><button class="primary" data-save-settings>保存配置</button></footer>`, false, {returnFocus});
  d.classList.add('settings-workspace');
  appearanceControls(d.querySelector('[data-appearance]'));
  const element = name => d.querySelector(`[name="${name}"]`);
  const value = name => element(name).value.trim();
  const error = d.querySelector('[data-settings-error]');
  const confirmation = d.querySelector('[data-settings-confirm]');

  function payload() {
    const result = {};
    for (const role of roles) result[role] = {
      provider:value(role+'.provider'), base_url:value(role+'.base_url'), model:value(role+'.model'),
      api_key:value(role+'.api_key') || null, clear_api_key:element(role+'.clear_api_key').checked,
    };
    result.generation.summary_concurrency_mode = value('mode');
    result.generation.summary_max_concurrency = Number(value('concurrency'));
    result.embedding.dimensions = Number(value('dimensions'));
    return result;
  }
  const dirty = () => baseline !== JSON.stringify(payload());
  const sameDestination = role => value(role+'.provider') === saved[role].provider && canonical(value(role+'.base_url')) === canonical(saved[role].base_url);

  function cancelProbe(role) {
    const current = probes.get(role);
    if (!current) return;
    current.controller.abort();probes.delete(role);
    d.querySelector(`[data-probe="${role}"]`).disabled = saving;
    d.querySelector(`[data-models="${role}"]`).disabled = saving;
  }
  function updateStatus() {
    if (hydrating || d.lifecycle.aborted) return;
    const changed = dirty();
    d.dataset.dirty = String(changed);
    d.querySelector('[data-draft-status]').textContent = saving ? '正在保存…' : changed ? '未保存更改' : '已保存配置';
    for (const role of roles) {
      const key = value(role+'.api_key'), clear = element(role+'.clear_api_key').checked;
      const state = clear ? (key ? '请在替换和清除之间选择一种操作。' : '保存时清除密钥，保留地址和模型。') : key ? '保存时替换为当前输入的密钥。' : !sameDestination(role) ? '目的地已改变：请输入此服务的新密钥，或明确勾选清除。' : saved[role].api_key_set ? '留空保留同地址已保存密钥；旧密钥不会回显。' : '此角色尚无密钥。';
      d.querySelector(`[data-key-status="${role}"]`).textContent = state;
      d.querySelector(`[data-preset-note="${role}"]`).textContent = role === 'embedding' && value(role+'.provider') === 'siliconflow' ? '硅基流动预设要求 Qwen/Qwen3-Embedding-4B、1024维；切换预设不会自动改写模型或维度，请手动核对。' : '模型名称可手动填写。地址变更不会自动携带另一地址的已保存密钥。';
    }
  }
  function populate() {
    hydrating = true;
    for (const role of roles) {
      for (const key of ['provider','base_url','model']) element(role+'.'+key).value = saved[role][key];
      element(role+'.api_key').value = '';element(role+'.api_key').type = 'password';
      element(role+'.clear_api_key').checked = false;
      const show = d.querySelector(`[data-show-key="${role}"]`);show.textContent = '显示当前输入';show.setAttribute('aria-pressed','false');
      const preset = presets[role].find(p => p.id !== 'custom' && p.provider === saved[role].provider && canonical(p.base_url) === canonical(saved[role].base_url));
      element(role+'.preset').value = preset?.id || 'custom';element(role+'.preset').dispatchEvent(new Event('change'));
      d.querySelector(`[data-saved-status="${role}"]`).textContent = saved[role].configured ? '已保存 · 已配置' : '已保存 · 未配置';
      d.querySelector(`[data-saved-endpoint="${role}"]`).textContent = `当前保存：${saved[role].base_url} · ${saved[role].model}`;
    }
    element('mode').value = saved.generation.summary_concurrency_mode;element('mode').dispatchEvent(new Event('change'));
    element('concurrency').value = saved.generation.summary_max_concurrency;element('dimensions').value = saved.embedding.dimensions;
    baseline = JSON.stringify(payload());hydrating = false;updateStatus();
  }
  function setSaving(value) {
    saving = value;
    d.querySelectorAll('fieldset, [data-save-settings], [data-reset-settings], [data-probe], [data-models], [data-setting-tab]').forEach(node => node.disabled = value);
    updateStatus();
  }
  function validate() {
    error.textContent = '';
    for (const role of roles) {
      if (!value(role+'.model') || !value(role+'.base_url')) {selectTab(role);throw new Error('请填写模型名称与服务地址。');}
      const address = new URL(value(role+'.base_url'));
      if (!['http:','https:'].includes(address.protocol) || address.username || address.password || address.search || address.hash)
        throw new Error('服务地址须为不含凭据、查询参数或片段的 HTTP(S) 根地址。');
      if (element(role+'.clear_api_key').checked && value(role+'.api_key')) throw new Error('密钥不能同时替换和清除。');
      if (!sameDestination(role) && !value(role+'.api_key') && !element(role+'.clear_api_key').checked)
        throw new Error(`${labels[role]}目的地已改变，请填写新密钥或明确清除。`);
    }
    for (const name of ['concurrency','dimensions']) {
      const input = element(name), number = Number(input.value);
      if (!Number.isInteger(number) || number < Number(input.min) || number > Number(input.max))
        throw new Error(name === 'concurrency' ? '并发数应为1至2500的整数。' : '向量维度应为1至8192的整数。');
    }
  }
  async function save(after) {
    if (saving) return;
    try {
      validate();roles.forEach(cancelProbe);setSaving(true);
      const result = await api('/system/model-settings', {method:'PUT',json:payload()});
      if (d.lifecycle.aborted) return;
      saved = result;populate();confirmation.hidden = true;toast('配置已保存，后续任务使用新设置');
      if (after) {d.closeSheet({force:true});after();}
    } catch (reason) {if (!d.lifecycle.aborted) error.textContent = reason.message;}
    finally {if (!d.lifecycle.aborted) setSaving(false);}
  }
  function askToDiscard(after = () => {}) {
    confirmation.hidden = false;
    confirmation.innerHTML = '<p>模型配置有未保存更改。关闭前可以继续编辑、放弃草稿，或保存后关闭。</p><div class="settings-actions"><button data-keep>继续编辑</button><button data-discard>放弃更改</button><button class="primary" data-save-close>保存并关闭</button></div>';
    confirmation.querySelector('[data-keep]').onclick = () => {confirmation.hidden = true;};
    confirmation.querySelector('[data-discard]').onclick = () => {d.closeSheet({force:true});after();};
    confirmation.querySelector('[data-save-close]').onclick = () => save(after);
    confirmation.querySelector('[data-keep]').focus();
    confirmation.scrollIntoView({block:'nearest'});
  }
  function selectTab(role) {
    activeTab = role;
    for (const r of roles) {
      if (probes.has(r)) d.querySelector(`[data-probe-status="${r}"]`).textContent = '已取消上一项连接，可主动重试。';
      cancelProbe(r);
    }
    d.querySelectorAll('[data-setting-tab]').forEach(button => {
      button.setAttribute('aria-selected',String(button.dataset.settingTab === role));
      button.tabIndex = button.dataset.settingTab === role ? 0 : -1;
    });
    d.querySelectorAll('[data-setting-panel]').forEach(panel => panel.hidden = panel.dataset.settingPanel !== role);
    for (const r of roles) {
      element(r+'.api_key').type = 'password';
      const show = d.querySelector(`[data-show-key="${r}"]`);show.textContent = '显示当前输入';show.setAttribute('aria-pressed','false');
    }
  }
  async function probe(role, showModels) {
    cancelProbe(role);error.textContent = '';
    const controller = new AbortController(), pending = {controller};probes.set(role,pending);
    const status = d.querySelector(`[data-probe-status="${role}"]`), list = d.querySelector(`[data-model-list="${role}"]`);
    status.textContent = '正在连接所填服务的模型目录…';list.replaceChildren();
    d.querySelector(`[data-probe="${role}"]`).disabled = true;d.querySelector(`[data-models="${role}"]`).disabled = true;
    try {
      const result = await api('/system/model-probe', {method:'POST',signal:controller.signal,json:{
        role,provider:value(role+'.provider'),base_url:value(role+'.base_url'),
        api_key:value(role+'.api_key') || null,use_saved_key:!element(role+'.clear_api_key').checked,confirm_connection:true,
      }});
      if (d.lifecycle.aborted || probes.get(role) !== pending || activeTab !== role) return;
      status.textContent = `${result.message} (${result.latency_ms} ms)${result.truncated ? ' 列表已达到显示上限。' : ''}`;
      if (result.status === 'success' && showModels) {
        const restricted = role === 'embedding' && value(role+'.provider') === 'siliconflow';
        const choices = restricted ? result.models.filter(model => model.id === 'Qwen/Qwen3-Embedding-4B') : result.models;
        for (const model of choices) {
          const button = document.createElement('button');button.type = 'button';
          button.textContent = model.id + (restricted ? ' · 1024维' : '');
          button.onclick = () => {
            element(role+'.model').value = model.id;
            if (restricted) element('dimensions').value = 1024;
            updateStatus();toast(restricted ? '已填写所选模型与1024维，尚未保存' : '已填写模型名称，尚未保存');
          };
          list.append(button);
        }
        if (restricted) {
          const note = document.createElement('p');note.className = 'muted';
          note.textContent = choices.length ? '此预设仅提供上述模型，点击将填写模型与标明的1024维。其他模型请用自定义兼容服务并核对维度。' : '目录中没有当前预设支持的模型。该预设仅允许 Qwen/Qwen3-Embedding-4B / 1024维；其他模型请使用自定义兼容服务手动配置。';
          list.append(note);
        } else if (!choices.length) list.textContent = '模型目录为空，仍可手动填写。';
      }
    } catch (reason) {
      if (reason.name !== 'AbortError' && !d.lifecycle.aborted && probes.get(role) === pending) status.textContent = reason.message;
    } finally {if (probes.get(role) === pending) cancelProbe(role);}
  }
  for (const role of roles) {
    element(role+'.preset').addEventListener('change', () => {
      if (hydrating) return;
      const selected = presets[role].find(p => p.id === value(role+'.preset'));
      element(role+'.provider').value = selected.provider;
      if (selected.base_url) element(role+'.base_url').value = selected.base_url;
      updateStatus();
    });
    d.querySelector(`[data-show-key="${role}"]`).onclick = event => {
      const input = element(role+'.api_key'), visible = input.type === 'password';input.type = visible ? 'text' : 'password';
      event.currentTarget.textContent = visible ? '隐藏当前输入' : '显示当前输入';event.currentTarget.setAttribute('aria-pressed',String(visible));
    };
    d.querySelector(`[data-probe="${role}"]`).onclick = () => probe(role,false);
    d.querySelector(`[data-models="${role}"]`).onclick = () => probe(role,true);
  }
  d.addEventListener('input', event => {
    if (hydrating || !event.target.name) return;
    const role = event.target.name.split('.')[0];
    if (roles.includes(role)) {
      cancelProbe(role);d.querySelector(`[data-model-list="${role}"]`).replaceChildren();
      d.querySelector(`[data-probe-status="${role}"]`).textContent = '草稿已变化，需主动重新连接目录。';
    }
    updateStatus();
  });
  d.addEventListener('change', updateStatus);
  for (const button of d.querySelectorAll('[data-setting-tab]')) {
    button.onclick = () => selectTab(button.dataset.settingTab);
    button.onkeydown = event => {
      if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
      event.preventDefault();
      const order = [...roles,'appearance'];let index = order.indexOf(activeTab);
      index = event.key === 'Home' ? 0 : event.key === 'End' ? 2 : (index + (event.key === 'ArrowRight' ? 1 : -1) + 3) % 3;
      selectTab(order[index]);d.querySelector(`[data-setting-tab="${order[index]}"]`).focus();
    };
  }
  d.beforeClose = () => {
    if (saving) {error.textContent = '正在保存，请稍候。';return false;}
    if (dirty()) {askToDiscard();return false;}
    return true;
  };
  d.querySelector('[data-save-settings]').onclick = () => save();
  d.querySelector('[data-privacy-settings]').onclick = () => {if (saving) return;if (dirty()) askToDiscard(onPrivacy);else onPrivacy?.();};
  d.querySelector('[data-reset-settings]').onclick = () => {
    confirmation.hidden = false;
    confirmation.innerHTML = '<p>恢复后会删除页面保存的生成与向量配置（包括密钥覆盖），重新使用 .env 默认值，并放弃当前草稿。只影响后续任务；运行中的任务继续使用原快照。</p><div class="settings-actions"><button data-reset-cancel>取消</button><button data-reset-confirm>确认恢复 .env</button></div>';
    confirmation.querySelector('[data-reset-cancel]').onclick = () => {confirmation.hidden = true;};
    confirmation.querySelector('[data-reset-confirm]').onclick = async () => {
      if (saving) return;
      roles.forEach(cancelProbe);setSaving(true);
      try {
        const result = await api('/system/model-settings', {method:'DELETE'});
        if (d.lifecycle.aborted) return;
        saved = result;populate();confirmation.hidden = true;error.textContent = '';toast('已恢复 .env 配置');
      } catch (reason) {if (!d.lifecycle.aborted) error.textContent = reason.message;}
      finally {if (!d.lifecycle.aborted) setSaving(false);}
    };
    confirmation.querySelector('[data-reset-cancel]').focus();
  };
  addEventListener('beforeunload', event => {if (dirty()) {event.preventDefault();event.returnValue = '';}}, {signal:d.lifecycle});
  d.lifecycle.addEventListener('abort', () => {roles.forEach(cancelProbe);for (const role of roles) element(role+'.api_key').value = '';}, {once:true});
  populate();selectTab(activeTab);
  return d;
}
