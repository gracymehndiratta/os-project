import { useEffect, useRef, useState } from 'react'
import type { AgentStatus, Alert, BaselineEntry, ProcessInfo } from './types'
import StatusCards from './components/StatusCards'
import AlertsPanel from './components/AlertsPanel'
import BaselinesPanel from './components/BaselinesPanel'
import ProcessesPanel from './components/ProcessesPanel'
import MetricsChart from './components/MetricsChart'
import BudgetPanel from './components/BudgetPanel'

type TabKey = 'overview' | 'alerts' | 'baselines' | 'processes'

export default function App() {
  const [status, setStatus] = useState<AgentStatus | null>(null)
  const [alerts, setAlerts] = useState<Alert[]>([])
  const [baselines, setBaselines] = useState<BaselineEntry[]>([])
  const [processes, setProcesses] = useState<ProcessInfo[]>([])
  const [connected, setConnected] = useState(false)
  const [activeTab, setActiveTab] = useState<TabKey>('overview')
  const wsRef = useRef<WebSocket | null>(null)

  // ---- WebSocket connection ----
  useEffect(() => {
    let reconnectTimer: ReturnType<typeof setTimeout>
    let alive = true

    function connect() {
      if (!alive) return
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const ws = new WebSocket(`${protocol}//${window.location.host}/ws`)
      wsRef.current = ws

      ws.onopen = () => setConnected(true)

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data)
          if (msg.type === 'status') {
            setStatus(msg.data)
          }
        } catch { /* ignore */ }
      }

      ws.onclose = () => {
        setConnected(false)
        if (alive) reconnectTimer = setTimeout(connect, 3000)
      }

      ws.onerror = () => ws.close()
    }

    connect()
    return () => {
      alive = false
      clearTimeout(reconnectTimer)
      wsRef.current?.close()
    }
  }, [])

  // ---- Periodic REST polling for alerts, baselines, processes ----
  useEffect(() => {
    let cancelled = false

    async function fetchData() {
      if (cancelled) return
      try {
        const [aRes, bRes, pRes] = await Promise.all([
          fetch('/api/alerts?limit=50'),
          fetch('/api/baselines'),
          fetch('/api/processes'),
        ])
        if (aRes.ok) setAlerts(await aRes.json())
        if (bRes.ok) setBaselines(await bRes.json())
        if (pRes.ok) setProcesses(await pRes.json())
      } catch { /* ignore network errors */ }
    }

    fetchData()
    const interval = setInterval(fetchData, 4000)
    return () => { cancelled = true; clearInterval(interval) }
  }, [])

  // ---- Actions ----
  async function acknowledgeAlert(alertId: string) {
    try {
      await fetch(`/api/alerts/${alertId}/acknowledge`, { method: 'POST' })
      setAlerts(prev => prev.map(a => a.alert_id === alertId ? { ...a, acknowledged: true } : a))
    } catch { /* ignore */ }
  }

  async function reconcileBaseline(pid: number) {
    try {
      await fetch(`/api/baselines/pid/${pid}/reconcile?corroborated=true`, { method: 'POST' })
      // Refresh baselines
      const res = await fetch('/api/baselines')
      if (res.ok) setBaselines(await res.json())
    } catch { /* ignore */ }
  }

  async function setTier(pid: number, tier: number) {
    try {
      await fetch(`/api/processes/${pid}/tier?tier=${tier}`, { method: 'POST' })
    } catch { /* ignore */ }
  }

  const unackAlerts = alerts.filter(a => !a.acknowledged)

  const tabs: { key: TabKey; label: string; count?: number }[] = [
    { key: 'overview', label: 'Overview' },
    { key: 'alerts', label: 'Alerts', count: unackAlerts.length || undefined },
    { key: 'baselines', label: 'Baselines' },
    { key: 'processes', label: 'Processes' },
  ]

  return (
    <div className="min-h-screen">
      {/* ─── Header ─── */}
      <header className="border-b border-slate-800/60 bg-slate-900/70 backdrop-blur-xl sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className={`status-dot ${connected ? 'bg-emerald-400' : 'bg-red-400'}`} />
            <h1 className="text-lg font-bold tracking-tight bg-gradient-to-r from-white to-slate-300 bg-clip-text text-transparent">
              OS Monitoring Agent
            </h1>
            <span className="hidden sm:inline text-xs text-slate-500 font-medium">
              Divergence-Gated Baseline Learning
            </span>
          </div>
          <div className="flex items-center gap-3">
            {status && (
              <span className="text-xs text-slate-500 hidden md:inline">
                Tier: {status.current_tier}
              </span>
            )}
            <span className={connected ? 'badge badge-normal' : 'badge badge-critical'}>
              {connected ? '● Live' : '● Disconnected'}
            </span>
          </div>
        </div>
      </header>

      {/* ─── Tabs ─── */}
      <nav className="max-w-7xl mx-auto px-4 pt-4">
        <div className="flex gap-1 border-b border-slate-800/40">
          {tabs.map(tab => (
            <button
              key={tab.key}
              id={`tab-${tab.key}`}
              onClick={() => setActiveTab(tab.key)}
              className={`px-4 py-2.5 text-sm font-medium transition-all duration-200 border-b-2 -mb-px ${
                activeTab === tab.key
                  ? 'border-blue-500 text-white'
                  : 'border-transparent text-slate-400 hover:text-slate-200 hover:border-slate-600'
              }`}
            >
              {tab.label}
              {tab.count !== undefined && tab.count > 0 && (
                <span className="ml-2 badge badge-critical text-[10px] py-0">{tab.count}</span>
              )}
            </button>
          ))}
        </div>
      </nav>

      {/* ─── Content ─── */}
      <main className="max-w-7xl mx-auto px-4 py-6 pb-12">
        {activeTab === 'overview' && (
          <div className="space-y-5 animate-fade-in">
            <StatusCards status={status} />
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
              <BudgetPanel status={status} />
              <MetricsChart status={status} />
            </div>
            <AlertsPanel
              alerts={alerts.slice(0, 5)}
              onAcknowledge={acknowledgeAlert}
              compact
            />
          </div>
        )}

        {activeTab === 'alerts' && (
          <div className="animate-fade-in mt-4">
            <AlertsPanel alerts={alerts} onAcknowledge={acknowledgeAlert} />
          </div>
        )}

        {activeTab === 'baselines' && (
          <div className="animate-fade-in mt-4">
            <BaselinesPanel baselines={baselines} onReconcile={reconcileBaseline} />
          </div>
        )}

        {activeTab === 'processes' && (
          <div className="animate-fade-in mt-4">
            <ProcessesPanel processes={processes} onSetTier={setTier} />
          </div>
        )}
      </main>

      {/* ─── Footer ─── */}
      <footer className="border-t border-slate-800/30 py-4 text-center text-xs text-slate-600">
        Divergence-Gated Behavioral Baseline Learning — Gracy Mehndiratta & Manan Kulshrestha
      </footer>
    </div>
  )
}
