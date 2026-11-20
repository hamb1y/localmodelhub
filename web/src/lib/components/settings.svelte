<script lang="ts">
  import { CircleHelp, KeyRound, Server } from 'lucide-svelte';
  import Button from './ui/button.svelte';

  let { token, origin, onSave }: { token: string; origin: string; onSave: (value: string) => void } = $props();
  let value = $state('');

  function save(event: SubmitEvent) {
    event.preventDefault();
    onSave(value.trim());
    value = '';
  }
</script>

<div class="grid gap-3 lg:grid-cols-2">
  <section class="panel p-4 sm:p-5">
    <div class="flex items-center gap-2"><Server class="size-4 text-accent" /><h2 class="text-sm font-semibold">Service connection</h2></div>
    <p class="mt-2 text-sm leading-relaxed text-muted">The panel uses the same origin it was served from. Run it through LocalModelHub rather than opening the HTML file directly.</p>
    <p class="mt-3 w-fit rounded-md border border-line bg-background px-2.5 py-1.5 font-mono text-xs wrap-anywhere text-muted">{origin || 'Same origin'}{origin ? '/api' : ''}</p>
  </section>
  <section class="panel p-4 sm:p-5">
    <div class="flex items-center gap-2"><KeyRound class="size-4 text-accent" /><h2 class="text-sm font-semibold">Bearer token</h2></div>
    <p class="mt-2 text-sm leading-relaxed text-muted">Local access may not require a token. For remote access, enter the token configured on the host. It is kept in memory and cleared when you reload this page.</p>
    <form class="mt-3" onsubmit={save}>
      <label for="auth-token" class="mb-1.5 block text-xs font-semibold">Token {#if token}<span class="font-normal text-accent">· Set</span>{/if}</label>
      <input id="auth-token" type="password" class="field" placeholder={token ? 'Enter a replacement token' : 'Enter bearer token'} autocomplete="off" bind:value />
      <div class="mt-3 flex flex-wrap gap-2">
        <Button type="submit" variant="primary" disabled={!value.trim()}>Use token</Button>
        {#if token}<Button onclick={() => { value = ''; onSave(''); }}>Clear token</Button>{/if}
      </div>
    </form>
  </section>
</div>
<section class="panel mt-3 p-4 sm:p-5">
  <div class="flex items-center gap-2"><CircleHelp class="size-4 text-accent" /><h2 class="text-sm font-semibold">Getting started</h2></div>
  <ol class="mt-2 list-decimal space-y-1.5 pl-5 text-sm leading-relaxed text-muted">
    <li>Open Models to see available profiles and their devices.</li>
    <li>Use Start to start another profile without stopping any running profile.</li>
    <li>Use Switch to choose exactly which running profile to stop before starting the destination.</li>
    <li>Track requests in Activity and open profile Logs for startup details.</li>
  </ol>
</section>
