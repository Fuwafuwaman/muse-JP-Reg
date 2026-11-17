import http from './request'

export const startRegister = (payload) => http.post('/api/register', payload)

export const listRuns = (limit = 50) => http.get('/api/runs', { params: { limit } })

export const listRegistered = (params) =>
  http.get('/api/registered', { params })

export const getRegistered = (email) =>
  http.get(`/api/registered/${encodeURIComponent(email)}`)

export const deleteRegistered = (email) =>
  http.delete(`/api/registered/${encodeURIComponent(email)}`)

export const bulkDeleteRegistered = (payload) =>
  http.post('/api/registered/bulk_delete', payload)

export { bulkDeleteAccounts } from './accounts'

export const listExportFormats = () => http.get('/api/registered/export/formats')
export const exportRegistered = (payload) => http.post('/api/registered/export', payload)

export const checkLive = (payload) =>
  http.post('/api/registered/check_live', payload)

export const redeemRegistered = (payload) =>
  http.post('/api/registered/redeem', payload)

export const autoStart = (payload) => http.post('/api/auto/start', payload)
export const autoPause = () => http.post('/api/auto/pause')
export const autoResume = () => http.post('/api/auto/resume')
export const autoStop = () => http.post('/api/auto/stop')
export const autoStatus = () => http.get('/api/auto/status')
