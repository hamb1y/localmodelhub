<script lang="ts">
  import { ArrowLeftRight, Box, Copy, Cpu, FileUp, Layers3, Play, Search, Square, Terminal, X } from 'lucide-svelte';
  import type { Action, Model, Target } from '$lib/api';
  import { canStart, canStop, isRunning, stateLabel, tone } from '$lib/model-state';
  import Badge from './ui/badge.svelte';
  import Button from './ui/button.svelte';

  let { models, ready, locked, loading, hasActive, inferenceUrl, onAction, onSwitch, onLogs, onImport }: {
    models: Model[]; ready: boolean; locked: boolean; loading: boolean; hasActive: boolean;
    inferenceUrl?: string;
    onAction: (action: Action) => void; onSwitch: (target: Target) => void;
    onLogs: (target: Target) => void; onImport: () => void;
  } = $props();

  let query = $state('');
  let onlyRunning = $state(false);
  let copyResult = $state('');
  async function copyEndpoint() {
    const url = inferenceUrl ?? 'http://127.0.0.1:5101/v1';
    try {
      await navigator.clipboard.writeText(url);
      copyResult = `Copied ${url}`;
    } catch {
      copyResult = url;
    }
  }
  let visible = $derived(models.filter((model) => {
    const search = query.trim().toLowerCase();
    return (!onlyRunning || model.profiles.some((p) => isRunning(p.state))) && (
      !search || [model.name, model.id, ...model.profiles.flatMap((p) => [p.id, p.device])].some((part) => part.toLowerCase().includes(search))
    );
  }));
</script>

{#if copyResult}<p class="mb-2 text-xs text-accent" role="status">{copyResult}</p>{/if}
<div class="mb-4 flex flex-wrap items-center justify-between gap-3">
  <div class="relative w-full sm:max-w-xs">
    <Search class="pointer-events-none absolute top-2.5 left-3 size-4 text-faint" />
    <input type="search" class="field h-9 pr-9 pl-9 [&::-webkit-search-cancel-button]:hidden" placeholder="Search models or profiles…" aria-label="Search models or profiles" bind:value={query} />
    {#if query}
      <button type="button" class="absolute top-1.5 right-2 grid size-6 place-items-center rounded text-muted hover:text-foreground" aria-label="Clear search" onclick={() => query = ''}><X size={14}/></button>
    {/if}
  </div>
  <div class="flex items-center gap-3">
    <Button size="sm" variant={onlyRunning ? 'secondary' : 'ghost'} aria-pressed={onlyRunning} onclick={() => onlyRunning = !onlyRunning}>
      <span class={onlyRunning ? 'size-1.5 rounded-full bg-accent' : 'size-1.5 rounded-full bg-faint'}></span> Running only
    </Button>
    {#if ready}<span class="text-xs text-faint">{visible.length} {visible.length === 1 ? 'model' : 'models'}</span>{/if}
  </div>
</div>

{#if !visible.length}
  <div class="panel flex min-h-60 flex-col items-center justify-center px-6 py-10 text-center">
    <div class="mb-3 grid size-11 place-items-center rounded-xl border border-line bg-raised text-accent"><Box size={21} /></div>
    <h2 class="font-semibold">{!ready ? (loading ? 'Connecting to LocalModelHub…' : 'Model status unavailable') : models.length ? 'No matching models' : 'Your model library is empty'}</h2>
    <p class="mt-1 max-w-sm text-sm text-muted">
      {!ready ? 'Model profiles appear when the local service responds.' : models.length ? 'Try another search or clear your filters.' : 'Preview a catalog to check its models and profiles before installing it on the host.'}
    </p>
    {#if ready && !models.length}
      <Button class="mt-4" size="sm" onclick={onImport}><FileUp /> Preview catalog</Button>
    {/if}
  </div>
{:else}
  <div class="space-y-3">
    {#each visible as model (model.id)}
      <article class="panel overflow-hidden">
        <header class="flex items-center gap-3 border-b border-line px-4 py-3.5 sm:px-5">
          <div class="grid size-9 shrink-0 place-items-center rounded-lg border border-accent/15 bg-accent/8 text-accent"><Layers3 size={17} /></div>
          <div class="min-w-0 flex-1">
            <h2 class="wrap-anywhere text-sm font-semibold">{model.name}</h2>
            <p class="mt-0.5 font-mono text-[11px] wrap-anywhere text-faint">{model.id}</p>
            {#if model.io?.length}
              <p class="mt-1 text-xs text-muted">{model.io.map((pair) => `${pair.input.join(' + ')} → ${pair.output}`).join(' · ')}</p>
            {/if}
            {#if model.description}<p class="mt-1 text-xs wrap-anywhere text-muted">{model.description}</p>{/if}
          </div>
          <span class="shrink-0 text-xs text-faint">{model.profiles.length} {model.profiles.length === 1 ? 'profile' : 'profiles'}</span>
        </header>
        {#each model.profiles as profile (profile.id)}
          {@const target = { model: model.id, profile: profile.id }}
          <div class="grid gap-2.5 border-b border-line px-4 py-3 last:border-b-0 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:px-5">
            <div class="min-w-0">
              <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span class="font-mono text-xs font-semibold wrap-anywhere">{profile.id}</span>
                <Badge dot tone={tone(profile.state)}>{stateLabel(profile.state)}</Badge>
                 {#if profile.id === model.default_profile}<span class="text-[11px] text-faint">Default</span>{/if}
                 {#if profile.start_on_boot}<span class="text-[11px] text-faint">Starts at boot</span>{/if}
              </div>
              <div class="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted">
                <span class="inline-flex items-center gap-1.5 wrap-anywhere"><Cpu size={13} class="shrink-0 text-faint" /> {profile.device}</span>
                {#if profile.message}<span class="wrap-anywhere text-faint">{profile.message}</span>{/if}
              </div>
            </div>
            <div class="flex flex-wrap items-center gap-1.5">
               <Button size="sm" variant="ghost" aria-label={`Logs for ${model.name} / ${profile.id}`} onclick={() => onLogs(target)}><Terminal /> Logs</Button>
               {#if profile.state === 'ready'}
                 <Button size="sm" variant="ghost" aria-label={`Copy API URL for ${model.name} / ${profile.id}`} onclick={copyEndpoint}><Copy /> API URL</Button>
               {/if}
               {#if profile.controllable === false}
                 <span class="px-2 text-xs text-faint">Read-only · runtime not approved for control</span>
               {:else if canStop(profile.state)}
                <Button size="sm" variant="danger" disabled={locked} aria-label={`Stop ${model.name} / ${profile.id}`} onclick={() => onAction({ action: 'stop', ...target })}><Square /> Stop</Button>
              {:else if canStart(profile.state)}
                {#if hasActive}
                  <Button size="sm" disabled={locked} aria-label={`Switch to ${model.name} / ${profile.id}`} onclick={() => onSwitch(target)}><ArrowLeftRight /> Switch</Button>
                {/if}
                <Button size="sm" variant="primary" disabled={locked} aria-label={`Start ${model.name} / ${profile.id}`} title="Start without stopping any running profile" onclick={() => onAction({ action: 'start', ...target })}><Play /> Start</Button>
              {/if}
            </div>
          </div>
        {:else}
          <p class="px-5 py-3 text-sm text-faint">No profiles configured.</p>
        {/each}
      </article>
    {/each}
  </div>
{/if}
