import http from './request'

export const listInvites = (params) =>
  http.get('/api/invites', { params })

export const getInviteStats = () =>
  http.get('/api/invites/stats')

export const importInvites = (text, maxUses = 30) =>
  http.post('/api/invites/import', { text, max_uses: maxUses })

export const deleteInvites = (payload) =>
  http.post('/api/invites/bulk_delete', payload)

export const resetInvites = (codes = null) =>
  http.post('/api/invites/reset', { codes })
