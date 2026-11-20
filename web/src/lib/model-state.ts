import type { Model, Target } from './api';

const running = new Set(['running', 'active', 'ready', 'online', 'unhealthy']);
const starting = new Set(['starting', 'loading', 'queued', 'pending', 'switching', 'restarting', 'stopping', 'provisioning', 'pulling', 'downloading']);
const stopped = new Set(['stopped', 'idle', 'offline', 'inactive', 'not_running', 'failed', 'error', 'exited', 'not_started', 'unloaded']);

export function normalize(state: string): string {
  return state.trim().toLowerCase().replace(/[\s-]+/g, '_');
}

export function stateLabel(state: string): string {
  return normalize(state).replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase()) || 'Unknown';
}

export const isRunning = (state: string) => running.has(normalize(state));
export const canStart = (state: string) => stopped.has(normalize(state));
export const canStop = (state: string) => isRunning(state) || ['starting', 'loading', 'queued', 'pending'].includes(normalize(state));
export const isWorking = (state: string) => starting.has(normalize(state));

export function tone(state: string): 'green' | 'amber' | 'red' | 'neutral' {
  const value = normalize(state);
  if (['failed', 'error', 'blocked', 'unavailable', 'unhealthy', 'interrupted'].includes(value)) return 'red';
  if (isRunning(value) || ['success', 'succeeded', 'complete', 'completed', 'done', 'ok'].includes(value)) return 'green';
  if (isWorking(value) || ['waiting', 'degraded', 'in_progress'].includes(value)) return 'amber';
  return 'neutral';
}

export function activeSources(models: Model[], destination?: Target) {
  return models.flatMap((model) => model.profiles.filter((p) =>
    p.controllable !== false && isRunning(p.state) && !(destination?.model === model.id && destination.profile === p.id)
  ).map((p) => ({ model: model.id, profile: p.id, name: model.name, device: p.device })));
}

export function formatBytes(bytes: number | null | undefined): string {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes === 0) return '0 B';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  const i = Math.max(0, Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024))));
  return `${new Intl.NumberFormat('en', { maximumFractionDigits: i > 0 ? 1 : 0 }).format(bytes / 1024 ** i)} ${units[i]}`;
}

export function memoryPercent(total?: number | null, available?: number | null): number | undefined {
  if (typeof total !== 'number' || typeof available !== 'number' || !Number.isFinite(total) || !Number.isFinite(available) || total <= 0 || available < 0) return undefined;
  return Math.round(Math.min(100, Math.max(0, (1 - available / total) * 100)));
}

export function formatDate(value?: string | number): string {
  if (value == null) return '—';
  const date = new Date(typeof value === 'number' && Math.abs(value) < 1e12 ? value * 1000 : value);
  if (Number.isNaN(date.valueOf())) return '—';
  return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit' }).format(date);
}
