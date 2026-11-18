<script setup>
import { ref } from 'vue'
import { login } from '../api'
import { setSession } from '../store'

const username = ref('codepilot')
const password = ref('codepilot123')
const loading = ref(false)
const error = ref('')

async function submit() {
  error.value = ''
  loading.value = true
  try {
    const data = await login(username.value.trim(), password.value)
    setSession(data.access_token, { user_id: data.user_id, role: data.role })
  } catch (e) {
    error.value = e?.response?.data?.detail || e.message || '登录失败'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-wrap">
    <div class="card login-card">
      <div class="hero">
        <div class="logo-big">🛫</div>
        <h1>CodePilot</h1>
        <p>PR 四维并行审查 · 代码库 RAG · 沙箱单测</p>
      </div>

      <form @submit.prevent="submit">
        <label class="field">
          <span>用户名 / 邮箱</span>
          <input v-model="username" autocomplete="username" placeholder="codepilot" />
        </label>
        <label class="field">
          <span>密码</span>
          <input v-model="password" type="password" autocomplete="current-password" placeholder="••••••••" />
        </label>

        <p v-if="error" class="error">{{ error }}</p>

        <button class="btn primary" style="width: 100%; margin-top: 6px" :disabled="loading">
          <span v-if="loading" class="spinner" />
          <span v-else>登录</span>
        </button>
      </form>

      <p class="hint" style="margin-top: 14px; text-align: center">
        演示账号：<code>codepilot / codepilot123</code><br />
        首次使用先执行 <code>python -m scripts.seed_data</code>
      </p>
    </div>
  </div>
</template>
