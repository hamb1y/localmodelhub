<script lang="ts">
  import { Dialog } from 'bits-ui';
  import { X } from 'lucide-svelte';
  import type { Snippet } from 'svelte';
  import { cn } from '$lib/utils';

  let { open = $bindable(false), title, description, children, class: className }: {
    open?: boolean;
    title: string;
    description: string;
    children: Snippet;
    class?: string;
  } = $props();
</script>

<Dialog.Root bind:open>
  <Dialog.Portal>
    <Dialog.Overlay class="fixed inset-0 z-40 bg-black/65 backdrop-blur-[2px]" />
    <Dialog.Content class={cn('fixed top-1/2 left-1/2 z-50 flex max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 flex-col overflow-y-auto rounded-2xl border border-line-strong bg-panel p-5 shadow-2xl sm:p-6', className)}>
      <Dialog.Close aria-label="Close dialog" class="absolute top-4 right-4 inline-flex size-8 items-center justify-center rounded-md text-muted hover:bg-hover hover:text-foreground focus-visible:outline-2 focus-visible:outline-accent">
        <X size={17} />
      </Dialog.Close>
      <Dialog.Title class="pr-9 text-lg font-semibold text-foreground">{title}</Dialog.Title>
      <Dialog.Description class="mt-1.5 pr-4 text-sm leading-relaxed text-muted">{description}</Dialog.Description>
      <div class="mt-5">
        {@render children()}
      </div>
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>
