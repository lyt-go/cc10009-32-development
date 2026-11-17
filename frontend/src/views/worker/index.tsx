import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/worker'
const columns = [
  '节点编号', '主机地址', '节点角色', '节点状态标签', '地域标签',
  '当前配置版本号', '差异摘要', '最后同步时间', '关联同步结果',
]
const nodeActions = ['标记在线', '标记离线']

type Row = Record<string, string | number | null>
type Json = Record<string, unknown>

export default function WorkerView() {
  const [view, setView] = useState<'capacity' | 'configNodes'>('capacity')
  const [rows, setRows] = useState<Row[]>([])
  const [configNodes, setConfigNodes] = useState<Json[]>([])
  const [total, setTotal] = useState(0)
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)
  const [busy, setBusy] = useState(false)

  const reload = useCallback(async () => {
    const resp = await request(`${ENDPOINT}?size=200`)
    if (!resp.ok) throw new Error('采集节点列表读取失败')
    const payload = (await resp.json()) as { items: Row[]; total: number }
    setRows(payload.items ?? [])
    setTotal(payload.total ?? 0)
  }, [])

  const reloadConfigNodes = useCallback(async () => {
    const resp = await request(`${ENDPOINT}/config-nodes`)
    if (!resp.ok) throw new Error('配置节点清单读取失败')
    const payload = (await resp.json()) as { items: Json[] }
    setConfigNodes(payload.items ?? [])
  }, [])

  useEffect(() => { void reload().catch((e) => setMessage({ ok: false, text: String(e) })) }, [reload])
  useEffect(() => {
    if (view === 'configNodes') void reloadConfigNodes().catch((e) => setMessage({ ok: false, text: String(e) }))
  }, [view, reloadConfigNodes])

  const stats = useMemo(() => [
    { label: '节点总数', value: rows.length },
    { label: '在线节点', value: rows.filter((r) => r.节点状态标签 === '在线').length },
    { label: '离线节点', value: rows.filter((r) => r.节点状态标签 === '离线').length },
    { label: '已回写配置版本', value: rows.filter((r) => r.关联同步结果).length },
  ], [rows])

  async function post(path: string, body: Json) {
    setBusy(true)
    setMessage(null)
    try {
      const resp = await request(path, {
        method: 'POST',
        headers: { 'X-User-Role': 'admin' },
        body: JSON.stringify({ values: body }),
      })
      const result = (await resp.json()) as { ok: boolean; message: string }
      setMessage({ ok: result.ok, text: result.message })
      if (result.ok) await reload()
      return result.ok
    } catch (e) {
      setMessage({ ok: false, text: e instanceof Error ? e.message : '操作失败' })
      return false
    } finally {
      setBusy(false)
    }
  }

  async function nodeAction(row: Row, action: string) {
    await post(`${ENDPOINT}/${row.id}/actions`, { action })
  }

  async function applySyncResult(row: Row) {
    await post(`${ENDPOINT}/apply-sync-result`, { 节点编号: String(row.节点编号) })
  }

  async function collectVersions() {
    const ok = await post(`${ENDPOINT}/collect-config-versions`, {})
    if (ok) await reloadConfigNodes()
  }

  const cnColumns = configNodes.length
    ? Object.keys(configNodes[0]).filter((k) => k !== 'id')
    : []

  return (
    <section className="page" data-module="worker">
      <header className="page-head">
        <div>
          <h2>采集节点管理（容量侧 / 执行侧）</h2>
          <p className="page-desc">
            容量侧读取配置同步结果并把实际生效版本回写到可调度节点记录；执行侧把节点配置版本采集到配置节点清单，
            配置版本号、差异摘要、最后同步时间共同解释节点配置版本。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" disabled={busy} onClick={() => void collectVersions()}>
            执行侧采集配置版本
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出节点清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <div className="tab-bar">
        <button type="button" className={`tab-btn ${view === 'capacity' ? 'active' : ''}`} onClick={() => setView('capacity')}>
          可调度节点清单（容量侧）
        </button>
        <button type="button" className={`tab-btn ${view === 'configNodes' ? 'active' : ''}`} onClick={() => setView('configNodes')}>
          配置节点清单（执行侧）
        </button>
      </div>

      {view === 'capacity' ? (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                {columns.map((c) => <th key={c}>{c}</th>)}
                <th>节点动作</th>
                <th>容量侧回写</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={String(row.id)} className={row.节点状态标签 === '离线' ? 'row-abn' : ''}>
                  {columns.map((c) => <td key={c}>{row[c] ?? '—'}</td>)}
                  <td className="row-actions">
                    {nodeActions.map((a) => (
                      <button key={a} className="link" type="button" disabled={busy} onClick={() => void nodeAction(row, a)}>{a}</button>
                    ))}
                  </td>
                  <td className="row-actions">
                    <button className="link" type="button" disabled={busy} onClick={() => void applySyncResult(row)}>
                      读取同步结果并回写版本
                    </button>
                  </td>
                </tr>
              ))}
              {rows.length === 0 && <tr><td className="empty-state" colSpan={columns.length + 2}>暂无采集节点数据</td></tr>}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="table-wrap">
          <table className="data-table compact">
            <thead>
              <tr>{cnColumns.map((c) => <th key={c}>{c}</th>)}</tr>
            </thead>
            <tbody>
              {configNodes.map((node, i) => (
                <tr key={i}>{cnColumns.map((c) => <td key={c}>{String(node[c] ?? '—')}</td>)}</tr>
              ))}
              {configNodes.length === 0 && <tr><td className="empty-state" colSpan={1}>尚未采集，点击右上角“执行侧采集配置版本”</td></tr>}
            </tbody>
          </table>
        </div>
      )}

      <footer className="page-foot">
        <span>共 {view === 'capacity' ? total : configNodes.length} 条记录</span>
        {message && <span className={message.ok ? 'ok-text' : 'error-text'}>{message.text}</span>}
      </footer>
    </section>
  )
}
