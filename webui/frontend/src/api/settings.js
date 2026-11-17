import http from './request'

export const getMailProviders = (pooledOnly = false) =>
  http.get('/api/mail/providers', { params: { pooled_only: pooledOnly } })

export const getMailConfig = () => http.get('/api/settings/mail')
export const saveMailConfig = (payload) => http.post('/api/settings/mail', payload)
export const testMail = () => http.post('/api/settings/mail/test')
