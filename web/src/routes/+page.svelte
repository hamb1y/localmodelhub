<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { Activity as ActivityIcon, ArrowLeftRight, BarChart3, Box, CircleAlert, CircleCheck, FileUp, Layers3, Radio, RefreshCw, Server, Settings as SettingsIcon, Terminal } from 'lucide-svelte';
  import { api, ApiError, errorMessage, type Action, type Stats as ServiceStats, type Status, type Target } from '$lib/api';
  import { activeSources, canStart, canStop, formatDate, isRunning, normalize, stateLabel, tone } from '$lib/model-state';
  import Activity from '$lib/components/activity.svelte';
  import Catalog from '$lib/components/catalog.svelte';
  import Models from '$lib/components/models.svelte';
  import Settings from '$lib/components/settings.svelte';
  import Stats from '$lib/components/stats.svelte';
  import Badge from '$lib/components/ui/badge.svelte';
  import Button from '$lib/components/ui/button.svelte';
  import Dialog from '$lib/components/ui/dialog.svelte';

  const views = {
    models: { label: 'Models', description: 'Start, stop, and switch local model profiles.', icon: Box, group: 'Workspace' },
    activity: { label: 'Activity', description: 'Track model actions and their outcomes.', icon: ActivityIcon, group: 'Workspace' },
    stats: { label: 'Stats', description: 'Inspect host hardware and service metrics.', icon: BarChart3, group: 'Workspace' },
    catalog: { label: 'Catalog import', description: 'Validate an incoming catalog and stage it for the next boot.', icon: FileUp, group: 'Manage' },
    settings: { label: 'Settings', description: 'Connect to the service and get started.', icon: SettingsIcon, group: 'Manage' }
  } as const;
  type View = keyof typeof views;
  const groups = ['Workspace', 'Manage'] as const;
  const navigation = (Object.keys(views) as View[]);
  const pollMs = 3000;

  let current = $state<View>('models');
  let view = $derived(views[current]);
  let token = $state('');
  let origin = $state('');
  let status = $state<Status>();
  let statusLoading = $state(true);
  let statusError = $state('');
  let lastUpdate = $state<number>();
  let statusRequest: AbortController | undefined;
  let statsRequest: AbortController | undefined;
  let separateStats = $state<ServiceStats>();
  let metricsLoading = $state(false);
  let metricsError = $state('');
  let commandPending = $state(false);
  let bootLocked = $derived(!!status && ['pending', 'reconciling', 'failed'].includes(normalize(status.boot.state)));
  let feedback = $state<{ text: string; error: boolean }>();
  let switchOpen = $state(false);
  let destination = $state<Target>();
  let sourceKey = $state('');
  let sourceOptions = $derived(activeSources(status?.models ?? [], destination));
  let chosenSource = $derived(sourceOptions.find((item) => keyOf(item) === sourceKey));
  let destinationProfile = $derived(destination && status?.models.find((model) => model.id === destination?.model)?.profiles.find((profile) => profile.id === destination?.profile));
  let activeCount = $derived(status?.models.reduce((count, model) => count + model.profiles.filter((p) => isRunning(p.state)).length, 0));
  let logsOpen = $state(false);
  let logTarget = $state<Target>();
  let logLines = $state<string[]>([]);
  let logLoading = $state(false);
  let logError = $state('');
  let logsRequest: AbortController | undefined;

  function keyOf(target: Target) { return JSON.stringify([target.model, target.profile]); }
  function targetName(target: Target) {
    return `${status?.models.find((model) => model.id === target.model)?.name ?? target.model} / ${target.profile}`;
  }

  async function refresh(force = false) {
    if (statusRequest && !force) return;
    statusRequest?.abort();
    const request = new AbortController();
    statusRequest = request;
    statusLoading = true;
    try {
      const result = await api(token).status(request.signal);
      if (request.signal.aborted) return;
      if (!Array.isArray(result?.models) || !Array.isArray(result?.jobs) || !result.boot || !result.hardware) {
        throw new Error('The service returned an unexpected status response.');
      }
      status = result;
      lastUpdate = Date.now();
      statusError = '';
    } catch (e) {
      if (!request.signal.aborted) statusError = errorMessage(e);
    } finally {
      if (statusRequest === request) {
        statusRequest = undefined;
        statusLoading = false;
      }
    }
  }

  async function refreshMetrics() {
    statsRequest?.abort();
    const request = new AbortController();
    statsRequest = request;
    metricsLoading = true;
    metricsError = '';
    try {
      const result = await api(token).stats(request.signal);
      if (!request.signal.aborted) separateStats = result;
    } catch (e) {
      if (!request.signal.aborted && !(e instanceof ApiError && [404, 405, 501].includes(e.status))) metricsError = errorMessage(e);
    } finally {
      if (statsRequest === request) {
        statsRequest = undefined;
        metricsLoading = false;
      }
    }
  }

  async function refreshView() {
    const refreshExistingMetrics = needsSeparateStats;
    await refresh(true);
    if (refreshExistingMetrics && needsSeparateStats) void refreshMetrics();
  }

  function syncView() {
    const fragment = location.hash.slice(1);
    if (fragment === 'panel-content') return;
    const next = navigation.find((key) => key === fragment) ?? 'models';
    if (next !== current) window.scrollTo(0, 0);
    current = next;
  }

  onMount(() => {
    origin = location.origin;
    syncView();
    window.addEventListener('hashchange', syncView);
    void refresh();
    const timer = setInterval(() => {
      if (!document.hidden) void refresh();
    }, pollMs);
    const statsTimer = setInterval(() => {
      if (!document.hidden && needsSeparateStats && !statsRequest) void refreshMetrics();
    }, 30_000);
    const onVisibility = () => { if (!document.hidden) void refresh(); };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.removeEventListener('hashchange', syncView);
      document.removeEventListener('visibilitychange', onVisibility);
      clearInterval(timer);
      clearInterval(statsTimer);
      statusRequest?.abort();
      statsRequest?.abort();
      logsRequest?.abort();
    };
  });

  // Fetch on entry, then at the slower stats polling rate.
  let needsSeparateStats = $derived(current === 'stats' && !!status && !status.stats && !statusError);
  $effect(() => {
    if (needsSeparateStats) {
      void untrack(refreshMetrics);
    }
  });

  function updateToken(value: string) {
    token = value;
    statsRequest?.abort();
    separateStats = undefined;
    metricsError = '';
    status = undefined;
    statusError = '';
    lastUpdate = undefined;
    void refresh(true);
  }

  async function submitAction(action: Action) {
    if (commandPending || statusError || bootLocked || !status) return;
    const profile = status.models.find((model) => model.id === action.model)?.profiles.find((item) => item.id === action.profile);
    const validDestination = profile && profile.controllable !== false && (action.action === 'stop' ? canStop(profile.state) : canStart(profile.state));
    const validSource = action.action !== 'switch' || activeSources(status.models, action).some((item) => item.model === action.from_model && item.profile === action.from_profile);
    if (!validDestination || !validSource) {
      feedback = { text: 'The profile status changed. Review the latest status before trying again.', error: true };
      return;
    }
    feedback = undefined;
    commandPending = true;
    try {
      await api(token).action(action);
      feedback = { text: `${stateLabel(action.action)} requested for ${targetName(action)}. Track progress in Activity.`, error: false };
      if (action.action === 'switch') switchOpen = false;
      await refresh(true);
    } catch (e) {
      feedback = { text: errorMessage(e), error: true };
      if (action.action === 'switch') switchOpen = false;
      void refresh(true);
    } finally {
      commandPending = false;
    }
  }

  function openSwitch(target: Target) {
    destination = target;
    sourceKey = '';
    switchOpen = true;
  }

  function confirmSwitch(event: SubmitEvent) {
    event.preventDefault();
    if (!destination || !chosenSource) return;
    void submitAction({ action: 'switch', ...destination, from_model: chosenSource.model, from_profile: chosenSource.profile });
  }

  async function readLogs(target: Target) {
    logsRequest?.abort();
    const request = new AbortController();
    logsRequest = request;
    logError = '';
    logLoading = true;
    try {
      const result = await api(token).logs(target, request.signal);
      if (!request.signal.aborted) logLines = result.lines;
    } catch (e) {
      if (!request.signal.aborted) logError = errorMessage(e);
    } finally {
      if (logsRequest === request) {
        logsRequest = undefined;
        logLoading = false;
      }
    }
  }

  function openLogs(target: Target) {
    logTarget = target;
    logLines = [];
    logsOpen = true;
    void readLogs(target);
  }

  function openCatalog() { location.hash = 'catalog'; }
