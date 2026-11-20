<script lang="ts">
  import { Activity, ArrowLeftRight, Play, Square } from 'lucide-svelte';
  import type { Job, Model } from '$lib/api';
  import { formatDate, stateLabel, tone } from '$lib/model-state';
  import Badge from './ui/badge.svelte';

  let { jobs, models, ready }: { jobs: Job[]; models: Model[]; ready: boolean } = $props();
  let filter = $state('all');
  const actionIcons = { start: Play, stop: Square, switch: ArrowLeftRight };
  function timestamp(value: Job['updated_at']) {
    if (typeof value === 'number') return Math.abs(value) < 1e12 ? value * 1000 : value;
    return Date.parse(value) || 0;
  }
  const visible = $derived(jobs.filter((job) => filter === 'all' || job.action === filter)
    .sort((a, b) => timestamp(b.updated_at) - timestamp(a.updated_at)));
</script>

<section class="panel overflow-hidden">
  <header class="flex flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-3 sm:px-5">
    <div>
      <h2 class="text-sm font-semibold">Job history</h2>
      <p class="mt-0.5 text-xs text-faint">{ready ? `${visible.length} ${visible.length === 1 ? 'job' : 'jobs'}` : 'Waiting for the service'}</p>
    </div>
    <select aria-label="Filter by action" bind:value={filter} class="field h-8 w-auto py-0 text-xs">
      <option value="all">All actions</option>
      <option value="start">Start</option>
      <option value="stop">Stop</option>
      <option value="switch">Switch</option>
    </select>
  </header>
  {#each visible as job (job.id)}
    {@const Icon = actionIcons[job.action as keyof typeof actionIcons] ?? Activity}
    <article class="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-2 border-b border-line px-4 py-3 last:border-b-0 sm:grid-cols-[auto_minmax(0,1fr)_auto] sm:px-5">
      <div class="grid size-8 place-items-center rounded-lg border border-line bg-raised text-accent"><Icon size={15} /></div>
      <div class="min-w-0">
        <div class="flex flex-wrap items-center gap-2">
          <h3 class="text-sm font-medium wrap-anywhere">{stateLabel(job.action)} · {models.find((model) => model.id === job.model)?.name ?? job.model}</h3>
          <Badge dot tone={tone(job.state)}>{stateLabel(job.state)}</Badge>
        </div>
        <p class="mt-1 font-mono text-[11px] wrap-anywhere text-faint">{job.profile} · {job.id}{job.source ? ` · ${job.source}` : ''}</p>
        {#if job.message}<p class="mt-1 text-xs wrap-anywhere text-muted">{job.message}</p>{/if}
      </div>
      <div class="col-start-2 text-xs text-faint sm:col-start-auto sm:text-right">
        <p title={`Last updated ${formatDate(job.updated_at)}`}>{formatDate(job.updated_at ?? job.created_at)}</p>
        {#if job.created_at !== job.updated_at}<p class="mt-1 text-[11px]">Created {formatDate(job.created_at)}</p>{/if}
      </div>
    </article>
  {:else}
    <div class="flex min-h-56 flex-col items-center justify-center px-5 text-center">
      <Activity class="mb-3 size-6 text-faint" />
      <h3 class="text-sm font-semibold">{!ready ? 'Activity unavailable' : jobs.length ? 'No matching jobs' : 'No activity yet'}</h3>
      <p class="mt-1 text-sm text-muted">{!ready ? 'Connect to the service to see job history.' : jobs.length ? 'Try a different action filter.' : 'Start, stop, and switch jobs will appear here.'}</p>
    </div>
  {/each}
</section>
