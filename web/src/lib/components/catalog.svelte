<script lang="ts">
  import { CircleAlert, CircleCheck, FileUp, Link2, ListChecks, Upload } from 'lucide-svelte';
  import { Tabs } from 'bits-ui';
  import { api, errorMessage, type Preview } from '$lib/api';
  import Button from './ui/button.svelte';

  let { token, alreadyStaged, onStage }: { token: string; alreadyStaged?: boolean; onStage: () => void } = $props();
  let mode = $state('url');
  let url = $state('');
  let text = $state('');
  let filename = $state('');
  let preview = $state<Preview>();
  let pending = $state(false);
  let staging = $state(false);
  let error = $state('');
  let staged = $state('');
  const tabClass = 'inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium text-muted hover:text-foreground data-[state=active]:bg-hover data-[state=active]:text-foreground';

  function clearResult() { preview = undefined; staged = ''; error = ''; }

  async function pickFile(event: Event) {
    const file = (event.currentTarget as HTMLInputElement).files?.[0];
    if (!file) return;
    clearResult();
    try {
      text = await file.text();
      filename = file.name;
    } catch (e) { error = errorMessage(e); }
  }

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    if (pending || staging) return;
    clearResult();
    pending = true;
    try {
      if (mode === 'url') {
        const parsed = new URL(url.trim());
        if (parsed.protocol !== 'https:' || parsed.port) throw new Error('Enter an HTTPS catalog URL on port 443.');
        preview = await api(token).preview({ url: url.trim() });
      } else {
        let payload: unknown;
        try { payload = JSON.parse(text); } catch { throw new Error('The catalog must contain valid JSON.'); }
        if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error('The catalog must be a JSON object.');
        preview = await api(token).preview({ catalog: payload as Record<string, unknown> });
      }
      if (!preview || preview.catalog == null) throw new Error('The response did not contain a catalog.');
    } catch (e) { preview = undefined; error = errorMessage(e); }
    finally { pending = false; }
  }

  async function stage() {
    if (!preview || pending || staging || staged) return;
    staging = true;
    error = '';
    try {
      const reply = await api(token).stage(preview.catalog);
      if (reply?.state !== 'staged') throw new Error('The service did not confirm staging.');
      staged = reply.message || 'Catalog staged for next boot.';
      onStage();
    } catch (e) { error = errorMessage(e); }
    finally { staging = false; }
  }
</script>

