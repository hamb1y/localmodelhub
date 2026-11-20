<script lang="ts">
  import { Activity, Cpu, HardDrive, MemoryStick } from 'lucide-svelte';
  import type { Hardware, InferenceProfile, Model, Stats } from '$lib/api';
  import { formatBytes, formatDate, memoryPercent } from '$lib/model-state';
  import TrendChart from './ui/trend-chart.svelte';

  let { hardware, stats, models, loading = false, error = '' }: { hardware?: Hardware; stats?: Stats; models: Model[]; loading?: boolean; error?: string } = $props();
  const label = (key: string) => key.replace(/[_-]+/g, ' ').replace(/^./, (c) => c.toUpperCase());
  let samples = $derived(stats?.samples ?? []);
  let profiles = $derived(stats?.profiles ?? []);
  let selection = $state('');
  const profileKey = (profile: InferenceProfile) => JSON.stringify([profile.model, profile.profile]);
  const modelName = (model: string) => models.find((item) => item.id === model)?.name ?? model;
  let inference = $derived(profiles.find((item) => profileKey(item) === selection) ?? profiles[0]);
  let metrics = $derived(stats?.metrics ?? Object.fromEntries(Object.entries(stats ?? {}).filter(([key]) => !['samples', 'jobs_by_model', 'profiles'].includes(key))));
  const tokenNumber = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
  const formatCount = (value: number) => tokenNumber.format(value);
  const formatRate = (value: number) => `${formatCount(value)} tok/s`;

  function value(key: string, data: unknown) {
    if (data == null) return 'Unreported';
    if (typeof data === 'number') return /(?:^|_)bytes$/.test(key) ? formatBytes(data) : new Intl.NumberFormat().format(data);
    if (typeof data === 'boolean') return data ? 'Yes' : 'No';
    if (typeof data === 'object') return JSON.stringify(data, null, 2);
    return String(data);
  }

</script>

