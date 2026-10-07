/* Shared setup for isolated feature suites; no persistent browser profile. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

async function eventually(read, message, timeout = 7000) {
  const end = Date.now() + timeout;
  let last;
  while (Date.now() < end) {
    try { last = await read(); if (last) return last; } catch (error) { last = error.message; }
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error(`${message}: ${JSON.stringify(last)}`);
}

async function runSuite(name, scenario, options = {}) {
  const url = process.env.QA_URL, output = process.env.QA_OUTPUT;
  assert(url && output && process.env.NOVELMIND_QA === 'isolated', 'Use scripts/check_web.py');
  const browser = await chromium.launch({channel:'msedge', headless:true});
  const context = await browser.newContext({viewport:{width:1440,height:1000}, colorScheme:'light', ...options});
  const page = await context.newPage();
  const checks = [], errors = [], screenshots = [], details = {};
  let failure = null;
  const started = Date.now();
  page.setDefaultTimeout(10000);
  page.on('pageerror', error => errors.push(error.message));
  await context.route('**/*', route => {
    const target = new URL(route.request().url());
    if (['http:','https:'].includes(target.protocol) && target.origin !== new URL(url).origin) {
      errors.push('Unexpected external browser request: ' + target.origin);
      return route.abort();
    }
    return route.continue();
  });
  const api = async (route, request = {}) => {
    const response = await context.request.fetch(url + '/api' + route, request);
    assert(response.ok(), `${route}: ${response.status()} ${await response.text()}`);
    return response.status() === 204 ? null : response.json();
  };
  const shot = async label => {
    const file = `${name}-${label}.png`;
    await page.screenshot({path:path.join(output,file),fullPage:true});
    screenshots.push(file);
  };
  const check = label => { checks.push(label); console.log('PASS',name,label); };
  const select = async (scope, label, choice) => {
    await scope.getByRole('combobox',{name:label,exact:true}).click();
    await page.getByRole('option',{name:choice,exact:true}).click();
  };
  const upload = (name, text) => api('/novels/upload?filename='+encodeURIComponent(name),{
    method:'POST',data:Buffer.from(text),headers:{'content-type':'application/octet-stream'}
  });
  try {
    await api('/qa/probe-records',{method:'POST'}); // Fail before mutations against a non-QA server.
    await api('/workspace/privacy',{method:'PUT',data:{decided:true,external:false,rights:true}});
    await scenario({page,context,browser,url,output,api,shot,check,select,upload,eventually,details,assert});
    assert.deepEqual(errors,[]);
  } catch (error) {
    failure = {message:error.message,stack:error.stack};
    await shot('failure').catch(() => {});
    throw error;
  } finally {
    fs.writeFileSync(path.join(output,name+'-results.json'),JSON.stringify({
      passed:!failure, checks, errors, failure, elapsedSeconds:(Date.now()-started)/1000,screenshots,details
    },null,2));
    await browser.close();
  }
}
module.exports = {runSuite,eventually};