<div class="grid gap-3 xl:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)]">
  <section class="panel p-4 sm:p-5">
    <div class="mb-4 flex items-center gap-3">
      <div class="grid size-9 place-items-center rounded-lg border border-accent/15 bg-accent/8 text-accent"><FileUp size={17} /></div>
      <div><h2 class="text-sm font-semibold">Catalog source</h2><p class="mt-0.5 text-xs text-muted">Use a URL or provide a JSON catalog.</p></div>
    </div>
    <form onsubmit={submit}>
      <Tabs.Root bind:value={mode} onValueChange={clearResult}>
        <Tabs.List aria-label="Catalog source" class="mb-4 inline-flex gap-1 rounded-lg border border-line bg-background p-1">
          <Tabs.Trigger value="url" disabled={pending || staging} class={tabClass}><Link2 size={15} /> URL</Tabs.Trigger>
          <Tabs.Trigger value="json" disabled={pending || staging} class={tabClass}><FileUp size={15} /> JSON</Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content value="url">
          <label for="catalog-url" class="mb-1.5 block text-xs font-semibold">Public HTTPS catalog URL</label>
          <input id="catalog-url" type="url" placeholder="https://example.com/catalog.json" class="field" required disabled={mode !== 'url' || pending || staging} bind:value={url} oninput={clearResult} />
        </Tabs.Content>
        <Tabs.Content value="json">
          <label for="catalog-json" class="mb-1.5 block text-xs font-semibold">Catalog JSON</label>
          <textarea id="catalog-json" rows="8" spellcheck={false} placeholder="Paste a JSON catalog…" class="field min-h-32 resize-y font-mono text-xs" required disabled={mode !== 'json' || pending || staging} bind:value={text} oninput={() => { filename = ''; clearResult(); }}></textarea>
          <label class="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted">
            <span class="sr-only">Choose a JSON catalog file</span>
            <input type="file" accept=".json,application/json" disabled={pending || staging} onchange={pickFile} class="max-w-full text-xs file:mr-2 file:rounded-md file:border file:border-line file:bg-raised file:px-2 file:py-1.5 file:text-foreground" />
            {#if filename}<span class="wrap-anywhere">Loaded {filename}</span>{/if}
          </label>
        </Tabs.Content>
      </Tabs.Root>
      <Button type="submit" variant="primary" class="mt-4" disabled={pending || staging}><ListChecks /> {pending ? 'Checking catalog…' : 'Preview catalog'}</Button>
    </form>
  </section>
  <aside class="panel self-start p-4 sm:p-5">
    <h2 class="text-sm font-semibold">Preview, then stage</h2>
    <p class="mt-2 text-sm leading-relaxed text-muted">Preview validates the catalog without changing your current models. Review the result before staging it.</p>
    <p class="mt-2 text-sm leading-relaxed text-muted">Staging replaces the catalog at the next OS boot, not on a service restart. It does not reload the catalog or change any running profiles now. Imported boot actions are not carried over.</p>
    {#if alreadyStaged && !staged}<p class="mt-2 text-sm leading-relaxed text-warning">A catalog is already staged. Staging another replaces that pending catalog.</p>{/if}
  </aside>
</div>

{#if error}<div role="alert" class="notice notice-error mt-3"><CircleAlert class="mt-0.5 size-4 shrink-0" /> <span class="wrap-anywhere">{error}</span></div>{/if}
{#if preview}
  <section class="panel mt-3 p-4 sm:p-5">
    <div class="flex items-center gap-2"><CircleCheck class="size-4 text-accent" /><h2 class="text-sm font-semibold">Preview result</h2></div>
    {#if typeof preview.summary === 'string'}
      <p class="mt-2 text-sm wrap-anywhere text-muted">{preview.summary}</p>
    {:else if preview.summary && typeof preview.summary === 'object' && !Array.isArray(preview.summary)}
      <dl class="mt-3 flex flex-wrap gap-2">
        {#each Object.entries(preview.summary) as [key, count] (key)}
          <div class="rounded-lg border border-line bg-background px-3 py-2">
            <dt class="text-xs text-muted capitalize">{key === 'boot' ? 'Boot actions in source' : key}</dt>
            <dd class="mt-1 text-sm font-semibold">{typeof count === 'number' || typeof count === 'string' ? count : '—'}</dd>
          </div>
        {/each}
      </dl>
    {:else if preview.summary != null}
      <pre class="terminal mt-3">{JSON.stringify(preview.summary, null, 2)}</pre>
    {/if}
    <details class="mt-3 text-sm text-muted">
      <summary class="w-fit cursor-pointer rounded text-foreground">View validated catalog JSON</summary>
      <pre class="terminal mt-2">{JSON.stringify(preview.catalog, null, 2)}</pre>
    </details>
    <div class="mt-4 flex flex-wrap items-center gap-3 border-t border-line pt-4">
      <Button variant="primary" disabled={pending || staging || !!staged} onclick={stage}><Upload /> {staging ? 'Staging…' : 'Stage for next boot'}</Button>
      <p class="text-xs text-muted">No immediate reload or change to running models.</p>
    </div>
    {#if staged}<div role="status" class="notice notice-info mt-3"><CircleCheck class="mt-0.5 size-4 shrink-0" /> <span class="wrap-anywhere">{staged}</span></div>{/if}
  </section>
{/if}
