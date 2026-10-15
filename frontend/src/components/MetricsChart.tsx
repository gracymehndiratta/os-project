import { useEffect, useRef, useState } from 'react'
import type { AgentStatus } from '../types'

interface Props {
  status: AgentStatus | null
}

interface DataPoint {
  t: number
  cpu: number
  mem: number
}

const MAX_POINTS = 60
const CHART_W = 600
const CHART_H = 160
const PAD = { top: 10, right: 10, bottom: 25, left: 40 }
const INNER_W = CHART_W - PAD.left - PAD.right
const INNER_H = CHART_H - PAD.top - PAD.bottom

function buildPath(data: DataPoint[], key: 'cpu' | 'mem', maxVal: number): string {
  if (data.length < 2) return ''
  return data
    .map((d, i) => {
      const x = PAD.left + (i / (MAX_POINTS - 1)) * INNER_W
      const y = PAD.top + INNER_H - (Math.min(d[key], maxVal) / maxVal) * INNER_H
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
}

function buildArea(data: DataPoint[], key: 'cpu' | 'mem', maxVal: number): string {
  if (data.length < 2) return ''
  const line = data.map((d, i) => {
    const x = PAD.left + (i / (MAX_POINTS - 1)) * INNER_W
    const y = PAD.top + INNER_H - (Math.min(d[key], maxVal) / maxVal) * INNER_H
    return `${x.toFixed(1)},${y.toFixed(1)}`
  })
  const bottom = PAD.top + INNER_H
  return `M${line[0]} L${line.join(' L')} L${PAD.left + ((data.length - 1) / (MAX_POINTS - 1)) * INNER_W},${bottom} L${PAD.left},${bottom} Z`
}

export default function MetricsChart({ status }: Props) {
  const [data, setData] = useState<DataPoint[]>([])
  const prevStatus = useRef(status)

  useEffect(() => {
    if (!status || status === prevStatus.current) return
    prevStatus.current = status

    setData(prev => {
      const next = [
        ...prev,
        { t: Date.now(), cpu: status.cpu_usage_percent, mem: status.memory_usage_mib },
      ]
      return next.slice(-MAX_POINTS)
    })
  }, [status])

  const cpuMax = 10
  const memMax = 300

  const yTicksCpu = [0, 2.5, 5, 7.5, 10]

  return (
    <div className="card">
      <div className="card-header">Agent Overhead — Live</div>
      <svg
        viewBox={`0 0 ${CHART_W} ${CHART_H}`}
        className="w-full"
        preserveAspectRatio="xMidYMid meet"
      >
        <defs>
          <linearGradient id="cpuGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#38bdf8" stopOpacity="0.3" />
            <stop offset="100%" stopColor="#38bdf8" stopOpacity="0.02" />
          </linearGradient>
          <linearGradient id="memGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#a78bfa" stopOpacity="0.25" />
            <stop offset="100%" stopColor="#a78bfa" stopOpacity="0.02" />
          </linearGradient>
        </defs>

        {/* Grid lines */}
        {yTicksCpu.map(v => {
          const y = PAD.top + INNER_H - (v / cpuMax) * INNER_H
          return (
            <g key={v}>
              <line x1={PAD.left} y1={y} x2={CHART_W - PAD.right} y2={y} stroke="rgba(51,65,85,0.4)" strokeDasharray="4" />
              <text x={PAD.left - 6} y={y + 3} textAnchor="end" fontSize="9" fill="#64748b">{v}%</text>
            </g>
          )
        })}

        {/* CPU area + line */}
        {data.length >= 2 && (
          <>
            <path d={buildArea(data, 'cpu', cpuMax)} fill="url(#cpuGrad)" />
            <path d={buildPath(data, 'cpu', cpuMax)} fill="none" stroke="#38bdf8" strokeWidth="2" strokeLinejoin="round" />
          </>
        )}

        {/* Mem area + line (normalized to fit) */}
        {data.length >= 2 && (
          <>
            <path d={buildArea(data, 'mem', memMax)} fill="url(#memGrad)" />
            <path d={buildPath(data, 'mem', memMax)} fill="none" stroke="#a78bfa" strokeWidth="2" strokeLinejoin="round" strokeDasharray="6 3" />
          </>
        )}

        {/* Legend */}
        <circle cx={PAD.left + 8} cy={CHART_H - 8} r="4" fill="#38bdf8" />
        <text x={PAD.left + 16} y={CHART_H - 5} fontSize="9" fill="#94a3b8">CPU %</text>
        <circle cx={PAD.left + 68} cy={CHART_H - 8} r="4" fill="#a78bfa" />
        <text x={PAD.left + 76} y={CHART_H - 5} fontSize="9" fill="#94a3b8">Mem MiB</text>
      </svg>
      {data.length < 2 && (
        <div className="text-center text-slate-500 text-xs py-6">Waiting for data…</div>
      )}
    </div>
  )
}
