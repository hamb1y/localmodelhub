<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { HTMLButtonAttributes } from 'svelte/elements';
  import { cn } from '$lib/utils';

  let {
    children, variant = 'secondary', size = 'default', class: className,
    type = 'button', ...props
  }: HTMLButtonAttributes & {
    children?: Snippet;
    variant?: 'primary' | 'secondary' | 'ghost' | 'danger';
    size?: 'default' | 'sm' | 'icon';
  } = $props();
</script>

<button
  {type}
  class={cn(
    'inline-flex shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-lg border text-sm font-semibold transition-colors focus-visible:outline-2 focus-visible:outline-offset-3 focus-visible:outline-accent disabled:pointer-events-none disabled:opacity-45 [&_svg]:size-4',
    {
      primary: 'border-accent bg-accent text-accent-foreground hover:border-accent-hover hover:bg-accent-hover',
      secondary: 'border-line bg-raised text-foreground hover:border-line-strong hover:bg-hover',
      ghost: 'border-transparent text-muted hover:bg-hover hover:text-foreground',
      danger: 'border-danger/25 bg-danger/10 text-danger hover:bg-danger/20'
    }[variant],
    { default: 'h-9 px-3.5', sm: 'h-8 px-2.5 text-xs', icon: 'size-9' }[size],
    className
  )}
  {...props}
>
  {@render children?.()}
</button>
