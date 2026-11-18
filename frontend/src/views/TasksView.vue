<script setup>
import { onMounted, ref } from 'vue'
import { getReview, listReviews } from '../api'
import FindingsPanel from '../components/FindingsPanel.vue'

const items = ref([])
const selected = ref(null)
const loading = ref(false)
const error = ref('')

const statusClass = { done: 'ok', failed: 'block', waiting_human: 'warn', running: 'info', queued: 'muted' }

async function refresh() {
  loading.value = true
  error.value = ''
  try {
    items.value = await listReviews()
  } catch (e) {
    error.value = e?.response?.data?.detail || e.message
  } finally {
    loading.value = false
  }
}

async function open(row) {
  try {
    selected.value = await getReview(row.id)
  } catch (e) {
    error.value = e?.response?.data?.detail || e.message
  }
}

onMounted(refresh)
</script>

<template>
  <div class="grid">
    <section class="card">
      <div class="row" style="justify-content: space-between">
        <h3 style="margin: 0">任务记录</h3>
        <button class="btn ghost" :disabled="loading" @click="refresh">刷新</button>
      </div>

      <p v-if="error" class="error" style="margin-top: 10px">{{ error }}</p>

      <div v-if="!items.length" class="empty">还没有审查记录</div>

      <div class="tasks" style="margin-top: 12px">
        <div v-for="row in items" :key="row.id" class="task-row" @click="open(row)">
          <div class="t-main">
            <div class="t-title">
              {{ row.repo_full_name || 'local' }}
              <span v-if="row.pr_number" class="badge muted">#{{ row.pr_number }}</span>
            </div>
            <div class="t-sub">{{ row.id.slice(0, 8) }} · {{ row.created_at?.replace('T', ' ').slice(0, 19) }}</div>
          </div>
          <span class="badge" :class="statusClass[row.status] || 'muted'">{{ row.status }}</span>
          <span class="hint">${{ Number(row.cost_usd || 0).toFixed(3) }}</span>
        </div>
      </div>
    </section>

    <FindingsPanel :task="selected" @resumed="open(selected)" />
  </div>
</template>
