import type { Alert } from '../types'
import { badgeClass, featureLabel } from '../types'

interface Props {
  alerts: Alert[]
  onAcknowledge: (alertId: string) => void
  compact?: boolean
}

function timeAgo(ts: string): string {
  const diff = (Date.now() - new Date(ts).getTime()) / 1000
  if (diff < 60) return `${Math.round(diff)}s ago`
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`
  return `${Math.round(diff / 3600)}h ago`
}

function ContributionBars({ contributions }: { contributions: Alert['contributions'] }) {
  if (!contributions || contributions.length === 0) return null
  const sorted = [...contributions].sort((a, b) => b.contribution - a.contribution)
  const top3 = sorted.slice(0, 3)

  const barColors = ['bg-red-400', 'bg-amber-400', 'bg-sky-400']

  return (
    <div className="mt-3 space-y-1.5">
      {top3.map((c, i) => {
        const pct = Math.min(c.contribution * 100, 100)
        const label = featureLabel(c.name)
        return (
          <div key={c.name} className="flex items-center gap-2">
            <span className="text-[10px] text-slate-400 w-24 truncate">{label}</span>
            <div className="flex-1 bg-slate-800 rounded-full h-2 overflow-hidden">
              <div
                className={`contrib-bar ${barColors[i] || 'bg-slate-500'}`}
                style={{ width: `${pct}%` }}
              />
            </div>
            <span className="text-[10px] text-slate-500 w-8 text-right">{(c.z_score ?? 0).toFixed(1)}σ</span>
          </div>
        )
      })}
    </div>
  )
}

export default function AlertsPanel({ alerts, onAcknowledge, compact }: Props) {
  const shown = compact ? alerts.slice(0, 5) : alerts

  return (
    <div className="card">
      <div className="flex items-center justify-between mb-4">
        <div className="card-header mb-0">
          {compact ? 'Recent Alerts' : 'Alert Feed'}
        </div>
        <span className="text-xs text-slate-500">{alerts.length} total</span>
      </div>

      {shown.length === 0 ? (
        <div className="text-center text-slate-500 text-sm py-8">
          No alerts yet — baselines are learning.
        </div>
      ) : (
        <div className="space-y-3 max-h-[500px] overflow-y-auto pr-1">
          {shown.map(alert => (
            <div
              key={alert.alert_id}
              className={`rounded-xl p-4 border transition-all animate-slide-in ${
                alert.acknowledged
                  ? 'border-slate-800 bg-slate-900/40 opacity-60'
                  : 'border-slate-700 bg-slate-900/70'
              }`}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className={badgeClass(alert.risk_level)}>{alert.risk_level}</span>
                    <span className="font-semibold text-sm text-white">{alert.process_name}</span>
                    <span className="text-xs text-slate-500">PID {alert.pid}</span>
                    <span className="text-xs text-slate-600">{timeAgo(alert.timestamp)}</span>
                  </div>

                  {alert.explanation && alert.explanation.length > 0 && (
                    <div className="mt-2 text-xs text-slate-400 space-y-0.5">
                      {alert.explanation.slice(0, 3).map((line, i) => (
                        <div key={i}>• {line}</div>
                      ))}
                    </div>
                  )}

                  <ContributionBars contributions={alert.contributions || []} />
                </div>

                <div className="flex flex-col items-end gap-1.5 shrink-0">
                  <div className="text-lg font-bold text-white">{alert.risk_score?.toFixed(1)}</div>
                  {!alert.acknowledged && (
                    <button
                      className="btn btn-ghost btn-sm"
                      onClick={() => onAcknowledge(alert.alert_id)}
                    >
                      Acknowledge
                    </button>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
