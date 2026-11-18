<script setup>
import { onBeforeUnmount, ref } from 'vue'
import { createReview, getReview } from '../api'
import FindingsPanel from '../components/FindingsPanel.vue'

const SAMPLE = `diff --git a/app/api/routes.py b/app/api/routes.py
--- a/app/api/routes.py
+++ b/app/api/routes.py
@@ -1,3 +1,14 @@
 from fastapi import APIRouter
+
+router = APIRouter()
+
+
+@router.get("/admin/users")
+def list_users():
+    conn = get_conn()
+    for uid in uid_list:
+        conn.execute(f"SELECT * FROM users WHERE id = {uid}")
+    return {"users": []}
diff --git a/tests/test_routes.py b/tests/test_routes.py
new file mode 100644
--- /dev/null
+++ b/tests/test_routes.py
@@ -0,0 +1,4 @@
+import pytest
+
+@pytest.mark.skip(reason="later")
+def test_list_users():
+    list_users()
`

const repo = ref('local/demo')
const prNumber = ref(1)
const prTitle = ref('')
const localPath = ref('')
const diff = ref('')
const loading = ref(false)
const error = ref('')
const taskId = ref('')
const task = ref(null)
let timer = null

function stopPolling() {
  if (timer) clearInterval(timer)
  timer = null
}

async function poll() {
  if (!taskId.value) return
  try {
    task.value = await getReview(taskId.value)
    if (['done', 'failed', 'waiting_human', 'cancelled', 'superseded'].includes(task.value.status)) {
      stopPolling()
    }
  } catch (e) {
    error.value = e?.response?.data?.detail || e.message
    stopPolling()
  }
}

async function submit() {
  error.value = ''
  if (!diff.value.trim()) {
    error.value = '请粘贴 unified diff（可点「填入示例」）'
    return
  }
  loading.value = true
  stopPolling()
  try {
    const payload = {
      repo: repo.value.trim() || 'local/demo',
      diff: diff.value,
      pr_number: prNumber.value || null,
      pr_title: prTitle.value,
      local_path: localPath.value.trim() || null,
    }
    const created = await createReview(payload)
    taskId.value = created.task_id
    task.value = { id: created.task_id, status: created.status, findings: [] }
    timer = setInterval(poll, 2000)
    poll()
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.response?.data?.error?.message || e.message
  } finally {
    loading.value = false
  }
}

function fillSample() {
  diff.value = SAMPLE
  prTitle.value = '示例：管理端接口'
  repo.value = 'local/demo'
}

onBeforeUnmount(stopPolling)
</script>

<template>
  <div class="grid">
    <section class="card">
      <h3>提交审查</h3>
      <div class="row" style="align-items: flex-start">
        <label class="field" style="flex: 2">
          <span>仓库</span>
          <input v-model="repo" placeholder="owner/repo 或 local/demo" />
        </label>
        <label class="field" style="flex: 1">
          <span>PR 编号</span>
          <input v-model.number="prNumber" type="number" min="1" />
        </label>
      </div>

      <label class="field">
        <span>PR 标题（可选）</span>
        <input v-model="prTitle" placeholder="feat: xxx" />
      </label>

      <label class="field">
        <span>本地仓库路径（可选，沙箱执行单测时需要；必须在 REPO_LOCAL_ROOTS 白名单内）</span>
        <input v-model="localPath" placeholder="./workspace/demo-repo" />
      </label>

      <label class="field">
        <span>Diff</span>
        <textarea v-model="diff" spellcheck="false" placeholder="粘贴 git diff 输出…" />
      </label>

      <p v-if="error" class="error">{{ error }}</p>

      <div class="row">
        <button class="btn primary" :disabled="loading" @click="submit">
          <span v-if="loading" class="spinner" />
          <span v-else>开始审查</span>
        </button>
        <button class="btn ghost" @click="fillSample">填入示例</button>
        <span class="hint" v-if="taskId">任务 <code>{{ taskId.slice(0, 8) }}</code></span>
      </div>
    </section>

    <FindingsPanel :task="task" :busy="loading" @resumed="poll" />
  </div>
</template>
