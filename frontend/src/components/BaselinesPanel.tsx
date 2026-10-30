import type { BaselineEntry } from '../types'
import { stateClass, FEATURE_NAMES } from '../types'

interface Props {
  baselines: BaselineEntry[]
  onReconcile: (pid: number) => void
}

function MiniBar({ st, lt, label }: { st: number; lt: number; label: string }) {
  const maxVal = Math.max(Math.abs(st), Math.abs(lt), 0.1)
  const stPct = (Math.abs(st) / maxVal) * 100
  const ltPct = (Math.abs(lt) / maxVal) * 100

  return (
    <div className="flex items-center gap-1.5">
      <span className="text-[9px] text-slate-500 w-16 truncate">{label}</span>
      <div className="flex-1 flex items-center gap-0.5 h-3">
        <div className="bg-cyan-500/50 h-full rounded-sm" style={{ width: `${stPct}%`, minWidth: '2px' }} title={`ST: ${st.toFixed(2)}`} />
        <div className="bg-emerald-500/50 h-full rounded-sm" style={{ width: `${ltPct}%`, minWidth: '2px' }} title={`LT: ${lt.toFixed(2)}`} />
      </div>
    </div>
  )
}

export default function BaselinesPanel({ baselines, onReconcile }: Props) {
  if (baselines.length === 0) {
    return (
      <div className="card">
        <div className="card-header">Baselines</div>
        <div className="text-center text-slate-500 text-sm py-8">No baselines collected yet.</div>
      </div>
    )
  }

  // One row per process. Prefer the most "interesting" context for a PID
  // (a frozen/quarantined key is more informative than a learning one) and
  // break ties by the most recently updated baseline.
  const rank: Record<string, number> = {
    FROZEN: 0, MERGE: 1, DISCARD: 2, LEARNING: 3, NORMAL: 4,
  }
  const byPid = new Map<number, BaselineEntry>()
  for (const b of baselines) {
    const cur = byPid.get(b.pid)
    if (
      !cur ||
      (rank[b.state] ?? 9) < (rank[cur.state] ?? 9) ||
      ((rank[b.state] ?? 9) === (rank[cur.state] ?? 9) && b.last_update > cur.last_update)
    ) {
      byPid.set(b.pid, b)
    }
  }
  const entries = Array.from(byPid.values()).sort((a, b) => {
    const order: Record<string, number> = { FROZEN: 0, LEARNING: 1, NORMAL: 2, MERGE: 3, DISCARD: 4 }
    return (order[a.state] ?? 9) - (order[b.state] ?? 9)
  })

  return (
    <div className="card">
      <div className="flex items-center justify-between mb-4">
        <div className="card-header mb-0">Baseline Store</div>
        <div className="flex gap-2 text-[10px] text-slate-500">
          <span><span className="inline-block w-2 h-2 bg-cyan-500/60 rounded-sm mr-1" />Short-term</span>
          <span><span className="inline-block w-2 h-2 bg-emerald-500/60 rounded-sm mr-1" />Long-term</span>
        </div>
      </div>

      <div className="space-y-3 max-h-[500px] overflow-y-auto pr-1">
        {entries.map(b => (
          <div
            key={`${b.pid}-${b.context}`}
            className={`rounded-xl p-4 border animate-slide-in ${
              b.state === 'FROZEN'
                ? 'border-violet-500/40 bg-violet-500/5'
                : 'border-slate-800 bg-slate-900/40'
            }`}
          >
            <div className="flex items-start justify-between gap-3">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap mb-2">
                  <span className="font-semibold text-sm text-white">{b.process_name}</span>
                  <span className="text-xs text-slate-500">PID {b.pid}</span>
                  <span className={stateClass(b.state)}>{b.state}</span>
                  {b.quarantine_size > 0 && (
                    <span className="text-[10px] text-violet-300">
                      🔒 {b.quarantine_size} quarantined
                    </span>
                  )}
                </div>

                <div className="grid grid-cols-2 gap-x-4 gap-y-0.5">
                  {FEATURE_NAMES.map((name, i) => (
                    <MiniBar
                      key={name}
                      label={name}
                      st={b.st_mean[i] ?? 0}
                      lt={b.lt_mean[i] ?? 0}
                    />
                  ))}
                </div>

                <div className="flex gap-4 mt-2 text-[10px] text-slate-500">
                  <span>Observations: {b.lt_n}</span>
                  <span>CUSUM: {b.cusum_value.toFixed(2)}</span>
                  <span>Freezes: {b.freeze_count}</span>
                </div>
              </div>

              <div className="flex flex-col items-end gap-1.5 shrink-0">
                {b.state === 'FROZEN' && (
                  <button
                    className="btn btn-success btn-sm"
                    onClick={() => onReconcile(b.pid)}
                    title="Merge quarantined samples into long-term baseline"
                  >
                    ✓ Reconcile
                  </button>
                )}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
