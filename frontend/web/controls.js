let selectSequence = 0;
function customSelect(select, dialog) {
  const label = select.getAttribute('aria-label') || [...(select.closest('label')?.childNodes || [])].filter(n => n.nodeType === 3).map(n => n.textContent.trim()).join('') || '选择选项';
  const id = 'select-menu-' + ++selectSequence,
    wrap = document.createElement('div');
  wrap.className = 'select-field custom-select';
  select.replaceWith(wrap);
  wrap.append(select);
  select.hidden = true;
  select.tabIndex = -1;
  select.setAttribute('aria-hidden', 'true');
  select.removeAttribute('aria-label');
  const trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.className = 'select-trigger';
  trigger.setAttribute('role', 'combobox');
  trigger.setAttribute('aria-label', label);
  trigger.setAttribute('aria-haspopup', 'listbox');
  trigger.setAttribute('aria-controls', id);
  trigger.setAttribute('aria-expanded', 'false');
  trigger.disabled = select.disabled || !select.options.length;
  const value = document.createElement('span');
  trigger.append(value);
  trigger.insertAdjacentHTML('beforeend', icon('down'));
  const menu = document.createElement('div');
  menu.id = id;
  menu.className = 'select-menu';
  menu.setAttribute('role', 'listbox');
  menu.setAttribute('aria-label', label + '选项');
  menu.hidden = true;
  let active = Math.max(0, select.selectedIndex),
    buffer = '',
    typedAt = 0;
  const options = [...select.options].map((option, i) => {
    const row = document.createElement('div');
    row.className = 'select-option';
    row.id = id + '-' + i;
    row.setAttribute('role', 'option');
    row.setAttribute('aria-disabled', String(option.disabled));
    const text = document.createElement('span');
    text.textContent = option.label;
    row.append(text);
    row.insertAdjacentHTML('beforeend', '<svg class="select-check" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12l4 4L19 6"/></svg>');
    row.onpointerdown = e => e.preventDefault();
    row.onclick = () => {
      if (!option.disabled) choose(i);
    };
    row.onpointermove = () => {
      if (!option.disabled) {
        active = i;
        highlight(false);
      }
    };
    menu.append(row);
    return row;
  });
  wrap.append(trigger, menu);
  function sync() {
    value.textContent = select.selectedOptions[0]?.label || '请选择';
    options.forEach((r, i) => r.setAttribute('aria-selected', String(i === select.selectedIndex)));
  }
  function highlight(scroll = true) {
    options.forEach((r, i) => r.classList.toggle('is-active', i === active));
    trigger.setAttribute('aria-activedescendant', options[active]?.id || '');
    if (scroll) options[active]?.scrollIntoView({
      block: 'nearest'
    });
  }
  function close() {
    menu.hidden = true;
    trigger.setAttribute('aria-expanded', 'false');
    trigger.removeAttribute('aria-activedescendant');
    wrap.classList.remove('is-open');
    if (dialog.openSelect?.trigger === trigger) dialog.openSelect = null;
    buffer = '';
  }
  function open() {
    if (trigger.disabled) return;
    dialog.openSelect?.close();
    active = Math.max(0, select.selectedIndex);
    menu.style.maxHeight = '';
    menu.hidden = false;
    wrap.classList.add('is-open');
    trigger.setAttribute('aria-expanded', 'true');
    dialog.openSelect = {
      close,
      trigger
    };
    // A settings footer stays outside its scroll viewport; menus must fit the
    // visible field area, not extend behind that fixed action strip.
    const viewport = (trigger.closest('.settings-scroll') || dialog).getBoundingClientRect();
    const bottom = viewport.bottom - 12, rect = trigger.getBoundingClientRect();
    const below = Math.max(0, bottom - rect.bottom - 8), above = Math.max(0, rect.top - viewport.top - 12);
    const opensUp = below < menu.offsetHeight && above > below;
    wrap.classList.toggle('opens-up', opensUp);
    menu.style.maxHeight = Math.min(228, opensUp ? above : below) + 'px';
    highlight();
  }
  function choose(i) {
    if (!select.options[i] || select.options[i].disabled) return;
    select.selectedIndex = i;
    sync();
    close();
    select.dispatchEvent(new Event('input', {
      bubbles: true
    }));
    select.dispatchEvent(new Event('change', {
      bubbles: true
    }));
    trigger.focus();
  }
  function move(delta) {
    let next = active;
    for (let count = 0; count < options.length; count++) {
      next = (next + delta + options.length) % options.length;
      if (!select.options[next].disabled) {
        active = next;
        break;
      }
    }
    highlight();
  }
  trigger.onclick = () => menu.hidden ? open() : close();
  trigger.onkeydown = e => {
    if (e.key === 'Escape' && !menu.hidden) {
      e.preventDefault();
      e.stopPropagation();
      close();
      return;
    }
    if (e.key === 'Tab') {
      close();
      return;
    }
    if (['ArrowDown', 'ArrowUp'].includes(e.key)) {
      e.preventDefault();
      if (menu.hidden) open();
      move(e.key === 'ArrowDown' ? 1 : -1);
      return;
    }
    if (['Enter', ' '].includes(e.key)) {
      e.preventDefault();
      menu.hidden ? open() : choose(active);
      return;
    }
    if (['Home', 'End'].includes(e.key)) {
      e.preventDefault();
      if (menu.hidden) open();
      const enabled = options.map((_, i) => i).filter(i => !select.options[i].disabled);
      active = e.key === 'Home' ? enabled[0] : enabled.at(-1);
      highlight();
      return;
    }
    if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      if (menu.hidden) open();
      buffer = Date.now() - typedAt < 700 ? buffer + e.key : e.key;
      typedAt = Date.now();
      const index = [...select.options].findIndex(o => !o.disabled && o.label.toLowerCase().startsWith(buffer.toLowerCase()));
      if (index >= 0) {
        active = index;
        highlight();
      }
    }
  };
  dialog.addEventListener('pointerdown', e => {
    if (!wrap.contains(e.target)) close();
  });
  dialog.addEventListener('focusin', e => {
    if (!wrap.contains(e.target)) close();
  });
  select.addEventListener('change', sync);
  sync();
}
function icon(name) {
  const paths = {
    close: 'M6 6l12 12M18 6L6 18',
    search: 'M21 21l-4.35-4.35M19 11a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
    down: 'M6 9l6 6 6-6',
    back: 'M19 12H5M12 19l-7-7 7-7',
    external: 'M7 17 17 7M7 7h10v10'
  };
  return `<svg class="ui-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="${paths[name] || paths.down}"/></svg>`;
}
export { customSelect, icon };
