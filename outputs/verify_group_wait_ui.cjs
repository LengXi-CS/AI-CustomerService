// Serve the built UI and mock all admin APIs. Never writes deployed configuration.
const { chromium } = require('C:/Users/THINK/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const root = path.resolve('src/fastgpt_assistant/server/admin_static');
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const relative = decodeURIComponent(url.pathname.replace(/^\/admin\/?/, '')) || 'index.html';
    const file = path.resolve(root, relative);
    if (!file.startsWith(root + path.sep)) { res.writeHead(404).end(); return; }
    fs.readFile(file, (error, bytes) => {
      if (error) { res.writeHead(404).end(); return; }
      res.setHeader('content-type', { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png', '.jpg': 'image/jpeg' }[path.extname(file)] || 'application/octet-stream');
      res.end(bytes);
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({ channel: 'msedge', headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const names = ['客户协作群 A', '客服演示群', '产品咨询群'];
    let revision = 'a'.repeat(64);
    let groups = names.map((name, index) => ({ group_id: `${100 + index}@chatroom`, group_name: name,
      mode: 'active', require_customer_mention: false, allow_images: true, allow_files: true,
      owner_id: 'owner', customer_service_ids: ['staff'], bot_aliases: ['客服助手'],
      reply_to_staff_when_mentioned: true, native_quote_replies: true, associate_customer_demands: true,
      conversation_quiet_window_seconds: index === 2 ? 300 : null }));
    const before = structuredClone(groups);
    const writes = [];
    await page.route('**/api/admin/**', async route => {
      const req = route.request();
      const pathname = new URL(req.url()).pathname;
      let data;
      if (pathname.endsWith('/session')) data = { configured: true, authenticated: true, csrf_token: 'test-only' };
      else if (pathname.endsWith('/groups')) {
        if (req.method() === 'PUT') {
          const input = req.postDataJSON();
          assert.equal(input.revision, revision);
          assert.equal(input.global_enabled, true);
          writes.push(structuredClone(input.groups));
          groups = input.groups;
          revision = writes.length.toString().repeat(64);
          data = { revision, changed: true };
        } else data = { revision, groups, global_enabled: true, apply_mode: 'next_policy_read' };
      } else if (pathname.endsWith('/reply-policy/soothe')) data = { revision,
        values: { soothe_mode: 'model', soothe_fixed_reply: '收到，我们加急处理' }, apply_mode: 'next_policy_read' };
      else if (pathname.endsWith('/reply-policy/runtime')) {
        const values = { conversation_quiet_window_seconds: 1800, conversation_poll_interval_seconds: 30,
          attachment_dispatch_interval_seconds: 5, routing_normal_confidence_threshold: .8,
          routing_human_confidence_threshold: .5, owner_notify_enabled: true };
        data = { revision: 'f'.repeat(64), values, active_values: values,
          fields: Object.fromEntries(Object.keys(values).map(key => [key, { editable: true,
            pending_restart: false, source: 'dotenv', status: 'effective', source_key: key }])),
          apply_mode: 'restart', restart_required: false };
      } else throw new Error(`Unexpected mock API ${pathname}`);
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/admin/#reply`);
    await page.getByRole('heading', { name: '各群客户发言后等待时间' }).waitFor();
    const card = page.locator('.policy-card').filter({ has: page.getByRole('heading', { name: '各群客户发言后等待时间' }) });
    await card.getByText('1800 秒').first().waitFor();
    const first = card.locator('.group-wait-row').first();
    await first.getByText('跟随默认', { exact: true }).click();
    await first.getByRole('spinbutton').fill('0');
    await first.getByRole('spinbutton').press('Tab');
    await card.getByRole('button', { name: '查看并保存等待时间' }).click();
    await page.getByRole('button', { name: '确认保存 1 个群', exact: true }).click();
    await page.getByText('各群等待时间已保存，下次调度读取时生效，无需重启。', { exact: true }).waitFor();
    assert.equal(writes[0][0].conversation_quiet_window_seconds, 0);
    assert.deepEqual(writes[0].slice(1), before.slice(1));
    assert.deepEqual({ ...writes[0][0], conversation_quiet_window_seconds: null }, before[0]);
    assert.doesNotMatch(await card.innerText(), /瀚思|handsin|杭州|GYS00673/i);
    await card.screenshot({ path: 'outputs/group-waits-desktop.png' });
    await first.getByText('跟随默认', { exact: true }).click();
    await card.getByRole('button', { name: '查看并保存等待时间' }).click();
    await page.getByRole('button', { name: '确认保存 1 个群', exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll('.group-wait-row input[role=spinbutton]').length === 1);
    assert.equal(writes[1][0].conversation_quiet_window_seconds, null);
    await page.getByRole('dialog', { name: '确认各群等待时间', exact: true }).waitFor({ state: 'hidden' });
    await page.locator('.el-message').last().waitFor({ state: 'hidden' });
    await page.setViewportSize({ width: 390, height: 844 });
    await card.scrollIntoViewIfNeeded();
    assert.doesNotMatch(await card.innerText(), /瀚思|handsin|杭州|GYS00673/i);
    await card.screenshot({ path: 'outputs/group-waits-mobile.png' });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    assert.equal(overflow, false, 'Mobile page must not overflow horizontally');
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ writes: writes.length, zero_wait_saved: true, inherit_restored: true,
      unrelated_groups_preserved: true, mobile_no_overflow: true, page_errors: errors }));
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
