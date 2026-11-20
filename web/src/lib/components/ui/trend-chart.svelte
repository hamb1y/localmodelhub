<script lang="ts">
  import { formatDate } from '$lib/model-state';

  type Point = { timestamp: number; value: number | null | undefined };
  type Series = { label: string; color: string; points: Point[] };
  let { title, series, formatValue, gap = 300 }: {
    title: string; series: Series[]; formatValue: (value: number) => string; gap?: number;
  } = $props();
  const valid = (p: Point) => Number.isFinite(p.timestamp) && p.value != null && Number.isFinite(p.value) && p.value >= 0;
  let visible = $derived(series.filter((entry) => entry.points.some(valid)));
  let points = $derived(visible.flatMap((entry) => entry.points).filter(valid));
  let start = $derived(Math.min(...points.map((point) => point.timestamp)));
  let end = $derived(Math.max(...points.map((point) => point.timestamp)));
  let peak = $derived(Math.max(0, ...points.map((point) => point.value ?? 0)));
  let lines = $derived(visible.map((entry) => ({ ...entry, segments: segments(entry.points, start, end, peak, gap) })));

  function segments(data: Point[], first: number, last: number, max: number, maxGap: number) {
    const result: { x: number; y: number }[][] = [];
    const duration = last - first;
    let group: { x: number; y: number }[] = [];
    let previous: number | undefined;
    for (const point of data.filter((item) => Number.isFinite(item.timestamp)).sort((a, b) => a.timestamp - b.timestamp)) {
      if (!valid(point) || (previous !== undefined && point.timestamp - previous > maxGap)) {
        if (group.length) result.push(group);
        group = [];
      }
      if (valid(point)) {
        group.push({
          x: duration ? 6 + (point.timestamp - first) / duration * 588 : 300,
          y: 113 - (point.value ?? 0) / Math.max(max, 1) * 106
        });
      }
      previous = point.timestamp;
    }
    if (group.length) result.push(group);
    return result;
  }
</script>

{#if lines.length}
  <div class="mt-3 flex flex-wrap gap-3 text-xs text-muted">
    {#each lines as line (line.label)}
      <span class="inline-flex items-center gap-1.5"><span class="size-2 rounded-full" style:background-color={line.color}></span> {line.label}</span>
    {/each}
    <span class="sm:ml-auto">Peak {formatValue(peak)}</span>
  </div>
  <div class="mt-3 rounded-lg border border-line bg-background px-2 py-1.5">
    <svg viewBox="0 0 600 120" preserveAspectRatio="none" class="h-26 w-full" role="img" aria-label={`${title}, from ${formatDate(start)} to ${formatDate(end)}; peak ${formatValue(peak)}`}>
      {#each [7, 42, 78, 113] as y (y)}
        <line x1="6" x2="594" y1={y} y2={y} stroke="var(--color-line)" vector-effect="non-scaling-stroke" />
      {/each}
      {#each lines as line (line.label)}
        {#each line.segments as segment, index (index)}
          {#if segment.length === 1}
            <circle cx={segment[0].x} cy={segment[0].y} r="3" fill={line.color} />
          {:else}
            <polyline points={segment.map((point) => `${point.x},${point.y}`).join(' ')} fill="none" stroke={line.color} stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke" />
          {/if}
        {/each}
      {/each}
    </svg>
  </div>
  <div class="mt-2 flex flex-wrap justify-between gap-2 text-[11px] text-faint">
    <span>{formatDate(start)}</span>{#if start !== end}<span>{formatDate(end)}</span>{/if}
  </div>
{:else}
  <p class="mt-3 text-sm text-muted">No samples reported yet.</p>
{/if}
