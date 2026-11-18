<script setup>
import { computed, ref } from 'vue'
import { resumeReview } from '../api'

const props = defineProps({
  task: { type: Object, default: null },
  busy: { type: Boolean, default: false },
})
const emit = defineEmits(['resumed'])

const severityFilter = ref('all')
const dimensionFilter = ref('all')
const acting = ref(false)
const actionError = ref('')

const findings = computed(() => {
  const rows = props.task?.findings || props.task?.final?.findings || []
  return rows.filter(
    (f) =>
      (severityFilter.value === 'all' || f.severity === severityFilter.value) &&
      (dimensionFilter.value === 'all' || f.dimension === dimensionFilter.value),
  )
})

const summary = computed(() => {
  const s = props.task?.summary || props.task?.final?.summary
  if (s) return s
  const rows = props.task?.findings || []
  const by = { block: 0, warn: 0, info: 0 }
  rows.forEach((f) => (by[f.severity] = (by[f.severity] || 0) + 1))
  return { total: rows.length, by_severity: by, mergeable: by.block === 0 }
})

const dims = computed(() => {
  const set = new Set((props.task?.findings || []).map((f) => f.dimension))
  return ['all', ...set]
})

const status = computed(() => props.task?.status || 'idle')
const waiting = computed(() => status.value === 'waiting_human')
const testReport = computed(() => props.task?.final?.test_report)
const sevLabel = { block: '阻断', warn: '警告', info: '建议' }
const dimLabel = { security: '安全', performance: '性能', style: '规范', test: '测试' }

async function decide(action) {
  acting.value = true
  actionError.value = ''
  try {
    await resumeReview(props.task.id, action, action === 'approve' ? '前端确认合入' : '前端驳回')
    emit('resumed')
  } catch (e) {
    actionError.value = e?.response?.data?.detail || e.message
  } finally {
    acting.value = false
  }
}
</script>

<template>
  <div v-if="!task" class="card">
    <div class="empty">提交一个 diff，或从「任务记录」里选一条查看报告</div>
  </div>

  <div v-else class="card">
    <div class="report-head">
      <div class="row">
        <span class="badge" :class="status === 'done' ? 'ok' : status === 'failed' ? 'block' : 'muted'">
          <span class="dot" />{{ status }}
        </span>
        <span v-if="summary.mergeable === false" class="badge block">不可合并</span>
        <span v-else-if="status === 'done'" class="badge ok">可合并</span>
        <span v-if="task.degraded || task.final?.degraded" class="badge warn">有降级</span>
      </div>
      <div class="hint">
        成本 {{ Number(task.cost_usd || 0).toFixed(4) }} USD · 步数 {{ task.steps || 0 }}
        <span v-if="task.final?.tokens"> · tokens {{ task.final.tokens.prompt + task.final.tokens.completion }}</span>
      </div>
    </div>

    <div class="meta">
      <span class="badge block">阻断 {{ summary.by_severity?.block || 0 }}</span>
      <span class="badge warn">警告 {{ summary.by_severity?.warn || 0 }}</span>
      <span class="badge info">建议 {{ summary.by_severity?.info || 0 }}</span>
      <span class="badge muted">共 {{ summary.total }} 条</span>
    </div>

    <div v-if="busy || ['queued', 'running'].includes(status)" class="row hint">
      <span class="spinner" /> 正在审查，规则引擎先出结果，LLM 维度稍后合并…
    </div>

    <div v-if="waiting" class="card" style="background: rgba(245,158,11,.08); border-color: rgba(245,158,11,.35)">
      <h3>⏸ 单测已通过沙箱，等待人工确认</h3>
      <p class="hint" style="margin-top: 0">
        沙箱结果：{{ task.final?.hitl?.sandbox?.passed ?? '-' }} passed /
        {{ task.final?.hitl?.sandbox?.failed ?? '-' }} failed，
        覆盖率 {{ task.final?.hitl?.sandbox?.coverage ?? '-' }}%
      </p>
      <div class="row">
        <button class="btn ok" :disabled="acting" @click="decide('approve')">确认合入</button>
        <button class="btn danger" :disabled="acting" @click="decide('reject')">驳回</button>
        <span v-if="acting" class="spinner" />
      </div>
      <p v-if="actionError" class="error" style="margin-top: 10px">{{ actionError }}</p>
    </div>

    <div class="filters">
      <button :class="{ on: severityFilter === 'all' }" @click="severityFilter = 'all'">全部</button>
      <button
        v-for="s in ['block', 'warn', 'info']"
        :key="s"
        :class="{ on: severityFilter === s }"
        @click="severityFilter = s"
      >{{ sevLabel[s] }}</button>
      <span style="width: 1px; background: var(--border); margin: 0 4px" />
      <button
        v-for="d in dims"
        :key="d"
        :class="{ on: dimensionFilter === d }"
        @click="dimensionFilter = d"
      >{{ d === 'all' ? '全维度' : dimLabel[d] || d }}</button>
    </div>

    <div v-if="!findings.length" class="empty">当前筛选下没有问题</div>

    <div v-for="(f, i) in findings" :key="i" class="finding" :class="f.severity">
      <div class="fhead">
        <span class="badge" :class="f.severity">{{ sevLabel[f.severity] }}</span>
        <span class="badge muted">{{ dimLabel[f.dimension] || f.dimension }}</span>
        <span class="loc">{{ f.file_path }}:{{ f.line_start }}</span>
        <span class="badge muted">{{ f.source }}</span>
        <span class="hint">置信度 {{ Number(f.confidence).toFixed(2) }}</span>
      </div>
      <div class="msg">{{ f.message }}</div>
      <div v-if="f.suggestion" class="sug">建议：{{ f.suggestion }}</div>
      <pre v-if="f.evidence">{{ f.evidence }}</pre>
    </div>

    <div v-if="testReport" class="card" style="margin-top: 14px; background: #0f1628">
      <h3>🧪 单测沙箱：{{ testReport.status }}</h3>
      <p class="hint" style="margin: 0">
        {{ testReport.passed }} passed / {{ testReport.failed }} failed ·
        覆盖率 {{ testReport.coverage ?? '—' }}% · {{ testReport.duration_ms }}ms
      </p>
      <pre v-if="testReport.stdout_tail" style="max-height: 220px; overflow: auto; margin-top: 10px;
        padding: 10px; background: #0a1020; border: 1px solid var(--border); border-radius: 8px; font-size: 11.5px">{{ testReport.stdout_tail }}</pre>
    </div>

    <p v-if="task.final?.open_questions?.length" class="hint" style="margin-top: 12px">
      待人工确认：{{ task.final.open_questions.join('；') }}
    </p>
  </div>
</template>
