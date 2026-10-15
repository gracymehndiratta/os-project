import type { AgentStatus } from '../types'

interface Props {
  status: AgentStatus | null
}

function StatCard({
  label,
  value,
  sub,
  color,
}: {
  label: string
  value: string | number
  sub?: string
  color: string
}) {
  const dotColor = {
    emerald: 'bg-emerald-400',
    red: 'bg-red-400',
    violet: 'bg-violet-400',
    sky: 'bg-sky-400',
  }[color] || 'bg-slate-400'

  return (
    <div className="card animate-slide-in group">
      <div className="flex items-center gap-2 mb-2">
        <div className={`w-2 h-2 rounded-full ${dotColor}`} />
        <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">{label}</span>
      </div>
      <div className="text-3xl font-extrabold tracking-tight text-white">{value}</div>
      {sub && <div className="text-xs text-slate-500 mt-1">{sub}</div>}
    </div>
  )
}

export default function StatusCards({ status }: Props) {
  if (!status) {
    return (
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mt-4">
        {[1, 2, 3, 4].map(i => (
          <div key={i} className="card">
            <div className="skeleton h-4 w-24 mb-3" />
            <div className="skeleton h-8 w-16" />
          </div>
        ))}
      </div>
    )
  }

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mt-4">
      <StatCard
        label="Processes"
        value={status.processes_monitored}
        sub={`${status.active_baselines} baselines active`}
        color="emerald"
      />
      <StatCard
        label="Active Alerts"
        value={status.alerts_raised}
        sub="total raised"
        color="red"
      />
      <StatCard
        label="Frozen Baselines"
        value={status.frozen_baselines}
        sub={`${status.quarantined_samples} quarantined samples`}
        color="violet"
      />
      <StatCard
        label="Budget Status"
        value={status.within_budget ? 'OK' : 'OVER'}
        sub={`${status.budget_violations} violations`}
        color={status.within_budget ? 'sky' : 'red'}
      />
    </div>
  )
}