</script>

<svelte:head>
  <title>{view.label} · LocalModelHub</title>
  <meta name="description" content="Manage model profiles on your LocalModelHub host." />
</svelte:head>

<a href="#panel-content" class="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-60 focus:rounded-md focus:bg-accent focus:px-3 focus:py-2 focus:text-accent-foreground">Skip to content</a>

<div class="min-h-dvh lg:grid lg:grid-cols-[225px_minmax(0,1fr)]">
  <aside class="border-b border-line bg-sidebar lg:sticky lg:top-0 lg:flex lg:h-dvh lg:flex-col lg:border-r lg:border-b-0">
    <a href="#models" class="mx-4 mt-3 mb-2 flex w-fit items-center gap-2.5 rounded-lg lg:mx-5 lg:mt-6 lg:mb-8">
      <span class="grid size-9 place-items-center rounded-[10px] bg-accent text-accent-foreground"><Layers3 size={20} strokeWidth={2.2} /></span>
      <span><span class="block text-sm leading-tight font-bold tracking-tight">LocalModelHub</span><span class="mt-0.5 block text-[11px] text-faint">Control panel</span></span>
    </a>
    <nav aria-label="Main navigation" class="flex flex-wrap gap-1 px-3 pb-2 lg:block lg:px-3.5 lg:pb-0">
      {#each groups as group (group)}
        <div class="contents lg:mb-6 lg:block">
          <p class="eyebrow mx-2.5 mb-2 hidden lg:block">{group}</p>
          {#each navigation.filter((id) => views[id].group === group) as id (id)}
            {@const item = views[id]}
            <a {id} href={`#${id}`} aria-current={id === current ? 'page' : undefined} class={[
              'flex items-center gap-2 rounded-lg border px-2.5 py-2 text-xs font-medium transition-colors lg:mb-1 lg:gap-2.5 lg:text-sm',
              id === current ? 'border-accent bg-accent/55 text-foreground' : 'border-transparent text-muted hover:bg-hover hover:text-foreground'
            ]}>
              <item.icon size={16} strokeWidth={1.8} /> {item.label}
            </a>
          {/each}
        </div>
      {/each}
    </nav>
    <div class="mt-auto hidden px-4 pb-5 lg:block">
      <div class="flex items-center gap-2.5 rounded-lg border border-line bg-panel p-2.5">
        <Server size={16} class="shrink-0 text-faint"/>
        <div class="min-w-0"><p class="text-xs font-medium">LocalModelHub host</p><p class="mt-0.5 truncate font-mono text-[10px] text-faint" title={origin}>{origin ? origin.split('://').at(-1) : 'Same origin'}</p></div>
      </div>
    </div>
  </aside>

  <div class="min-w-0">
    <header class="border-b border-line bg-sidebar/45">
      <div class="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-2 px-4 py-2.5 sm:px-7 lg:min-h-14 lg:px-9">
        <div class="flex items-center gap-2 text-xs text-muted"><Radio size={15} class="text-faint" /> <span>Service status</span></div>
        <div class="flex items-center gap-2">
          <Badge dot tone={statusError ? 'red' : status ? 'green' : 'amber'}>{statusError ? 'Unavailable' : status ? 'Connected' : 'Connecting'}</Badge>
          <Button size="icon" variant="ghost" class="size-7 [&_svg]:size-3.5" onclick={refreshView} aria-label="Refresh service status" title={lastUpdate ? `Last update: ${formatDate(lastUpdate)}` : 'Refresh service status'}><RefreshCw /></Button>
        </div>
      </div>
    </header>

    <main id="panel-content" tabindex="-1" class="mx-auto max-w-6xl px-4 pt-6 pb-10 outline-none sm:px-7 sm:pt-8 lg:px-9">
      <div class="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div>
          <p class="eyebrow mb-1.5">{view.group}</p>
          <h1 class="text-[28px] leading-tight font-bold tracking-tight sm:text-[32px]">{view.label}</h1>
          <p class="mt-1.5 text-sm text-muted">{view.description}</p>
        </div>
        {#if current === 'models'}
          <Button onclick={openCatalog}><FileUp /> Import catalog</Button>
        {/if}
      </div>

      {#if statusError}
        <div role="status" class="notice notice-error mb-4">
          <CircleAlert class="mt-0.5 size-4 shrink-0" />
          <div class="min-w-0"><p class="font-semibold">Unable to reach the service</p><p class="wrap-anywhere">{statusError}{status ? ' Showing the last available snapshot.' : ''}</p></div>
        </div>
      {/if}
      {#if feedback}
        <div role={feedback.error ? 'alert' : 'status'} class={['notice mb-4', feedback.error ? 'notice-error' : 'notice-info']}>
          {#if feedback.error}<CircleAlert class="mt-0.5 size-4 shrink-0" />{:else}<CircleCheck class="mt-0.5 size-4 shrink-0" />{/if}
          <span class="min-w-0 flex-1 wrap-anywhere">{feedback.text}</span>
          <button type="button" aria-label="Dismiss action message" class="shrink-0 rounded px-1 text-xs underline underline-offset-2" onclick={() => feedback = undefined}>Dismiss</button>
        </div>
      {/if}

      {#if current === 'models'}
        <div class="panel mb-4 flex flex-wrap items-center gap-x-4 gap-y-2 px-3.5 py-2.5 text-xs sm:px-4">
          <span class="inline-flex items-center gap-2 text-muted"><span class="size-1.5 rounded-full bg-accent"></span>{activeCount === undefined ? 'Running profiles: —' : `${activeCount} running`}</span>
          <span class="hidden h-3 w-px bg-line sm:block" aria-hidden="true"></span>
          <span class="text-muted">Start never stops a running profile.</span>
          {#if status?.catalog_staged}<Badge tone="amber">Catalog staged for next boot</Badge>{/if}
          {#if status?.boot}
            <span class="flex flex-wrap items-center gap-2 sm:ml-auto"><span class="text-faint">Boot</span><Badge dot tone={tone(status.boot.state)}>{stateLabel(status.boot.state)}</Badge></span>
            {#if status.boot.message}<p class="w-full wrap-anywhere text-faint">{status.boot.message}</p>{/if}
          {/if}
        </div>
        <Models models={status?.models ?? []} ready={!!status} locked={commandPending || !!statusError || bootLocked} loading={statusLoading && !statusError} hasActive={!!activeCount} inferenceUrl={status?.inference_url} onAction={submitAction} onSwitch={openSwitch} onLogs={openLogs} onImport={openCatalog} />
      {:else if current === 'activity'}
        <Activity jobs={status?.jobs ?? []} models={status?.models ?? []} ready={!!status} />
      {:else if current === 'stats'}
        <Stats hardware={status?.hardware} stats={status?.stats ?? separateStats} models={status?.models ?? []} loading={metricsLoading} error={metricsError} />
      {:else if current === 'catalog'}
        <Catalog {token} alreadyStaged={status?.catalog_staged} onStage={() => void refresh(true)} />
      {:else}
        <Settings {token} {origin} onSave={updateToken} />
      {/if}
    </main>
  </div>
</div>

<Dialog bind:open={switchOpen} title="Switch model profile" description="Choose the running profile to stop. No other profile will be stopped by this request.">
  {#if destination}
    <form onsubmit={confirmSwitch}>
      <label for="switch-source" class="mb-1.5 block text-xs font-semibold">Stop this running profile</label>
      <select id="switch-source" class="field" bind:value={sourceKey} required disabled={commandPending || !sourceOptions.length}>
        <option value="" disabled>Choose a running profile</option>
        {#each sourceOptions as item (keyOf(item))}
          <option value={keyOf(item)}>{item.name} / {item.profile} · {item.device}</option>
        {/each}
      </select>
      {#if !sourceOptions.length}<p class="mt-2 text-xs text-warning">No running profiles are available to switch from.</p>{/if}
      <div class="mt-4 rounded-lg border border-line bg-background px-3 py-2.5">
        <p class="eyebrow">Then start</p>
        <p class="mt-1 text-sm font-semibold wrap-anywhere">{targetName(destination)}</p>
        {#if !destinationProfile || !canStart(destinationProfile.state)}<p class="mt-1 text-xs text-warning">This destination is no longer available to start.</p>{/if}
      </div>
      <div class="mt-5 flex flex-wrap justify-end gap-2">
        <Button onclick={() => switchOpen = false}>Cancel</Button>
        <Button type="submit" variant="primary" disabled={!chosenSource || commandPending || !!statusError || bootLocked || !destinationProfile || !canStart(destinationProfile.state)}>
          <ArrowLeftRight /> {commandPending ? 'Submitting…' : 'Confirm switch'}
        </Button>
      </div>
    </form>
  {/if}
</Dialog>

<Dialog bind:open={logsOpen} title="Profile logs" description={logTarget ? targetName(logTarget) : 'Output from the selected profile.'} class="max-w-3xl">
  <div class="flex justify-end">
    <Button size="sm" disabled={logLoading || !logTarget} onclick={() => logTarget && readLogs(logTarget)}><RefreshCw /> Refresh logs</Button>
  </div>
  {#if logError}
    <p role="alert" class="notice notice-error mt-3">{logError}</p>
  {:else if logLines.length}
    <!-- svelte-ignore a11y_no_noninteractive_tabindex (make the scrollable log keyboard accessible) -->
    <pre class="terminal mt-3" tabindex="0" aria-label="Log lines">{logLines.join('\n')}</pre>
  {:else}
    <p class="terminal mt-3 flex items-center gap-2"><Terminal size={14} />{logLoading ? 'Reading logs…' : 'No log lines returned.'}</p>
  {/if}
</Dialog>
