/* ---- Agent status pushed via WebSocket ---- */
export interface AgentStatus {
  running: boolean
  tick: number
  current_tier: string
  cpu_usage_percent: number
  memory_usage_mib: number
  io_bytes_per_sec: number
  latency_p50_ms: number
  latency_p95_ms: number
  latency_p99_ms: number
  budget_violations: number
  reduced_coverage_ticks: number
  budget: number
  active_baselines: number
  frozen_baselines: number
  quarantined_samples: number
  alerts_raised: number
  processes_monitored: number
  within_budget: boolean
  cpu_budget_max: number
  memory_budget_max: number
  io_budget_max: number
}

/* ---- Alert from /api/alerts ---- */
export interface AlertContribution {
  name: string
  value: number
  mean: number
  std: number
  z_score: number
  contribution: number
}

export interface Alert {
  alert_id: string
  pid: number
  process_name: string
  context_key: string
  risk_score: number
  risk_level: string
  severity: string
  d_squared: number
  level: string
  gate_state: string
  consecutive_count: number
  explanation: string[]
  contributions: AlertContribution[]
  recommended_action: string
  dry_run: boolean
  acknowledged: boolean
  timestamp: string
}

/* ---- Baseline from /api/baselines ---- */
export interface BaselineEntry {
  pid: number
  process_name: string
  context: string
  application_id: string
  state: string
  flagged: boolean
  st_mean: number[]
  st_std: number[]
  lt_mean: number[]
  lt_std: number[]
  lt_n: number
  cusum_value: number
  quarantine_size: number
  freeze_count: number
  freeze_tick: number | null
  freeze_time: number | null
  freeze_reason: string
  reconcile_tick: number | null
  reconcile_time: number | null
  last_update: number
  last_risk: number
  last_level: string
  feature_names: string[]
}

/* ---- Process from /api/processes ---- */
export interface ProcessInfo {
  pid: number
  name: string
  cpu_pct: number
  mem_mib: number
  risk_score: number
  risk_level: string
  baseline_state: string
  sampling_tier: string
  sampling_interval_sec: number
  parent_name: string
  username: string
}

/* ---- WebSocket message envelope ---- */
export interface WSMessage {
  type: string
  timestamp: string
  data: AgentStatus
}

/* ---- Feature name labels (canonical order, design §7) ---- */
export const FEATURE_NAMES = [
  'CPU %',
  'RSS MB',
  'Disk I/O KB/s',
  'Net KB/s',
  'Child Rate',
]

export const FEATURE_KEYS = [
  'cpu_pct',
  'rss_mb',
  'disk_io_kbps',
  'net_kbps',
  'child_rate',
]

/* Map a raw feature key (as emitted by the backend) to its display label. */
export function featureLabel(key: string): string {
  const i = FEATURE_KEYS.indexOf(key)
  return i >= 0 ? FEATURE_NAMES[i] : key
}

/* ---- Risk level → badge class map (design §8) ---- */
export function badgeClass(level: string): string {
  const l = level.toUpperCase()
  // Risk levels
  if (l === 'SUSPEND' || l === 'CRITICAL') return 'badge badge-critical'
  if (l === 'THROTTLE' || l === 'HIGH') return 'badge badge-high'
  if (l === 'NOTIFY' || l === 'MEDIUM') return 'badge badge-medium'
  if (l === 'LOW') return 'badge badge-low'
  // Gate / baseline states
  if (l === 'FROZEN') return 'badge badge-frozen'
  if (l === 'LEARNING') return 'badge badge-learning'
  if (l === 'MERGE' || l === 'RECONCILED') return 'badge badge-reconciled'
  return 'badge badge-normal'
}

export function stateClass(state: string): string {
  const s = state.toUpperCase()
  if (s === 'FROZEN') return 'badge badge-frozen'
  if (s === 'LEARNING') return 'badge badge-learning'
  if (s === 'MERGE' || s === 'RECONCILED') return 'badge badge-reconciled'
  if (s === 'DISCARD') return 'badge badge-medium'
  return 'badge badge-normal'
}
