import http from './request'

// 代理连通性测试（后端并发测试，可能耗时，单独放宽超时到 3 分钟）
export const testProxies = (proxies, timeout = 8) =>
  http.post('/api/proxy/test', { proxies, timeout }, { timeout: 180000 })

// 代理池后端持久化（跨浏览器共享，localStorage 仅作本地缓存）
export const getProxyPool = () => http.get('/api/proxy/pool')
export const saveProxyPool = (proxies) => http.post('/api/proxy/pool', { proxies })
