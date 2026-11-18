<script setup>
import { nextTick, ref } from 'vue'
import { streamChat } from '../api'

const messages = ref([
  {
    role: 'assistant',
    text: '你好，我是 CodePilot。可以问我代码库实现、规范用法，例如「鉴权是在哪里做的？」\n先在「审查工作台」提交过 PR 后，也可以直接让我审查 diff。',
    citations: [],
    route: null,
  },
])
const input = ref('')
const busy = ref(false)
const error = ref('')
const repoId = ref('')
const bodyEl = ref(null)

function scrollToBottom() {
  nextTick(() => {
    if (bodyEl.value) bodyEl.value.scrollTop = bodyEl.value.scrollHeight
  })
}

async function send() {
  const text = input.value.trim()
  if (!text || busy.value) return
  input.value = ''
  error.value = ''
  busy.value = true

  messages.value.push({ role: 'user', text })
  const assistant = { role: 'assistant', text: '', citations: [], route: null, streaming: true }
  messages.value.push(assistant)
  scrollToBottom()

  try {
    await streamChat({ message: text, repo: 'local/demo', repo_id: repoId.value || null }, (evt) => {
      if (evt.event === 'route') {
        assistant.route = evt.data
      } else if (evt.event === 'citations') {
        assistant.citations = evt.data.chunks || []
      } else if (evt.event === 'token') {
        assistant.text += evt.data.text || ''
      } else if (evt.event === 'result') {
        assistant.result = evt.data
        if (evt.data?.summary) {
          assistant.text += `\n\n审查完成：阻断 ${evt.data.summary.by_severity?.block ?? 0} · ` +
            `警告 ${evt.data.summary.by_severity?.warn ?? 0} · 建议 ${evt.data.summary.by_severity?.info ?? 0}` +
            `${evt.data.summary.mergeable ? '（可合并）' : '（不可合并）'}`
        }
      } else if (evt.event === 'error') {
        error.value = evt.data.message || '生成失败'
      }
      scrollToBottom()
    })
  } catch (e) {
    error.value = e.message || '请求失败'
  } finally {
    assistant.streaming = false
    if (!assistant.text && !assistant.result) {
      assistant.text = '（没有收到内容，请检查后端日志与 /readyz）'
    }
    busy.value = false
    scrollToBottom()
  }
}

/** 极简 Markdown：只把 ``` 代码块渲染成 <pre>，其余保持纯文本（pre-wrap）。 */
function segments(text) {
  const out = []
  let rest = text || ''
  for (;;) {
    const start = rest.indexOf('```')
    if (start < 0) {
      if (rest) out.push({ type: 'text', value: rest })
      return out
    }
    if (start > 0) out.push({ type: 'text', value: rest.slice(0, start) })
    const after = rest.slice(start + 3)
    const end = after.indexOf('```')
    if (end < 0) {
      out.push({ type: 'code', value: after.replace(/^[a-zA-Z]*\n/, '') })
      return out
    }
    out.push({ type: 'code', value: after.slice(0, end).replace(/^[a-zA-Z]*\n/, '') })
    rest = after.slice(end + 3)
  }
}

function copyCite(c) {
  navigator.clipboard?.writeText(`${c.path}:${c.start_line}-${c.end_line}`)
}

function onKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    send()
  }
}
</script>

<template>
  <section class="card chat-card">
    <div class="row" style="justify-content: space-between; margin-bottom: 12px">
      <h3 style="margin: 0">代码问答 <span class="hint">（SSE 流式 + 引用溯源）</span></h3>
      <input v-model="repoId" placeholder="repo_id（可空）" style="width: 280px" />
    </div>

    <div ref="bodyEl" class="chat-body">
      <div v-for="(m, i) in messages" :key="i" class="msg" :class="m.role">
        <div class="avatar">{{ m.role === 'user' ? '我' : '🛫' }}</div>
        <div>
          <div class="bubble">
            <template v-if="m.text">
              <template v-for="(seg, k) in segments(m.text)" :key="k">
                <pre v-if="seg.type === 'code'" class="code-block">{{ seg.value }}</pre>
                <span v-else>{{ seg.value }}</span>
              </template>
            </template>
            <span v-else-if="m.streaming" class="spinner" />
          </div>

          <div v-if="m.route" class="hint" style="margin-top: 5px">
            路由 → {{ m.route.intent }} · {{ m.route.reason }}
          </div>

          <div v-if="m.citations?.length">
            <span v-for="c in m.citations" :key="c.citation" class="cite" @click="copyCite(c)">
              {{ c.path }}:{{ c.start_line }}
            </span>
          </div>
        </div>
      </div>
    </div>

    <p v-if="error" class="error" style="margin-top: 10px">{{ error }}</p>

    <div class="chat-input">
      <textarea v-model="input" placeholder="问点什么…（Enter 发送，Shift+Enter 换行）" @keydown="onKeydown" />
      <button class="btn primary" :disabled="busy" @click="send">
        <span v-if="busy" class="spinner" />
        <span v-else>发送</span>
      </button>
    </div>
  </section>
</template>
