import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/configsync'

type Row = Record<string, string | number | null>
type Json = Record<string, unknown>
type Role = 'admin' | 'viewer'

const taskColumns = [
  '任务编号', '源节点', '目标节点', '同步类型', '握手状态', '批次号',
  '源快照版本', '生效前版本', '生效后版本', '差异摘要', '最后同步时间', '同步状态',
]

// 需要在弹窗里补充参数的动作；其余动作直接执行。
const PARAM_ACTIONS: Record<string, { label: string; field: string; placeholder: string; required: boolean }> = {
  标记失败: { label: '失败原因', field: '失败原因', placeholder: '请填写失败原因', required: true },
  发布源新版本: { label: '新版本号', field: '新版本号', placeholder: '留空则自动递增，如 cfg-v4', required: false },
}

const DATA_TABS = [
  { key: 'tasks', label: '同步任务', path: '' },
  { key: 'results', label: '同步结果', path: '/results' },
  { key: 'audits', label: '同步审计', path: '/audits' },
  { key: 'snapshots', label: '配置快照', path: '/snapshots' },
  { key: 'progress', label: '应用进度', path: '/progress' },
] as const

type TabKey = (typeof DATA_TABS)[number]['key']

async function load<T>(path: string): Promise<T> {
  const resp = await request(path)
  if (!resp.ok) throw new Error(`接口返回 ${resp.status}`)
  return (await resp.json()) as T
}

