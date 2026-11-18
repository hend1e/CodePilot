<script setup>
import { computed } from 'vue'
import { clearSession, store } from './store'
import LoginView from './views/LoginView.vue'
import ReviewView from './views/ReviewView.vue'
import ChatView from './views/ChatView.vue'
import TasksView from './views/TasksView.vue'

const tabs = [
  { key: 'review', label: '审查工作台' },
  { key: 'chat', label: '代码问答' },
  { key: 'tasks', label: '任务记录' },
]

const loggedIn = computed(() => Boolean(store.token))
const userLabel = computed(() => store.user?.user_id?.slice(0, 8) || 'anonymous')

function logout() {
  clearSession()
  store.tab = 'review'
}
</script>

<template>
  <LoginView v-if="!loggedIn" />

  <div v-else class="shell">
    <header class="topbar">
      <div class="brand">
        <span class="logo">🛫</span>
        <span>CodePilot <small>多 Agent 代码审查</small></span>
      </div>
      <nav class="tabs">
        <button
          v-for="t in tabs"
          :key="t.key"
          :class="{ active: store.tab === t.key }"
          @click="store.tab = t.key"
        >{{ t.label }}</button>
      </nav>
      <div class="spacer" />
      <div class="user">
        <span class="badge muted">{{ store.user?.role || 'developer' }}</span>
        <span>{{ userLabel }}</span>
        <button class="btn ghost" @click="logout">退出</button>
      </div>
    </header>

    <main class="main">
      <ReviewView v-if="store.tab === 'review'" />
      <ChatView v-else-if="store.tab === 'chat'" />
      <TasksView v-else @open-review="store.tab = 'review'" />
    </main>
  </div>
</template>
