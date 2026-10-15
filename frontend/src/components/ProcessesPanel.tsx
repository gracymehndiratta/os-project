import type { ProcessInfo } from '../types'
import { badgeClass, stateClass } from '../types'

interface Props {
  processes: ProcessInfo[]
  onSetTier: (pid: number, tier: number) => void
}

export default function ProcessesPanel({ processes, onSetTier }: Props) {
  if (processes.length === 0) {
    return (
      <div className="card">
        <div className="card-header">Monitored Processes</div>
        <div className="text-center text-slate-500 text-sm py-8">No processes collected yet.</div>
      </div>
    )
  }

  const sorted = [...processes].sort((a, b) => (b.risk_score ?? 0) - (a.risk_score ?? 0))

  return (
    <div className="card">
      <div className="flex items-center justify-between mb-4">
        <div className="card-header mb-0">Monitored Processes</div>
        <span className="text-xs text-slate-500">{processes.length} processes</span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-slate-500 uppercase tracking-wider border-b border-slate-800">
              <th className="text-left py-2 px-2">PID</th>
              <th className="text-left py-2 px-2">Name</th>
              <th className="text-right py-2 px-2">CPU %</th>
              <th className="text-right py-2 px-2">Mem MiB</th>
              <th className="text-center py-2 px-2">Risk</th>
              <th className="text-center py-2 px-2">Baseline</th>
              <th className="text-center py-2 px-2">Tier</th>
              <th className="text-left py-2 px-2">Parent</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map(p => (
              <tr key={p.pid} className="table-row">
                <td className="py-2.5 px-2 text-slate-400 font-mono text-xs">{p.pid}</td>
                <td className="py-2.5 px-2 font-medium text-white">{p.name}</td>
                <td className="py-2.5 px-2 text-right font-mono">
                  <span className={p.cpu_pct > 50 ? 'text-red-400' : p.cpu_pct > 20 ? 'text-amber-400' : 'text-slate-300'}>
                    {p.cpu_pct?.toFixed(1)}
                  </span>
                </td>
                <td className="py-2.5 px-2 text-right font-mono text-slate-300">
                  {p.mem_mib?.toFixed(0)}
                </td>
                <td className="py-2.5 px-2 text-center">
                  <div className="flex items-center justify-center gap-1.5">
                    <span className={badgeClass(p.risk_level || 'NORMAL')}>{p.risk_level || 'NORMAL'}</span>
                    <span className="text-[10px] text-slate-500 font-mono">{(p.risk_score ?? 0).toFixed(1)}</span>
                  </div>
                </td>
                <td className="py-2.5 px-2 text-center">
                  <span className={stateClass(p.baseline_state || 'LEARNING')}>
                    {p.baseline_state || 'LEARNING'}
                  </span>
                </td>
                <td className="py-2.5 px-2 text-center">
                  <span className="text-xs text-slate-400">{p.sampling_tier}</span>
                </td>
                <td className="py-2.5 px-2 text-slate-500 text-xs">{p.parent_name}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
