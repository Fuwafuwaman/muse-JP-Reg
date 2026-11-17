import http from './request'

export const getStats = () => http.get('/api/stats')

export const importAccounts = (text, kind = '') =>
  http.post('/api/import', { text, kind })

export const syncLocalIcloud = (limit = 0) =>
  http.post('/api/import/icloud_local', { limit })

export const listAccounts = (params) =>
  http.get('/api/accounts', { params })

export const deleteAccount = (email) =>
  http.delete(`/api/accounts/${encodeURIComponent(email)}`)

export const bulkDeleteAccounts = (payload) =>
  http.post('/api/accounts/bulk_delete', payload)

export const resetFailed = () => http.post('/api/accounts/reset_failed')

export const resetAccount = (email) =>
  http.post(`/api/accounts/reset/${encodeURIComponent(email)}`)

export const bulkResetAccounts = (emails) =>
  http.post('/api/accounts/bulk_reset', { emails })

export const releaseStale = () => http.post('/api/accounts/release_stale')
