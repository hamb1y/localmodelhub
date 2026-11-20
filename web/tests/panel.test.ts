import { expect, test } from 'bun:test';
import { chromium } from 'playwright-core';
import { resolve, sep } from 'node:path';
import type { Action, Status } from '../src/lib/api';

test('static panel uses explicit, separate lifecycle and catalog operations', async () => {
  const now = 1_795_230_000;
  const status: Status = {
    models: [
      { id: 'alice', name: 'Alice', profiles: [{ id: 'gpu', device: 'gpu', state: 'ready', port: 8123 }] },
      { id: 'bob', name: 'Bob', profiles: [{ id: 'cpu', device: 'cpu', state: 'stopped' }], default_profile: 'cpu' },
      { id: 'cara', name: 'Cara', profiles: [{ id: 'gpu', device: 'gpu', state: 'ready' }] }
    ],
    jobs: [{ id: 'job-1', action: 'start', model: 'alice', profile: 'gpu', state: 'done', message: 'Ready', source: 'boot', created_at: now, updated_at: now + 1 }],
    hardware: { cpu: 'Fixture CPU', ram_available_bytes: 4096, ram_total_bytes: 8192, gpus: [
      { id: 'test-gpu', name: 'Fixture GPU', total_bytes: 8192, free_bytes: 1000 }
    ] },
    boot: { state: 'complete', message: 'Reconciled' }
  };
  const actions: Action[] = [];
  const stages: unknown[] = [];
  const validated = { models: {}, boot: [], server_added: true };
  const build = resolve(import.meta.dir, '../build');
  let requireAuth = false;
  const server = Bun.serve({
    port: 0,
    hostname: '127.0.0.1',
    async fetch(request) {
      const { pathname, searchParams } = new URL(request.url);
      if (pathname.startsWith('/api/') && requireAuth && request.headers.get('Authorization') !== 'Bearer test-only-token') {
        return Response.json({ error: 'Bearer token required' }, { status: 401 });
      }
      switch (pathname) {
        case '/api/status': return Response.json(status);
        case '/api/stats': return Response.json({
          samples: [
            { timestamp: now, ram_used_bytes: 3000, vram_used_bytes: null },
            { timestamp: now + 60, ram_used_bytes: 4000, vram_used_bytes: 2000 }
          ], jobs_by_model: [{ model: 'alice', finished: 1, failed: 0, start_seconds: 4.2 }],
          metrics: { requests: null, tokens: 25 },
          profiles: [{
            model: 'alice', profile: 'gpu', timestamp: now + 60, prompt_tokens: 20, generation_tokens: 5, tokens: 25,
            samples: [
              { timestamp: now, prompt_tokens: 10, generation_tokens: 2, prompt_tokens_per_second: 3, generation_tokens_per_second: 1 },
              { timestamp: now + 60, prompt_tokens: 10, generation_tokens: 3, prompt_tokens_per_second: 2, generation_tokens_per_second: 1 }
            ]
          }]
        });
        case '/api/logs': return Response.json({ lines: [`${searchParams.get('model')}/${searchParams.get('profile')} fixture line`] });
        case '/api/actions':
          actions.push(await request.json() as Action);
          return Response.json({}, { status: 202 });
        case '/api/catalog/preview':
          expect(await request.json()).toEqual({ catalog: { models: {} } });
          return Response.json({ catalog: validated, summary: { models: 0 } });
        case '/api/catalog/stage':
          stages.push(await request.json());
          status.catalog_staged = true;
          return Response.json({ state: 'staged', message: 'Fixture catalog staged' });
        default: {
          const asset = resolve(build, `.${pathname === '/' ? '/index.html' : pathname}`);
          if (!asset.startsWith(`${build}${sep}`)) return new Response(null, { status: 404 });
          const file = Bun.file(asset);
          return await file.exists() ? new Response(file) : new Response(null, { status: 404 });
        }
      }
    }
  });
  try {
    const browser = await chromium.launch({ executablePath: process.env.CHROMIUM ?? Bun.which('chromium') ?? undefined, args: ['--no-sandbox'] });
    try {
      const page = await browser.newPage({ viewport: { width: 360, height: 740 } });
      page.setDefaultTimeout(4000);
      const errors: Error[] = [];
      page.on('pageerror', (error) => errors.push(error));
      await page.goto(server.url.toString());
      await page.getByRole('button', { name: 'Start Bob / cpu' }).click();
      await page.getByText('Start requested for Bob / cpu.', { exact: false }).waitFor();
      expect(actions).toEqual([{ action: 'start', model: 'bob', profile: 'cpu' }]);

      await page.getByRole('button', { name: 'Switch to Bob / cpu' }).click();
      const dialog = page.getByRole('dialog', { name: 'Switch model profile' });
      expect(await dialog.getByRole('button', { name: 'Confirm switch' }).isDisabled()).toBe(true);
      await dialog.getByLabel('Stop this running profile').selectOption({ label: 'Cara / gpu · gpu' });
      expect(actions.length).toBe(1);
      await dialog.getByRole('button', { name: 'Confirm switch' }).click();
      await dialog.waitFor({ state: 'hidden' });
      expect(actions[1]).toEqual({ action: 'switch', model: 'bob', profile: 'cpu', from_model: 'cara', from_profile: 'gpu' });

      await page.getByRole('button', { name: 'Stop Alice / gpu' }).click();
      await page.getByText('Stop requested for Alice / gpu.', { exact: false }).waitFor();
      expect(actions[2]).toEqual({ action: 'stop', model: 'alice', profile: 'gpu' });
      await page.getByRole('button', { name: 'Logs for Alice / gpu' }).click();
      await page.getByText('alice/gpu fixture line').waitFor();
      await page.keyboard.press('Escape');
      await page.getByRole('dialog').waitFor({ state: 'hidden' });

      await page.getByRole('link', { name: 'Activity' }).click();
      await page.getByRole('heading', { name: 'Start · Alice' }).waitFor();
      await page.getByLabel('Filter by action').selectOption('switch');
      await page.getByText('No matching jobs').waitFor();
      await page.getByRole('link', { name: 'Stats' }).click();
      await page.getByRole('heading', { name: 'Memory history' }).waitFor();
      await page.getByText('Fixture GPU').waitFor();
      await page.getByRole('heading', { name: 'Token usage' }).waitFor();
      await page.getByRole('img', { name: /Token throughput.*peak 3 tok\/s/ }).waitFor();
      await page.getByText('Unreported', { exact: true }).waitFor();

      await page.getByRole('link', { name: 'Catalog import' }).click();
      await page.getByRole('tab', { name: 'JSON' }).click();
      await page.getByLabel('Catalog JSON', { exact: true }).fill('{"models":{}}');
      expect(await page.getByRole('button', { name: 'Stage for next boot' }).count()).toBe(0);
      await page.getByRole('button', { name: 'Preview catalog' }).click();
      await page.getByRole('heading', { name: 'Preview result' }).waitFor().catch(async (error) => {
        throw new Error(`${error.message}\nPanel: ${await page.locator('main').innerText()}\nBrowser errors: ${errors.join(', ')}`);
      });
      expect(stages).toEqual([]);
      await page.getByRole('button', { name: 'Stage for next boot' }).click();
      await page.getByText('Fixture catalog staged').waitFor();
      expect(stages).toEqual([{ catalog: validated }]);
      expect(actions.length).toBe(3);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(360);
      await page.getByRole('link', { name: 'Models' }).click();
      await page.getByText('Catalog staged for next boot', { exact: true }).waitFor();
      await page.getByRole('button', { name: 'Start Bob / cpu' }).waitFor();

      requireAuth = true;
      await page.reload();
      await page.getByText('Bearer token required', { exact: false }).waitFor();
      await page.getByRole('link', { name: 'Settings' }).click();
      await page.getByLabel('Token', { exact: true }).fill('test-only-token');
      await page.getByRole('button', { name: 'Use token' }).click();
      await page.getByText('Connected', { exact: true }).waitFor();
      expect(errors).toEqual([]);
    } finally {
      await browser.close();
    }
  } finally {
    server.stop(true);
  }
}, 20000);
