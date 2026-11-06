import { useEffect, useRef } from 'react'
import * as echarts from 'echarts/core'
import { BarChart, GaugeChart, PieChart, ScatterChart } from 'echarts/charts'
import {
  AriaComponent,
  DatasetComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from 'echarts/components'
import { SVGRenderer } from 'echarts/renderers'
import type { EChartsCoreOption } from 'echarts/core'

// Tree-shaken Apache ECharts (DASHBOARD_RESEARCH.md §7) with the SVG renderer.
echarts.use([BarChart, PieChart, ScatterChart, GaugeChart, GridComponent, TooltipComponent, LegendComponent, DatasetComponent, AriaComponent, SVGRenderer])

import { CHART_PALETTE } from '../lib/chartPalette'

interface Props {
  option: EChartsCoreOption
  height?: number
  ariaLabel: string
  onSelect?: (name: string) => void
}

/** Thin typed React wrapper around ECharts. */
export function EChart({ option, height = 260, ariaLabel, onSelect }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)
  const selectRef = useRef(onSelect)

  useEffect(() => {
    selectRef.current = onSelect
  }, [onSelect])

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const chart = echarts.init(el, null, { renderer: 'svg' })
    chartRef.current = chart
    chart.on('click', (p) => {
      if (typeof p.name === 'string') selectRef.current?.(p.name)
    })
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => chart.resize()) : null
    ro?.observe(el)
    return () => {
      ro?.disconnect()
      chart.dispose()
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    chartRef.current?.setOption(
      {
        color: CHART_PALETTE,
        textStyle: { fontFamily: 'Segoe UI, system-ui, sans-serif' },
        aria: { enabled: true },
        animationDuration: 300,
        ...option,
      },
      true,
    )
  }, [option])

  return <div ref={ref} className="chart" style={{ height }} role="img" aria-label={ariaLabel} />
}