export default function ConfigsyncView() {
  const [role, setRole] = useState<Role>('admin')
  const isAdmin = role === 'admin'

  const [tab, setTab] = useState<TabKey>('tasks')
  const [tasks, setTasks] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [statusFilter, setStatusFilter] = useState('')
  const [targetFilter, setTargetFilter] = useState('')
  const [actionFlow, setActionFlow] = useState<Record<string, string[]>>({})
  const [statuses, setStatuses] = useState<string[]>([])

  const [dataset, setDataset] = useState<Json[]>([])
  const [detail, setDetail] = useState<Json | null>(null)
  const [modal, setModal] = useState<{ taskId: number; action: string } | null>(null)
  const [paramValue, setParamValue] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [createForm, setCreateForm] = useState({ 任务编号: '', 源节点: 'WORK-0003', 目标节点: '', 同步类型: '全量同步' })

  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)
  const [loading, setLoading] = useState(false)

  const headers = useMemo(() => ({ 'X-User-Role': role }), [role])

  const reloadTasks = useCallback(async () => {
    const qs = new URLSearchParams()
    if (statusFilter) qs.set('status', statusFilter)
    if (targetFilter) qs.set('target', targetFilter)
    qs.set('size', '200')
    const data = await load<{ items: Row[]; total: number }>(`${ENDPOINT}?${qs.toString()}`)
    setTasks(data.items)
    setTotal(data.total)
  }, [statusFilter, targetFilter])

  const reloadMeta = useCallback(async () => {
    const meta = await load<{ statuses: string[]; actions: Record<string, string[]> }>(`${ENDPOINT}/status-meta`)
    setStatuses(meta.statuses)
    setActionFlow(meta.actions)
  }, [])

  const reloadDataset = useCallback(async (key: TabKey) => {
    const conf = DATA_TABS.find((t) => t.key === key)!
    if (!conf.path) {
      setDataset([])
      return
    }
    const data = await load<{ items: Json[] }>(`${ENDPOINT}${conf.path}`)
    setDataset(data.items)
  }, [])

  useEffect(() => { void reloadMeta() }, [reloadMeta])
  useEffect(() => { void reloadTasks().catch((e) => setMessage({ ok: false, text: String(e) })) }, [reloadTasks])
  useEffect(() => { void reloadDataset(tab).catch((e) => setMessage({ ok: false, text: String(e) })) }, [tab, reloadDataset])

  const stats = useMemo(() => {
    const completed = tasks.filter((t) => t.status === '已完成').length
    const failed = tasks.filter((t) => t.status === '已失败' || t.status === '版本冲突' || t.status === '传输中断').length
    const active = tasks.filter((t) => t.status !== '已完成' && t.status !== '已失败').length
    return [
      { label: '同步任务总数', value: tasks.length },
      { label: '已完成（唯一结果）', value: completed },
      { label: '活动任务', value: active },
      { label: '异常/中断/冲突', value: failed },
    ]
  }, [tasks])

  function availableActions(row: Row): string[] {
    const current = String(row.status ?? '')
    return Object.entries(actionFlow)
      .filter(([, states]) => states.includes(current))
      .map(([action]) => action)
  }

  async function openDetail(row: Row) {
    const data = await load<Json>(`${ENDPOINT}/${row.id}`)
    setDetail(data)
  }

  async function executeAction(taskId: number, action: string, extra: Json = {}) {
    setLoading(true)
    setMessage(null)
    try {
      const resp = await request(`${ENDPOINT}/${taskId}/actions`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ values: { action, ...extra } }),
      })
      const result = (await resp.json()) as { ok: boolean; message: string }
      setMessage({ ok: result.ok, text: result.message })
      if (result.ok) {
        await reloadTasks()
        if (detail) setDetail(await load<Json>(`${ENDPOINT}/${(detail as Row).id}`))
      }
    } catch (e) {
      setMessage({ ok: false, text: e instanceof Error ? e.message : '动作执行失败' })
    } finally {
      setLoading(false)
    }
  }

  function onActionClick(row: Row, action: string) {
    if (!isAdmin) {
      setMessage({ ok: false, text: '当前为只读角色，非管理员只能只读查看同步差异，不能执行维护动作' })
      return
    }
    if (PARAM_ACTIONS[action]) {
      setModal({ taskId: Number(row.id), action })
      setParamValue('')
    } else {
      void executeAction(Number(row.id), action)
    }
  }

  async function confirmModal() {
    if (!modal) return
    const conf = PARAM_ACTIONS[modal.action]
    if (conf.required && !paramValue.trim()) {
      setMessage({ ok: false, text: `${conf.label}为必填项` })
      return
    }
    const extra = paramValue.trim() ? { [conf.field]: paramValue.trim() } : {}
    setModal(null)
    await executeAction(modal.taskId, modal.action, extra)
  }

  async function submitCreate() {
    if (!createForm.任务编号.trim() || !createForm.目标节点.trim()) {
      setMessage({ ok: false, text: '任务编号与目标节点为必填项' })
      return
    }
    setLoading(true)
    try {
      const resp = await request(ENDPOINT, { method: 'POST', headers, body: JSON.stringify({ values: createForm }) })
      const result = (await resp.json()) as { ok: boolean; message: string }
      setMessage({ ok: result.ok, text: result.message })
      if (result.ok) {
        setShowCreate(false)
        setCreateForm({ 任务编号: '', 源节点: 'WORK-0003', 目标节点: '', 同步类型: '全量同步' })
        await reloadTasks()
      }
    } catch (e) {
      setMessage({ ok: false, text: e instanceof Error ? e.message : '登记失败' })
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="page" data-module="configsync">
      <header className="page-head">
        <div>
          <h2>配置同步管理</h2>
          <p className="page-desc">
            双端版本握手 + 分段传输 + 版本冲突隔离；同一任务重复/并发提交只产生一个完成版本，
            同步结果与审计数据集逐项可核对，容量侧回写实际生效版本。
          </p>
        </div>
        <div className="page-actions">
          <label className="role-switch">
            角色：
            <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
              <option value="admin">管理员（可维护）</option>
              <option value="viewer">只读观察员（仅查看）</option>
            </select>
          </label>
          <button className="btn primary" type="button" disabled={!isAdmin} onClick={() => setShowCreate(true)}>
            登记同步任务
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出同步域数据集
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
        {DATA_TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            className={`tab-btn ${tab === t.key ? 'active' : ''}`}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'tasks' && (
        <>
          <form className="filter-bar" onSubmit={(e) => { e.preventDefault(); void reloadTasks() }}>
            <label className="filter-item">
              <span>同步状态</span>
              <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
                <option value="">全部</option>
                {statuses.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </label>
            <label className="filter-item">
              <span>目标节点</span>
              <input value={targetFilter} placeholder="如 WORK-0002" onChange={(e) => setTargetFilter(e.target.value)} />
            </label>
            <button className="btn" type="submit">查询</button>
            <button className="btn ghost" type="button" onClick={() => { setStatusFilter(''); setTargetFilter('') }}>重置</button>
          </form>

          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  {taskColumns.map((c) => <th key={c}>{c}</th>)}
                  <th>可执行动作（按状态机）</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((row) => (
                  <tr key={String(row.id)} className={row.status === '已完成' ? 'row-ok' : row.abnormal ? 'row-abn' : ''}>
                    {taskColumns.map((c) => (
                      <td key={c}>
                        {c === '任务编号'
                          ? <button className="link" type="button" onClick={() => void openDetail(row)}>{String(row[c] ?? '—')}</button>
                          : (row[c] ?? '—')}
                      </td>
                    ))}
                    <td className="row-actions">
                      {availableActions(row).map((action) => (
                        <button
                          key={action}
                          className="link"
                          type="button"
                          disabled={!isAdmin || loading}
                          title={!isAdmin ? '只读角色无维护权限' : action}
                          onClick={() => onActionClick(row, action)}
                        >
                          {action}
                        </button>
                      ))}
                      {availableActions(row).length === 0 && <span className="muted">终态，无可用动作</span>}
                    </td>
                  </tr>
                ))}
                {tasks.length === 0 && (
                  <tr><td className="empty-state" colSpan={taskColumns.length + 1}>暂无符合条件的同步任务</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}

      {tab !== 'tasks' && <DatasetTable tab={tab} rows={dataset} />}

      <footer className="page-foot">
        <span>共 {tab === 'tasks' ? total : dataset.length} 条记录</span>
        {message && <span className={message.ok ? 'ok-text' : 'error-text'}>{message.text}</span>}
      </footer>

      {showCreate && (
        <div className="modal-mask" onClick={() => setShowCreate(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>登记同步任务</h3>
            {Object.keys(createForm).map((field) => (
              <label key={field} className="modal-field">
                <span>{field}</span>
                <input
                  value={createForm[field as keyof typeof createForm]}
                  disabled={field === '源节点'}
                  onChange={(e) => setCreateForm((p) => ({ ...p, [field]: e.target.value }))}
                />
              </label>
            ))}
            <div className="modal-actions">
              <button className="btn" type="button" onClick={() => setShowCreate(false)}>取消</button>
              <button className="btn primary" type="button" disabled={loading} onClick={() => void submitCreate()}>登记</button>
            </div>
          </div>
        </div>
      )}

      {modal && (
        <div className="modal-mask" onClick={() => setModal(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>执行「{modal.action}」</h3>
            <label className="modal-field">
              <span>{PARAM_ACTIONS[modal.action].label}</span>
              <input
                autoFocus
                value={paramValue}
                placeholder={PARAM_ACTIONS[modal.action].placeholder}
                onChange={(e) => setParamValue(e.target.value)}
              />
            </label>
            <div className="modal-actions">
              <button className="btn" type="button" onClick={() => setModal(null)}>取消</button>
              <button className="btn primary" type="button" disabled={loading} onClick={() => void confirmModal()}>确认执行</button>
            </div>
          </div>
        </div>
      )}

      {detail && <DetailDrawer detail={detail} onClose={() => setDetail(null)} />}
    </section>
  )
}

function DatasetTable({ tab, rows }: { tab: TabKey; rows: Json[] }) {
  const columns = useMemo(() => {
    if (rows.length === 0) return [] as string[]
    return Object.keys(rows[0]).filter((k) => k !== 'id')
  }, [rows])
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {columns.map((c) => {
                const v = row[c]
                return <td key={c}>{typeof v === 'object' ? JSON.stringify(v) : String(v ?? '—')}</td>
              })}
            </tr>
          ))}
          {rows.length === 0 && <tr><td className="empty-state" colSpan={1}>暂无数据</td></tr>}
        </tbody>
      </table>
    </div>
  )
}

function DetailDrawer({ detail, onClose }: { detail: Json; onClose: () => void }) {
  const d = detail as Row
  const results = (detail.结果 as Json[]) ?? []
  const audits = (detail.审计 as Json[]) ?? []
  const progress = (detail.应用进度 as Json[]) ?? []
  return (
    <div className="drawer-mask" onClick={onClose}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()}>
        <header className="drawer-head">
          <h3>任务明细 · {String(d.任务编号)}</h3>
          <button className="btn" type="button" onClick={onClose}>关闭</button>
        </header>
        <div className="drawer-body">
          <section>
            <h4>双端版本与握手</h4>
            <ul className="kv-list">
              <li>握手状态：{String(d.握手状态 ?? '—')}</li>
              <li>批次号：{String(d.批次号 ?? '—')}</li>
              <li>源快照版本：{String(d.源快照版本 ?? '—')}</li>
              <li>生效前版本：{String(d.生效前版本 ?? '—')}</li>
              <li>生效后版本：{String(d.生效后版本 ?? '—')}</li>
              <li>差异摘要：{String(d.差异摘要 ?? '—')}</li>
            </ul>
          </section>
          <section>
            <h4>目标应用进度（断点续传依据）</h4>
            {progress.length === 0 ? <p className="muted">尚未握手，无应用进度</p> : progress.map((p) => (
              <ul key={String(p.id)} className="kv-list">
                <li>期望版本：{String(p.期望版本)} / 生效前版本：{String(p.生效前版本)}</li>
                <li>已应用段：{JSON.stringify(p.已应用段)}（共 {String(p.段总数)} 段）</li>
                <li>节点在线：{String(p.节点在线)} / 中断标记：{String(p.中断标记)}</li>
              </ul>
            ))}
          </section>
          <section>
            <h4>配置同步结果（幂等，随容量侧保留）</h4>
            {results.length === 0 ? <p className="muted">暂无完成结果</p> : (
              <table className="data-table compact">
                <thead><tr><th>结果编号</th><th>前后版本</th><th>提交次数</th><th>容量侧回写</th></tr></thead>
                <tbody>
                  {results.map((r) => (
                    <tr key={String(r.id)}>
                      <td>{String(r.结果编号)}</td>
                      <td>{String(r.生效前版本)} → {String(r.生效后版本)}</td>
                      <td>{String(r.提交次数)}</td>
                      <td>{r.容量侧已回写 ? `已回写 ${String(r.容量侧回写时间)}` : '未回写'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
          <section>
            <h4>同步审计（处理痕迹）</h4>
            <table className="data-table compact">
              <thead><tr><th>时间</th><th>动作</th><th>发起人</th><th>前→后版本</th><th>失败原因/未应用项</th></tr></thead>
              <tbody>
                {audits.map((a) => (
                  <tr key={String(a.id)}>
                    <td>{String(a.动作时间)}</td>
                    <td>{String(a.动作)}</td>
                    <td>{String(a.发起人)}（{String(a.角色)}）</td>
                    <td>{String(a.目标前版本)} → {String(a.目标后版本)}</td>
                    <td>{String(a.失败原因 || '—')}{(a.未应用项 as string[])?.length ? `；未应用：${(a.未应用项 as string[]).join('、')}` : ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </div>
      </aside>
    </div>
  )
}
