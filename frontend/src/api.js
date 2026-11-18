// API 层：REST 用 axios（带 JWT 拦截器），SSE 用 fetch 流式读取。
// 为什么不给 SSE 用 EventSource：EventSource 只支持 GET，而 /chat 需要 POST body。
import axios from 'axios'
import { clearSession, store } from './store'

export const http = axios.create({ baseURL: '/api/v1', timeout: 120000 })

http.interceptors.request.use((cfg) => {
  if (store.token) cfg.headers.Authorization = `Bearer ${store.token}`
  return cfg
})

http.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err.response?.status === 401) clearSession()
    return Promise.reject(err)
  },
)

export const login = (username, password) =>
  http.post('/auth/login', { username, password }).then((r) => r.data)

export const createReview = (payload) => http.post('/reviews', payload).then((r) => r.data)
export const getReview = (id) => http.get(`/reviews/${id}`).then((r) => r.data)
export const listReviews = (limit = 30) =>
  http.get('/reviews', { params: { limit } }).then((r) => r.data.items)
export const resumeReview = (id, action, comment = '') =>
  http.post(`/reviews/${id}/resume`, { action, comment }).then((r) => r.data)
export const listRepos = () => http.get('/rag/repos').then((r) => r.data.items)
export const ragQuery = (question, repo_id = null) =>
  http.post('/rag/query', { question, repo_id }).then((r) => r.data)

/** 统一入口 SSE：逐事件回调 {event, data}。 */
export async function streamChat(payload, onEvent) {
  const resp = await fetch('/api/v1/chat', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(store.token ? { Authorization: `Bearer ${store.token}` } : {}),
    },
    body: JSON.stringify(payload),
  })
  if (!resp.ok || !resp.body) {
    throw new Error(`SSE 连接失败：HTTP ${resp.status}`)
  }
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    // sse-starlette 默认用 CRLF 分隔事件，统一成 LF 再按空行切块
    buffer += decoder.decode(value, { stream: true }).replace(/\r/g, '')
    let idx
    while ((idx = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, idx)
      buffer = buffer.slice(idx + 2)
      const parsed = parseSseBlock(block)
      if (parsed) onEvent(parsed)
    }
  }
}

function parseSseBlock(block) {
  let event = 'message'
  const dataLines = []
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
  }
  if (!dataLines.length) return null
  const raw = dataLines.join('\n')
  try {
    return { event, data: JSON.parse(raw) }
  } catch {
    return { event, data: raw }
  }
}
