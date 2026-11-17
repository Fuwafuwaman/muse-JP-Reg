import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'

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

  watch(list, (v) => {
    try { localStorage.setItem(KEY, JSON.stringify(v)) } catch (_) {}
  }, { deep: true })

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

  return { list, text, count, setFromText, append, remove, clear }
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