{#snippet capacity(title: string, total: number | null, free: number | null)}
  {@const used = memoryPercent(total, free)}
  <div class="mt-4">
    {#if used !== undefined}
      <div role="meter" aria-label={`${title} used`} aria-valuenow={used} aria-valuetext={`${used}% used`} aria-valuemin={0} aria-valuemax={100} class="h-1.5 overflow-hidden rounded-full bg-background">
        <div class="h-full rounded-full bg-accent/75" style:width={`${used}%`}></div>
      </div>
    {/if}
    <div class="mt-2 flex flex-wrap justify-between gap-x-2 gap-y-0.5 text-xs text-muted">
      <span>{formatBytes(free)} available</span><span>{formatBytes(total)} total</span>
    </div>
  </div>
{/snippet}

<div class="grid gap-3 sm:grid-cols-2">
  <section class="panel p-4 sm:p-5">
    <Cpu class="mb-3 size-5 text-accent" />
    <h2 class="eyebrow">Processor</h2>
    <p class="mt-1.5 text-base font-semibold wrap-anywhere">{hardware?.cpu || 'Not reported'}</p>
  </section>
  <section class="panel p-4 sm:p-5">
    <MemoryStick class="mb-3 size-5 text-accent" />
    <h2 class="eyebrow">System memory</h2>
    {#if hardware}
      <p class="mt-1.5 text-base font-semibold">{formatBytes(hardware.ram_available_bytes)} free</p>
      {@render capacity('System memory', hardware.ram_total_bytes, hardware.ram_available_bytes)}
    {:else}
      <p class="mt-1.5 text-base font-semibold">Not reported</p>
    {/if}
  </section>
</div>

<section class="panel mt-3 overflow-hidden">
  <header class="flex items-center gap-2 border-b border-line px-4 py-3 sm:px-5">
    <HardDrive class="size-4 text-accent" />
    <h2 class="text-sm font-semibold">GPUs</h2>
    {#if hardware}<span class="ml-auto text-xs text-faint">{hardware.gpus.length} detected</span>{/if}
  </header>
  {#each hardware?.gpus ?? [] as gpu (gpu.id)}
    <div class="border-b border-line px-4 py-4 last:border-b-0 sm:px-5">
      <h3 class="text-sm font-medium wrap-anywhere">{gpu.name}</h3>
      <p class="mt-0.5 font-mono text-[11px] wrap-anywhere text-faint">{gpu.id}</p>
      {@render capacity(gpu.name, gpu.total_bytes, gpu.free_bytes)}
    </div>
  {:else}
    <p class="px-4 py-5 text-sm text-muted sm:px-5">{hardware ? 'No GPUs reported by the host.' : 'GPU data unavailable.'}</p>
  {/each}
</section>

{#if stats?.profiles}
  <section class="panel mt-3 p-4 sm:p-5">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 class="text-sm font-semibold">Token usage</h2>
        <p class="mt-1 text-xs text-faint">Observed totals and throughput over the last 6 hours</p>
      </div>
      {#if inference}
        <select aria-label="Inference profile" class="field h-8 w-auto max-w-full py-0 text-xs" value={profileKey(inference)} onchange={(event) => selection = event.currentTarget.value}>
          {#each profiles as profile (profileKey(profile))}
            <option value={profileKey(profile)}>{modelName(profile.model)} / {profile.profile}</option>
          {/each}
        </select>
      {/if}
    </div>
    {#if inference}
      <dl class="mt-4 grid gap-2 sm:grid-cols-3">
        {#each [['Total tokens', inference.tokens], ['Prompt tokens', inference.prompt_tokens], ['Generated tokens', inference.generation_tokens]] as item (item[0])}
          <div class="rounded-lg border border-line bg-background px-3 py-2.5">
            <dt class="text-xs text-faint">{item[0]}</dt>
            <dd class="mt-1 text-sm font-semibold">{typeof item[1] === 'number' ? formatCount(item[1]) : 'Unreported'}</dd>
          </div>
        {/each}
      </dl>
      <TrendChart title="Token throughput" formatValue={formatRate} gap={120} series={[
        { label: 'Prompt tokens/s', color: 'var(--color-accent)', points: inference.samples.map((sample) => ({ timestamp: sample.timestamp, value: sample.prompt_tokens_per_second })) },
        { label: 'Generated tokens/s', color: 'var(--color-warning)', points: inference.samples.map((sample) => ({ timestamp: sample.timestamp, value: sample.generation_tokens_per_second })) }
      ]} />
      <p class="mt-3 text-xs text-faint">Last sampled {formatDate(inference.timestamp)}. Totals count tokens observed while metrics were collected.</p>
    {:else}
      <p class="mt-3 text-sm text-muted">No profile has reported token metrics yet.</p>
    {/if}
  </section>
{/if}

{#if stats?.samples}
  <section class="panel mt-3 p-4 sm:p-5">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <h2 class="text-sm font-semibold">Memory history</h2>
      <p class="text-xs text-faint">Up to 6 hours</p>
    </div>
    <TrendChart title="Memory used" formatValue={formatBytes} series={[
      { label: 'RAM used', color: 'var(--color-accent)', points: samples.map((sample) => ({ timestamp: sample.timestamp, value: sample.ram_used_bytes })) },
      { label: 'VRAM used', color: 'var(--color-warning)', points: samples.map((sample) => ({ timestamp: sample.timestamp, value: sample.vram_used_bytes })) }
    ]} />
  </section>
{/if}

{#if stats?.jobs_by_model}
  <section class="panel mt-3 overflow-hidden">
    <header class="flex items-center justify-between gap-2 border-b border-line px-4 py-3 sm:px-5">
      <h2 class="text-sm font-semibold">Jobs by model</h2>
      <span class="text-xs text-faint">Last 24 hours</span>
    </header>
    {#each stats.jobs_by_model as job (job.model)}
      <div class="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-3 last:border-b-0 sm:px-5">
        <span class="text-sm font-medium wrap-anywhere">{modelName(job.model)}</span>
        <span class="text-xs text-muted">{job.finished} finished · {job.failed} failed{job.start_seconds == null ? '' : ` · ${job.start_seconds.toFixed(1)} s average startup`}</span>
      </div>
    {:else}
      <p class="px-4 py-5 text-sm text-muted sm:px-5">No finished jobs during this period.</p>
    {/each}
  </section>
{/if}

<section class="panel mt-3 overflow-hidden">
  <header class="flex items-center gap-2 border-b border-line px-4 py-3 sm:px-5">
    <Activity class="size-4 text-accent" />
    <h2 class="text-sm font-semibold">Service metrics</h2>
  </header>
  {#if Object.keys(metrics).length}
    <dl class="grid gap-px bg-line sm:grid-cols-2">
      {#each Object.entries(metrics) as [key, entry] (key)}
        <div class="min-w-0 bg-panel p-4 sm:px-5">
          <dt class="text-xs wrap-anywhere text-muted">{label(key)}</dt>
          {#if entry && typeof entry === 'object'}
            <dd><pre class="terminal mt-2">{value(key, entry)}</pre></dd>
          {:else}
            <dd class="mt-1.5 text-sm font-semibold wrap-anywhere">{value(key, entry)}</dd>
          {/if}
        </div>
      {/each}
    </dl>
  {:else}
    <p class="px-4 py-5 text-sm text-muted sm:px-5">{error || (loading ? 'Reading metrics…' : 'The host has not published additional metrics.')}</p>
  {/if}
</section>
