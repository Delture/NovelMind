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
    forward: 'M5 12h14M13 6l6 6-6 6',
    chevron: 'M9 6l6 6-6 6',
    external: 'M7 17 17 7M7 7h10v10',
    more: 'M5 11.2a.8.8 0 1 0 0 1.6.8.8 0 0 0 0-1.6zM12 11.2a.8.8 0 1 0 0 1.6.8.8 0 0 0 0-1.6zM19 11.2a.8.8 0 1 0 0 1.6.8.8 0 0 0 0-1.6z',
    library: 'M4 4.5h4v15H4zM10 4.5h4v15h-4zM16.3 5.8l3.9-1 3 14.2-3.9 1z',
    reader: 'M12 6.5C10.3 5 8 4.5 4.5 4.5v13c3.5 0 5.8.5 7.5 2 1.7-1.5 4-2 7.5-2v-13c-3.5 0-5.8.5-7.5 2zM12 6.5v13',
    summary: 'M9.5 6h10.5M9.5 12h10.5M9.5 18h10.5M4 6h2.5M4 12h2.5M4 18h2.5',
    characters: 'M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM2.5 19.5c0-3.3 2.9-5.5 6.5-5.5s6.5 2.2 6.5 5.5M16 4.3a3.5 3.5 0 0 1 0 6.4M18 14.3c2.2.7 3.5 2.4 3.5 5.2',
    ask: 'M20 11.5a8 8 0 0 1-11.7 7.1L4 20l1.1-3.9A8 8 0 1 1 20 11.5zM9 10.5h6M9 13.5h3.5',
    report: 'M7 3.5h7l4.5 4.5v11.5a1 1 0 0 1-1 1h-10.5a1 1 0 0 1-1-1v-15a1 1 0 0 1 1-1zM14 3.5V8h4.5M9 13h6M9 16.5h6',
    tasks: 'M9.5 6H20M9.5 12H20M9.5 18H20M3.5 6l1.2 1.2L6.8 5M3.5 12l1.2 1.2L6.8 11M3.5 18l1.2 1.2L6.8 17',
    settings: 'M4 7h8M16 7h4M4 17h4M12 17h8M14 4.5v5M10 14.5v5'
  };
  return `<svg class="ui-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="${paths[name] || paths.down}"/></svg>`;
}
export { customSelect, icon };
