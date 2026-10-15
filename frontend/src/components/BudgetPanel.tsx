import type { AgentStatus } from '../types'

interface Props {
  status: AgentStatus | null
}

function RadialGauge({
  label,
  value,
  max,
  unit,
  color,
}: {
  label: string
  value: number
  max: number
  unit: string
  color: string
}) {
  const pct = Math.min((value / max) * 100, 100)
  const r = 45
  const circ = 2 * Math.PI * r
  const offset = circ - (pct / 100) * circ

  const strokeColor = pct > 85
    ? '#ef4444'
    : pct > 60
      ? '#f59e0b'
      : color

  return (
    <div className="flex flex-col items-center gap-2">
      <div className="relative">
        <svg width="120" height="120" viewBox="0 0 120 120">
          {/* Background ring */}
          <circle
            cx="60" cy="60" r={r}
            fill="none"
            stroke="rgba(51, 65, 85, 0.4)"
            strokeWidth="8"
          />
          {/* Value ring */}
          <circle
            cx="60" cy="60" r={r}
            fill="none"
            stroke={strokeColor}
            strokeWidth="8"
            strokeLinecap="round"
            strokeDasharray={circ}
            strokeDashoffset={offset}
            className="gauge-ring"
            transform="rotate(-90 60 60)"
            style={{ filter: `drop-shadow(0 0 6px ${strokeColor}40)` }}
          />
          {/* Center text */}
          <text x="60" y="54" textAnchor="middle" className="fill-white text-lg font-bold" fontSize="18">
            {value.toFixed(1)}
          </text>
          <text x="60" y="72" textAnchor="middle" className="fill-slate-400 text-xs" fontSize="11">
            {unit}
          </text>
        </svg>
      </div>
      <div className="text-xs font-semibold text-slate-400 uppercase tracking-wider">{label}</div>
      <div className="text-[10px] text-slate-500">
        Budget: {max} {unit}
      </div>
    </div>
  )
}

export default function BudgetPanel({ status }: Props) {
  if (!status) {
    return (
      <div className="card">
        <div className="card-header">Agent Overhead Budget</div>
        <div className="flex justify-around py-8">
          {[1, 2, 3].map(i => <div key={i} className="skeleton w-28 h-28 rounded-full" />)}
        </div>
      </div>
    )
  }

  return (
    <div className="card">
      <div className="flex items-center justify-between mb-4">
        <div className="card-header mb-0">Agent Overhead Budget</div>
        <span className={status.within_budget ? 'badge badge-normal' : 'badge badge-critical'}>
          {status.within_budget ? '● Within Budget' : '● Over Budget'}
        </span>
      </div>
      <div className="flex justify-around">
        <RadialGauge
          label="CPU"
          value={status.cpu_usage_percent}
          max={status.cpu_budget_max || 5}
          unit="%"
          color="#38bdf8"
        />
        <RadialGauge
          label="Memory"
          value={status.memory_usage_mib}
          max={status.memory_budget_max || 256}
          unit="MiB"
          color="#a78bfa"
        />
        <RadialGauge
          label="I/O"
          value={status.io_bytes_per_sec}
          max={status.io_budget_max || 1}
          unit="MB/s"
          color="#34d399"
        />
      </div>
    </div>
  )
}
