<script lang="ts">
  import type { Snippet } from 'svelte';
  import { cn } from '$lib/utils';

  let { children, tone = 'neutral', dot = false, class: className }: {
    children: Snippet;
    tone?: 'neutral' | 'green' | 'amber' | 'red';
    dot?: boolean;
    class?: string;
  } = $props();
</script>

<span class={cn(
  'inline-flex w-fit shrink-0 items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs leading-4 font-medium',
  {
    neutral: 'border-line bg-raised text-muted',
    green: 'border-accent/20 bg-accent/10 text-accent',
    amber: 'border-warning/25 bg-warning/10 text-warning',
    red: 'border-danger/25 bg-danger/10 text-danger'
  }[tone], className
)}>
  {#if dot}<span class="size-1.5 rounded-full bg-current" aria-hidden="true"></span>{/if}
  {@render children()}
</span>
