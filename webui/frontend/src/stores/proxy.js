import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import { t } from '@/i18n'
import { getProxyPool, saveProxyPool } from '@/api/proxy'

const KEY = 'muse_proxy_pool_v1'

function parseLines(s) {
  return String(s || '').split('\n').map((x) => x.trim()).filter((x) => x && !x.startsWith('#'))
}
function dedup(arr) {
  return [...new Set(arr)]
}

export const useProxyStore = defineStore('proxy', () => {
  let saved = []
  try { saved = JSON.parse(localStorage.getItem(KEY) || '[]') } catch (_) { saved = [] }

  const list = ref(saved)
  const text = computed(() => list.value.join('\n'))
  const count = computed(() => list.value.length)

  // 后端（settings.proxy_pool）才是真源：换浏览器/清缓存后仍能拿到同一份池。
  // 本地 localStorage 只作为首屏缓存与离线兜底。
  let hydrating = false
  let pushTimer = null
  const pushToServer = (v) => {
    if (pushTimer) clearTimeout(pushTimer)
    pushTimer = setTimeout(() => {
      saveProxyPool(v).catch(() => {})
    }, 400)
  }

  watch(list, (v) => {
    try { localStorage.setItem(KEY, JSON.stringify(v)) } catch (_) {}
    if (!hydrating) pushToServer(v)
  }, { deep: true })

  async function syncFromServer() {
    let remote = null
    try {
      const res = await getProxyPool()
      remote = dedup(Array.isArray(res?.proxies) ? res.proxies : [])
    } catch (_) {
      return
    }
    if (remote.length) {
      if (remote.join('\n') === list.value.join('\n')) return
      hydrating = true
      list.value = remote
      await Promise.resolve()
      hydrating = false
    } else if (list.value.length) {
      // 旧版本只有 localStorage，首次访问时把本地池迁移到后端
      pushToServer(list.value)
    }
  }

  function setFromText(s) {
    const parsed = parseLines(s)
    const unique = dedup(parsed)
    list.value = unique
    return { total: parsed.length, kept: unique.length, duplicated: parsed.length - unique.length }
  }

  function append(s) {
    const merged = dedup([...list.value, ...parseLines(s)])
    const added = merged.length - list.value.length
    list.value = merged
    return { added }
  }

  function remove(proxy) {
    list.value = list.value.filter((x) => x !== proxy)
  }
  function clear() {
    list.value = []
  }

  // 首次实例化时与后端对齐（后端为空且本地有内容则做一次性迁移）
  syncFromServer()

  return { list, text, count, setFromText, append, remove, clear, syncFromServer }
})

/**
 * 兼容：
 * 1) [scheme://][user:pass@]host:port
 * 2) host:port:user:pass（muse-protocol 四段格式）
 */
export function isValidProxy(p) {
  const s = String(p || '').trim()
  if (!s) return false
  if (/^((socks5h?|socks4|https?):\/\/)?[^:\s]+:\d+$/i.test(s)) return true
  if (/^((socks5h?|socks4|https?):\/\/)?\S+@[^:\s]+:\d+$/i.test(s)) return true
  const parts = s.split(':')
  if (parts.length === 4 && /^\d+$/.test(parts[1])) return true
  return false
}

export function proxyScheme(p) {
  const s = String(p || '').trim()
  const m = /^(socks5h?|socks4|https?):\/\//i.exec(s)
  if (m) return m[1].toLowerCase()
  const parts = s.split(':')
  if (parts.length === 4 && /^\d+$/.test(parts[1])) return 'http(四段认证)'
  return 'http(默认)'
}

/** 本地化后的协议标签（'http(四段认证)' / 'http(默认)' 走词典） */
export function proxySchemeLabel(p) {
  return t(proxyScheme(p))
}
