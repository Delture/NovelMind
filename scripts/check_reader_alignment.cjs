/* Scoped real-reader regression, run through check_web.py --reader-alignment. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const titles = [
  '第1章 雨',
  '第2章 归来',
  '第4章 沿着河岸寻找被风吹散的来信',
  '第5章 灯',
  '第6章 一封迟到了很久的信',
  '第7章 我们再次走过那条安静的旧街',
  '第8章 长标题包含标点，用于检查自然换行',
  '第9章 回声',
  '第10章 路口'
];
const tolerance = 0.75;
const spread = values => Math.max(...values) - Math.min(...values);

async function geometry(page) {
  return page.locator('.r-directory .r-chapter').evaluateAll(rows => rows.map(row => {
    const [number, title] = row.children;
    const text = document.createRange();
    text.selectNodeContents(title);
    const lines = [...text.getClientRects()].filter(rect => rect.width > 0)
      .map(rect => ({ x: rect.x, y: rect.y, right: rect.right }));
    const first = document.createRange();
    first.setStart(title.firstChild, 0);
    first.setEnd(title.firstChild, 1);
    return {
      index: Number(row.dataset.chapter), number: number.textContent, title: title.textContent,
      selected: row.getAttribute('aria-current') === 'true',
      rowRight: row.getBoundingClientRect().right,
      numberX: number.getBoundingClientRect().x,
      numberWidth: number.getBoundingClientRect().width,
      titleX: first.getBoundingClientRect().x, lines
    };
  }));
}

function assertAligned(rows) {
  for (const field of ['numberX', 'numberWidth', 'titleX']) {
    assert(spread(rows.map(row => row[field])) <= tolerance,
      `${field} drift: ${JSON.stringify(rows.map(row => [row.index, row[field]]))}`);
  }
  for (const row of rows) {
    assert(row.titleX > row.numberX + row.numberWidth, 'Number and title must not overlap');
    for (const line of row.lines) {
      assert(Math.abs(line.x - row.titleX) <= tolerance, 'Wrapped line lost its title-column start');
      assert(line.right <= row.rowRight + tolerance, 'Title overflows its row');
    }
  }
}

(async () => {
  const started = Date.now();
  const output = process.env.QA_OUTPUT;
  const url = process.env.QA_URL;
  assert(output && url, 'Run through the isolated check_web.py launcher');
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const context = await browser.newContext({ reducedMotion: 'reduce' });
  const page = await context.newPage();
  const errors = [], checks = [], measurements = [];
  const check = label => { checks.push(label); console.log('PASS', label); };
  page.on('pageerror', error => errors.push(error.message));
  const request = async (route, options = {}) => {
    const response = await context.request.fetch(url + '/api' + route, options);
    assert(response.ok(), `${route}: ${response.status()} ${await response.text()}`);
    return response.json();
  };
  const selectComplete = async index => {
    await page.waitForFunction(({ index, title }) =>
      document.querySelector(`[data-chapter="${index}"]`)?.getAttribute('aria-current') === 'true' &&
      document.querySelector('[data-chapter-title]')?.textContent === title,
    { index, title: titles[index - 1] });
    assert.equal(await page.locator('.r-directory [aria-current=true]').count(), 1);
  };
  const openDirectory = async () => {
    if (!await page.locator('.r-sidebar').isVisible()) {
      await page.getByRole('button', { name: '切换目录', exact: true }).click();
    }
    await page.locator('.r-sidebar').waitFor({ state: 'visible' });
  };
  try {
    await request('/workspace/privacy', { method: 'PUT', data: { decided: true, external: false, rights: false } });
    const content = titles.map((title, index) => `${title}\n这是第${index + 1}条自编验收记录，内容只用于目录布局测试。`).join('\n');
    const uploaded = await request('/novels/upload?filename=reader-alignment-original.txt', {
      method: 'POST', data: Buffer.from(content), headers: { 'content-type': 'application/octet-stream' }
    });
    assert.equal(uploaded.chapters.length, titles.length);
    for (const [label, viewport] of [['desktop', { width: 1440, height: 1000 }], ['narrow', { width: 390, height: 844 }]]) {
      await page.setViewportSize(viewport);
      // A distinct document URL resets selection; navigating to the same hash is a no-op.
      await page.goto(`${url}/?reader-alignment=${label}#page=reader&novel=${uploaded.novel_id}&chapter=1`);
      await selectComplete(1);
      await openDirectory();
      await page.evaluate(() => document.fonts.ready);
      const initial = await geometry(page);
      assert(initial.some(row => row.lines.length === 1));
      assert(initial.some(row => row.lines.length === 2), 'Fixture must actually wrap to two lines');
      assert.deepEqual(initial.map(row => row.title), titles);
      assert.equal(initial[2].number, '03'); // Record order; original heading is deliberately 第4章.
      check(`${label}: 长短与两行标题及原始标题映射`);
      assertAligned(initial);
      check(`${label}: 所有序号列与标题首字对齐，换行不溢出`);

      if (label === 'desktop') {
        // Negative control only in this disposable page, never in production files.
        const oldStyle = await page.addStyleTag({ content: '.r-chapter { justify-content: center !important; }' });
        const centered = await geometry(page);
        assert(spread(centered.map(row => row.numberX)) > 5, 'Fixture must detect the previous centering regression');
        await oldStyle.evaluate(node => node.remove());
        measurements.push({ viewport: label, negativeControl: centered });
        check('临时页面负向对照能捕获旧居中漂移');
      }

      await page.locator('[data-chapter="3"]').click();
      await selectComplete(3);
      await openDirectory();
      await page.mouse.move(0, 0);
      const backgrounds = await page.locator('.r-directory .r-chapter').evaluateAll(rows =>
        [getComputedStyle(rows[2]).backgroundColor, getComputedStyle(rows[0]).backgroundColor]);
      assert.notEqual(backgrounds[0], backgrounds[1], 'Current chapter must remain visibly selected');
      const clicked = await geometry(page);
      assertAligned(clicked);
      assert(clicked.every((row, i) => Math.abs(row.numberX - initial[i].numberX) <= tolerance &&
        Math.abs(row.titleX - initial[i].titleX) <= tolerance));
      check(`${label}: 点击切章、选中态明确且列起点不变`);

      await page.locator('[data-chapter="1"]').focus();
      await page.keyboard.press('Tab');
      assert.equal(await page.locator(':focus').getAttribute('data-chapter'), '2');
      await page.keyboard.press('Enter');
      await selectComplete(2);
      await openDirectory();
      const keyboard = await geometry(page);
      assertAligned(keyboard);
      check(`${label}: 现有Tab/Enter可切章且保持对齐`);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
      check(`${label}: 无页面横向溢出`);
      measurements.push({ viewport: label, initial, clicked, keyboard });
      await page.screenshot({ path: path.join(output, `reader-alignment-${label}.png`), fullPage: true });
    }
    assert.deepEqual(await request('/workspace/tasks'), []);
    assert.deepEqual(errors, []);
    check('零模型任务、零pageerror');
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'reader-alignment-failure.png'), fullPage: true });
    throw error;
  } finally {
    fs.writeFileSync(path.join(output, 'reader-alignment-results.json'), JSON.stringify({
      checks, errors, tolerancePx: tolerance, elapsedSeconds: (Date.now() - started) / 1000, measurements
    }, null, 2));
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
