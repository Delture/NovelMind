import {customSelect} from './controls.js';
// 仅保存非秘密界面偏好；系统的减少动态/透明度和高对比设置优先。
export const appearanceKey = 'novelmind.appearance.v1';
const defaults = {theme:'system', reduceMotion:false, reduceTransparency:false};
const media = {
  dark: matchMedia('(prefers-color-scheme: dark)'),
  motion: matchMedia('(prefers-reduced-motion: reduce)'),
  transparency: matchMedia('(prefers-reduced-transparency: reduce)'),
  contrast: matchMedia('(prefers-contrast: more)'),
};
let preference = {...defaults}, pendingFrame = 0, litSurface, point;
try {
  const saved = JSON.parse(localStorage.getItem(appearanceKey) || '{}');
  preference = {
    theme:['system','light','dark'].includes(saved.theme) ? saved.theme : 'system',
    reduceMotion:saved.reduceMotion === true,
    reduceTransparency:saved.reduceTransparency === true,
  };
} catch {}

export function getAppearance() { return {...preference}; }
export function motionReduced() { return preference.reduceMotion || media.motion.matches; }

function clearLight() {
  cancelAnimationFrame(pendingFrame);
  pendingFrame = 0;
  litSurface?.classList.remove('glass-lit');
  litSurface = null;
}

function apply() {
  const root = document.documentElement;
  root.dataset.uiTheme = preference.theme === 'system' ? (media.dark.matches ? 'dark' : 'light') : preference.theme;
  root.dataset.reducedMotion = String(motionReduced());
  root.dataset.reducedTransparency = String(preference.reduceTransparency || media.transparency.matches || media.contrast.matches);
  root.dataset.highContrast = String(media.contrast.matches);
  if (motionReduced() || root.dataset.reducedTransparency === 'true') clearLight();
  if (motionReduced()) {
    document.getAnimations?.().forEach(animation => animation.cancel());
  }
  dispatchEvent(new CustomEvent('novelmind-appearance-change', {detail:getAppearance()}));
}

export function setAppearance(update) {
  preference = {...preference, ...update};
  if (!['system','light','dark'].includes(preference.theme)) preference.theme = 'system';
  preference.reduceMotion = preference.reduceMotion === true;
  preference.reduceTransparency = preference.reduceTransparency === true;
  try { localStorage.setItem(appearanceKey, JSON.stringify(preference)); } catch {}
  apply();
}

export function animateEnter(element) {
  if (motionReduced() || !element.animate) return;
  element.getAnimations().forEach(animation => animation.cancel());
  element.animate([{opacity:.4, transform:'translateY(5px)'}, {opacity:1, transform:'none'}],
                  {duration:180, easing:'cubic-bezier(.2,.75,.25,1)'});
}

export function appearanceControls(container) {
  const value = getAppearance();
  container.innerHTML = `<div class="appearance-controls"><h3>外观与舒适度</h3>
    <label>界面主题<select aria-label="界面主题" data-appearance-theme>
      <option value="system">跟随系统</option><option value="light">暖纸浅色</option><option value="dark">深色</option>
    </select></label>
    <label class="check-label"><input type="checkbox" data-reduce-motion>降低动态效果</label>
    <label class="check-label"><input type="checkbox" data-reduce-transparency>降低透明度</label>
    <p class="muted">立即生效并保存在此浏览器。系统的减少动态、减少透明度和高对比设置始终优先；阅读纸面继续使用阅读器主题。</p></div>`;
  const theme = container.querySelector('[data-appearance-theme]');
  theme.value = value.theme;
  theme.onchange = () => setAppearance({theme:theme.value});
  const dialog = container.closest('dialog');
  if (dialog) customSelect(theme, dialog);
  for (const [selector, field] of [['[data-reduce-motion]','reduceMotion'],['[data-reduce-transparency]','reduceTransparency']]) {
    const input = container.querySelector(selector);
    input.checked = value[field];
    input.onchange = () => setAppearance({[field]:input.checked});
  }
}

for (const value of Object.values(media)) value.addEventListener('change', apply);
addEventListener('storage', event => {
  if (event.key !== appearanceKey) return;
  try {
    const saved = JSON.parse(event.newValue || '{}');
    preference = {...defaults, theme:['system','light','dark'].includes(saved.theme) ? saved.theme : 'system',
                  reduceMotion:saved.reduceMotion === true, reduceTransparency:saved.reduceTransparency === true};
    apply();
  } catch {}
});
document.addEventListener('pointermove', event => {
  if (motionReduced() || document.documentElement.dataset.reducedTransparency === 'true' || document.hidden || event.pointerType === 'touch') return;
  const surface = event.target.closest?.('.r-dock,.sheet,.select-menu');
  if (surface !== litSurface) {
    clearLight();
    litSurface = surface;
    surface?.classList.add('glass-lit');
  }
  if (!surface) return;
  point = {x:event.clientX, y:event.clientY};
  if (!pendingFrame) pendingFrame = requestAnimationFrame(() => {
    pendingFrame = 0;
    if (!litSurface?.isConnected || motionReduced()) return clearLight();
    const rect = litSurface.getBoundingClientRect();
    litSurface.style.setProperty('--light-x', `${point.x - rect.left}px`);
    litSurface.style.setProperty('--light-y', `${point.y - rect.top}px`);
  });
}, {passive:true});
document.addEventListener('pointerleave', clearLight);
document.addEventListener('visibilitychange', () => {if (document.hidden) clearLight();});
addEventListener('pagehide', clearLight);
apply();
