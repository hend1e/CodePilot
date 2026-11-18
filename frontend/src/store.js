// 全局轻量状态：token / 用户 / 当前页签。够用就不引 Pinia。
import { reactive } from 'vue'

export const store = reactive({
  token: localStorage.getItem('cp_token') || '',
  user: JSON.parse(localStorage.getItem('cp_user') || 'null'),
  tab: 'review',
})

export function setSession(token, user) {
  store.token = token
  store.user = user
  localStorage.setItem('cp_token', token)
  localStorage.setItem('cp_user', JSON.stringify(user))
}

export function clearSession() {
  store.token = ''
  store.user = null
  localStorage.removeItem('cp_token')
  localStorage.removeItem('cp_user')
}
