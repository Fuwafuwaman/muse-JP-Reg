import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getStats } from '@/api/accounts'

export const useStatsStore = defineStore('stats', () => {
  const stats = ref({ total: 0, available: 0, in_use: 0, done: 0, failed: 0 })
  const invites = ref({ total: 0, available: 0, exhausted: 0, invalid: 0, remaining_uses: 0 })
  const registered = ref({ total: 0, redeemed: 0, activated: 0, waitlist: 0, own_codes: 0 })
  let timer = null

  async function refresh() {
    try {
      const res = await getStats()
      if (res.stats) stats.value = res.stats
      if (res.invites) invites.value = res.invites
      if (res.registered) registered.value = res.registered
    } catch (e) {
      console.error('stats refresh:', e)
    }
  }

  function startPolling(interval = 5000) {
    refresh()
    if (timer) clearInterval(timer)
    timer = setInterval(refresh, interval)
  }

  function stopPolling() {
    if (timer) clearInterval(timer)
    timer = null
  }

  return { stats, invites, registered, refresh, startPolling, stopPolling }
})
