import { defineStore } from 'pinia'
import { ref } from 'vue'
import { createSSE } from '@/api/request'
import { useStatsStore } from './stats'

let _logId = 0
const MAX_LOGS = 2500

function classify(line) {
  const l = (line || '').toLowerCase()
  if (l.includes('error') || l.includes('❌') || l.includes('失败') || l.includes('拒绝')) return 'err'
  if (l.includes('warning') || l.includes('warn') || l.includes('⚠️') || l.includes('waitlist')) return 'warn'
  if (l.includes('✅') || l.includes('成功') || l.includes('完成') || l.includes('活号') || l.includes('✨')) return 'ok'
  return ''
}

export const useRuntimeStore = defineStore('runtime', () => {
  const logs = ref([])
  const autoStatus = ref({
    state: 'stopped',
    registered_ok: 0,
    registered_waitlist: 0,
    registered_fail: 0,
    ip_switches: 0,
  })
  const banner = ref('')
  const lastRunResult = ref(null)
  const dataVersion = ref(0)
  const runningSingle = ref(false)

  let currentEs = null
  let autoEs = null

  function addLog(text, kind) {
    logs.value.push({ id: ++_logId, text, kind: kind ?? classify(text) })
    if (logs.value.length > MAX_LOGS) logs.value.splice(0, logs.value.length - MAX_LOGS)
  }
  function clearLogs() { logs.value = [] }
  function bumpData() { dataVersion.value++ }
  function dismissBanner() { banner.value = '' }

  function streamRun(runId) {
    if (currentEs) { try { currentEs.close() } catch (_) {} }
    runningSingle.value = true
    const es = createSSE(`/api/runs/${runId}/stream`, {
      log: (e) => {
        try {
          const d = JSON.parse(e.data)
          if (d.line) addLog(d.line)
        } catch (_) {}
      },
      status: (e) => {
        try {
          const d = JSON.parse(e.data)
          if (d.kind === 'done') {
            lastRunResult.value = {
              email: d.email,
              status: d.status,
              stage: d.stage,
              account_id: d.account_id || '',
              own_code: d.own_code || '',
              used_invite_code: d.used_invite_code || '',
              session_token_len: d.session_token_len || 0,
              access_token_len: d.access_token_len || 0,
              ip: d.ip || '',
              last_error: d.last_error || '',
            }
            addLog(
              `✅ 注册完成: ${d.email} [状态=${d.status}]`
              + (d.own_code ? ` 自有邀请码=${d.own_code}` : '')
              + ` (hatch_sess=${d.session_token_len})`,
              'ok',
            )
          } else if (d.kind === 'error') {
            lastRunResult.value = {
              email: d.email,
              error: d.message,
              category: d.category,
              stage: d.stage,
              registered: d.registered,
              status: d.status,
            }
            addLog(
              (d.registered ? '⚠️ 已注册但受限: ' : '❌ 错误: ') + d.message,
              d.registered ? 'warn' : 'err',
            )
          } else if (d.kind === 'phase') {
            addLog(`[阶段 ${d.phase}] ${d.message || d.email}`, 'evt')
          }
        } catch (_) {}
      },
      end: () => {
        try { es.close() } catch (_) {}
        currentEs = null
        runningSingle.value = false
        useStatsStore().refresh()
        bumpData()
      },
    }, () => {
      try { es.close() } catch (_) {}
      currentEs = null
      runningSingle.value = false
    })
    currentEs = es
  }

  function connectAutoStream() {
    if (autoEs) { try { autoEs.close() } catch (_) {} }
    const es = createSSE('/api/auto/stream', {
      state: (e) => {
        try { autoStatus.value = JSON.parse(e.data) } catch (_) {}
      },
      run_started: (e) => {
        try {
          const d = JSON.parse(e.data)
          addLog(`[W${d.worker_id}] 启动注册 ${d.email} (代理=${d.proxy})`, 'evt')
          streamRun(d.run_id)
        } catch (_) {}
      },
      run_finished: (e) => {
        try {
          const d = JSON.parse(e.data)
          let tag = '[失败]'
          let level = 'err'
          if (d.ok) { tag = '[成功]'; level = 'ok' }
          else if (d.category === 'region_waitlist') { tag = '[地区受限保留]'; level = 'warn' }
          addLog(`[W${d.worker_id}] ${tag} ${d.email}`, level)
          useStatsStore().refresh()
          bumpData()
        } catch (_) {}
      },
      circuit_break: (e) => {
        try {
          const d = JSON.parse(e.data)
          addLog(`[批量暂停] ${d.reason}`, 'warn')
          banner.value = d.reason
        } catch (_) {}
      },
    }, () => {
      try { es.close() } catch (_) {}
      autoEs = null
      setTimeout(connectAutoStream, 2000)
    })
    autoEs = es
  }

  return {
    logs, autoStatus, banner, lastRunResult, dataVersion, runningSingle,
    addLog, clearLogs, bumpData, dismissBanner, streamRun, connectAutoStream,
  }
})
