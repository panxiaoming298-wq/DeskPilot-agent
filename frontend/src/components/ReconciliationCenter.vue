<script setup lang="ts">
import { ref, watch } from 'vue'

import { useReconciliationCenter } from '../composables/useReconciliationCenter'
import type { GraphRecoveryAction, ReconciliationOutcome, Task } from '../types'

const props = withDefaults(defineProps<{
  activeTaskId?: string | null
  taskSwitchLocked?: boolean
}>(), {
  activeTaskId: null,
  taskSwitchLocked: false,
})

const emit = defineEmits<{
  openTask: [task: Task]
}>()

const {
  tasks,
  taskTotal,
  taskOffset,
  taskFilter,
  reconciliations,
  reconciliationFilter,
  selected,
  loading,
  refreshing,
  activeAction,
  message,
  error,
  canPreviousTasks,
  canNextTasks,
  reload,
  selectReconciliation,
  refreshSelectedEvidence,
  resolveSelected,
  createAttempt,
  createCompensation,
  recoverGraph,
  taskForNavigation,
  previousTasks,
  nextTasks,
} = useReconciliationCenter()

const outcome = ref<ReconciliationOutcome>('accepted_unknown')
const evidenceSummary = ref('')
const confirmation = ref<
  'resolve' | 'attempt' | 'compensation' | 'recover_continue' | 'recover_terminate' | null
>(null)

const outcomeLabels: Record<ReconciliationOutcome, string> = {
  confirmed_succeeded: '已确认成功',
  confirmed_failed: '已确认失败（不证明无副作用）',
  confirmed_no_effect: '已确认未产生效果',
  accepted_unknown: '接受无法查明',
}

const taskStatusLabels: Record<Task['status'], string> = {
  created: '已创建',
  classifying: '正在分类',
  running: '执行中',
  waiting_approval: '等待审批',
  waiting_reconciliation: '等待对账恢复',
  succeeded: '已完成',
  failed: '已失败',
  cancelled: '已取消',
  paused: '已暂停',
}

function formattedTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(date)
}

function shortId(value: string): string {
  return value.length <= 24 ? value : `${value.slice(0, 12)}…${value.slice(-7)}`
}

async function openTask(taskId: string): Promise<void> {
  if (props.taskSwitchLocked && taskId !== props.activeTaskId) return
  const task = await taskForNavigation(taskId)
  if (task) emit('openTask', task)
}

async function submitResolution(): Promise<void> {
  if (confirmation.value !== 'resolve') {
    confirmation.value = 'resolve'
    return
  }
  if (await resolveSelected(outcome.value, evidenceSummary.value)) {
    confirmation.value = null
    evidenceSummary.value = ''
  }
}

async function submitAttempt(): Promise<void> {
  if (confirmation.value !== 'attempt') {
    confirmation.value = 'attempt'
    return
  }
  const task = await createAttempt()
  if (task) emit('openTask', task)
}

async function submitCompensation(): Promise<void> {
  if (confirmation.value !== 'compensation') {
    confirmation.value = 'compensation'
    return
  }
  const task = await createCompensation()
  if (task) emit('openTask', task)
}

async function submitGraphRecovery(action: GraphRecoveryAction): Promise<void> {
  const confirmationAction = action === 'continue'
    ? 'recover_continue'
    : 'recover_terminate'
  if (confirmation.value !== confirmationAction) {
    confirmation.value = confirmationAction
    return
  }
  const task = await recoverGraph(action)
  if (task) emit('openTask', task)
}

watch(
  () => selected.value?.reconciliation_id ?? null,
  () => {
    confirmation.value = null
    outcome.value = 'accepted_unknown'
    evidenceSummary.value = ''
  },
)
</script>

<template>
  <section class="center-shell" aria-labelledby="reconciliation-center-title">
    <header class="center-header">
      <div>
        <span class="eyebrow">DURABLE HISTORY &amp; RECONCILIATION</span>
        <h2 id="reconciliation-center-title">任务历史与集中对账</h2>
        <p>查看持久化任务、Runner 证据、人工裁决与后继任务血缘。</p>
      </div>
      <button
        class="inline-button"
        data-testid="reload-reconciliation-center"
        type="button"
        :disabled="loading || activeAction !== null || refreshing"
        @click="reload"
      >
        {{ loading ? '正在加载…' : '刷新中心' }}
      </button>
    </header>

    <div class="center-feedback" aria-live="polite">
      <p v-if="message" class="center-success" role="status">{{ message }}</p>
      <p v-if="error" class="center-error" role="alert">{{ error }}</p>
    </div>

    <div class="center-grid" :aria-busy="loading">
      <aside class="history-column">
        <section class="center-panel">
          <div class="panel-heading">
            <div><span class="eyebrow">TASK HISTORY</span><h3>最近任务</h3></div>
            <span>{{ taskTotal }}</span>
          </div>
          <label class="center-filter">
            <span>状态</span>
            <select v-model="taskFilter" data-testid="task-history-filter" :disabled="loading">
              <option value="all">全部</option>
              <option value="waiting_approval">等待审批</option>
              <option value="waiting_reconciliation">等待对账恢复</option>
              <option value="succeeded">已完成</option>
              <option value="failed">已失败</option>
              <option value="cancelled">已取消</option>
              <option value="paused">已暂停</option>
            </select>
          </label>
          <div v-if="tasks.length" class="history-list">
            <button
              v-for="item in tasks"
              :key="item.task_id"
              class="history-item"
              type="button"
              :disabled="activeAction !== null || (taskSwitchLocked && item.task_id !== activeTaskId)"
              :title="taskSwitchLocked && item.task_id !== activeTaskId ? '当前活动任务结束前不能切换' : ''"
              @click="openTask(item.task_id)"
            >
              <span class="history-title">{{ item.goal }}</span>
              <span class="history-meta">
                <b :data-status="item.status">{{ taskStatusLabels[item.status] }}</b>
                <time>{{ formattedTime(item.created_at) }}</time>
              </span>
              <code>{{ shortId(item.task_id) }}</code>
            </button>
          </div>
          <p v-else class="center-empty">当前筛选下没有任务。</p>
          <div class="pager">
            <button type="button" :disabled="!canPreviousTasks || loading" @click="previousTasks">上一页</button>
            <span>{{ taskOffset + 1 }}–{{ Math.min(taskOffset + tasks.length, taskTotal) }}</span>
            <button type="button" :disabled="!canNextTasks || loading" @click="nextTasks">下一页</button>
          </div>
        </section>

        <section class="center-panel reconciliation-list-panel">
          <div class="panel-heading">
            <div><span class="eyebrow">UNKNOWN CALLS</span><h3>Reconciliation</h3></div>
            <span>{{ reconciliations.length }}</span>
          </div>
          <label class="center-filter">
            <span>裁决状态</span>
            <select v-model="reconciliationFilter" data-testid="reconciliation-filter" :disabled="loading">
              <option value="all">全部</option>
              <option value="pending">等待裁决</option>
              <option value="resolved">已裁决</option>
            </select>
          </label>
          <div v-if="reconciliations.length" class="reconciliation-list">
            <button
              v-for="item in reconciliations"
              :key="item.reconciliation_id"
              type="button"
              :class="{ selected: selected?.reconciliation_id === item.reconciliation_id }"
              :disabled="activeAction !== null || refreshing"
              @click="selectReconciliation(item.reconciliation_id)"
            >
              <span><b>{{ item.tool_name }}@{{ item.tool_version }}</b><small>{{ formattedTime(item.unknown_at) }}</small></span>
              <em :data-status="item.status">{{ item.status === 'pending' ? '待裁决' : '已裁决' }}</em>
            </button>
          </div>
          <p v-else class="center-empty">当前筛选下没有 unknown 对账记录。</p>
        </section>
      </aside>

      <article class="center-panel reconciliation-detail">
        <template v-if="selected">
          <div class="panel-heading detail-heading">
            <div>
              <span class="eyebrow">SELECTED RECONCILIATION</span>
              <h3>{{ selected.tool_name }}@{{ selected.tool_version }}</h3>
            </div>
            <em :data-status="selected.status">{{ selected.status === 'pending' ? '等待人工裁决' : '已裁决' }}</em>
          </div>

          <div class="ledger-invariant">
            <strong>原 Tool 账本始终保持 unknown</strong>
            <p>人工 outcome、新 attempt 和 compensation 都是独立事实，不会覆盖原 call。</p>
          </div>

          <dl class="detail-facts">
            <div><dt>Reconciliation</dt><dd><code>{{ selected.reconciliation_id }}</code></dd></div>
            <div><dt>Call</dt><dd><code>{{ selected.call_id }}</code></dd></div>
            <div><dt>调用错误</dt><dd><code>{{ selected.call_error_code ?? '未记录' }}</code></dd></div>
            <div><dt>Runner</dt><dd><code>{{ selected.runner_id ?? '未记录' }}</code></dd></div>
          </dl>

          <section class="detail-section">
            <div class="detail-section-heading">
              <div><h3>Runner 证据</h3><span>{{ selected.receipt_evidence.length }} 条</span></div>
              <button
                class="inline-button"
                data-testid="center-refresh-evidence"
                type="button"
                :disabled="refreshing || activeAction !== null"
                @click="refreshSelectedEvidence"
              >{{ refreshing ? '查询中…' : '查询 Runner' }}</button>
            </div>
            <ol v-if="selected.receipt_evidence.length" class="evidence-list">
              <li v-for="item in selected.receipt_evidence" :key="item.evidence_id" :data-kind="item.kind">
                <div><strong>{{ item.kind }}</strong><time>{{ formattedTime(item.observed_at) }}</time></div>
                <code v-if="item.commit_receipt">{{ shortId(item.commit_receipt.receipt_id) }}</code>
                <code v-else-if="item.error_code">{{ item.error_code }}</code>
                <span v-else>当前 journal 无 committed receipt；该结果不证明无副作用。</span>
              </li>
            </ol>
            <p v-else class="center-empty">尚未保存 Runner 查询证据。</p>
          </section>

          <section class="detail-section lineage-section">
            <h3>任务血缘</h3>
            <div class="lineage-actions">
              <button type="button" :disabled="taskSwitchLocked && selected.task_id !== activeTaskId" @click="openTask(selected.task_id)">打开原任务</button>
              <button v-if="selected.new_attempt_task_id" type="button" :disabled="taskSwitchLocked && selected.new_attempt_task_id !== activeTaskId" @click="openTask(selected.new_attempt_task_id)">打开新 attempt</button>
              <button v-if="selected.compensation_task_id" type="button" :disabled="taskSwitchLocked && selected.compensation_task_id !== activeTaskId" @click="openTask(selected.compensation_task_id)">打开 compensation</button>
            </div>
          </section>

          <section v-if="selected.status === 'pending'" class="detail-section resolution-section">
            <h3>提交不可改写的人工裁决</h3>
            <label>
              <span>Outcome</span>
              <select v-model="outcome" data-testid="reconciliation-outcome" :disabled="activeAction !== null">
                <option v-for="(label, value) in outcomeLabels" :key="value" :value="value">{{ label }}</option>
              </select>
            </label>
            <label>
              <span>证据摘要</span>
              <textarea v-model="evidenceSummary" data-testid="reconciliation-summary" rows="4" maxlength="2000" placeholder="记录核对的外部事实，不要粘贴密钥或敏感日志。" />
            </label>
            <p v-if="confirmation === 'resolve'" class="action-confirm" role="alert">
              裁决提交后不可改写。请再次点击确认。
            </p>
            <div class="action-row">
              <button
                class="primary-button"
                data-testid="resolve-reconciliation"
                type="button"
                :disabled="!evidenceSummary.trim() || activeAction !== null"
                @click="submitResolution"
              >{{ activeAction === 'resolve' ? '提交中…' : confirmation === 'resolve' ? '确认不可改写裁决' : '提交裁决' }}</button>
              <button v-if="confirmation === 'resolve'" type="button" @click="confirmation = null">取消</button>
            </div>
          </section>

          <section v-if="selected.can_create_attempt || selected.can_create_compensation" class="detail-section successor-section">
            <h3>显式后继任务</h3>
            <p>后继任务使用新 task/call/幂等键和新授权；不会重放原 unknown call。</p>
            <p v-if="confirmation === 'attempt'" class="action-confirm" role="alert">仅在已确认原调用无任何效果时创建新 attempt。</p>
            <p v-if="confirmation === 'compensation'" class="action-confirm" role="alert">反向路径将由服务端从 committed receipt 派生，创建后仍需新审批。</p>
            <div class="action-row">
              <button v-if="selected.can_create_attempt" data-testid="create-center-attempt" type="button" :disabled="activeAction !== null" @click="submitAttempt">
                {{ activeAction === 'attempt' ? '创建中…' : confirmation === 'attempt' ? '确认创建 attempt' : '创建新 attempt' }}
              </button>
              <button v-if="selected.can_create_compensation" data-testid="create-center-compensation" type="button" :disabled="activeAction !== null" @click="submitCompensation">
                {{ activeAction === 'compensation' ? '创建中…' : confirmation === 'compensation' ? '确认创建 compensation' : '创建 compensation' }}
              </button>
              <button v-if="confirmation === 'attempt' || confirmation === 'compensation'" type="button" @click="confirmation = null">取消</button>
            </div>
          </section>

          <section v-if="selected.status === 'resolved'" class="detail-section resolved-section">
            <h3>已持久化裁决</h3>
            <strong>{{ selected.outcome ? outcomeLabels[selected.outcome] : '未知 outcome' }}</strong>
            <p>{{ selected.evidence_summary }}</p>
            <small>{{ selected.resolved_by }} · {{ selected.resolved_at ? formattedTime(selected.resolved_at) : '未记录时间' }}</small>
          </section>

          <section v-if="selected.graph_recovery_status !== 'not_applicable'" class="detail-section successor-section">
            <h3>原 effect graph 恢复</h3>
            <p v-if="selected.graph_recovery_status === 'pending'">
              Tool ledger 仍为 unknown。请明确选择按已持久化裁决推进原图，或终止原图。
            </p>
            <p v-else>
              已提交 {{ selected.graph_recovery_action === 'continue' ? '继续原图' : '终止原图' }} 命令。
            </p>
            <p v-if="confirmation === 'recover_continue'" class="action-confirm" role="alert">
              继续操作只消费 reconciliation 事实，不会把原 unknown call 改写为成功或失败。
            </p>
            <p v-if="confirmation === 'recover_terminate'" class="action-confirm" role="alert">
              终止后原图保持可审计，不再调度后继节点。
            </p>
            <div v-if="selected.graph_recovery_status === 'pending'" class="action-row">
              <button
                data-testid="recover-graph-continue"
                type="button"
                :disabled="activeAction !== null || !['confirmed_succeeded', 'confirmed_no_effect'].includes(selected.outcome ?? '')"
                @click="submitGraphRecovery('continue')"
              >{{ activeAction === 'recover_continue' ? '恢复中…' : confirmation === 'recover_continue' ? '确认按裁决恢复' : '按裁决恢复原图' }}</button>
              <button
                class="danger-button"
                data-testid="recover-graph-terminate"
                type="button"
                :disabled="activeAction !== null"
                @click="submitGraphRecovery('terminate')"
              >{{ activeAction === 'recover_terminate' ? '终止中…' : confirmation === 'recover_terminate' ? '确认终止原图' : '终止原图' }}</button>
              <button v-if="confirmation === 'recover_continue' || confirmation === 'recover_terminate'" type="button" @click="confirmation = null">取消</button>
            </div>
          </section>
        </template>
        <div v-else class="detail-empty">选择一条 Reconciliation 查看证据、裁决与血缘。</div>
      </article>
    </div>
  </section>
</template>

<style scoped>
.center-shell { display: grid; gap: 1rem; }
.center-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; padding: 1.25rem; border: 1px solid rgb(128 148 197 / 17%); border-radius: 1rem; background: linear-gradient(140deg, rgb(19 28 50 / 90%), rgb(10 16 30 / 94%)); }
.center-header h2 { margin-top: 0.35rem; }
.center-header p { margin: 0.5rem 0 0; color: #7f8aa2; font-size: 0.75rem; line-height: 1.6; }
.center-feedback:empty { display: none; }
.center-feedback p { margin: 0; padding: 0.65rem 0.8rem; border-radius: 0.6rem; font-size: 0.7rem; }
.center-success { color: #8edbc0; background: rgb(24 92 72 / 18%); }
.center-error { color: #f0a1a3; background: rgb(110 35 42 / 18%); }
.center-grid { display: grid; grid-template-columns: minmax(17rem, 0.72fr) minmax(28rem, 1.28fr); gap: 1rem; }
.history-column { display: grid; align-content: start; gap: 1rem; min-width: 0; }
.center-panel { min-width: 0; padding: 1rem; border: 1px solid rgb(128 148 197 / 15%); border-radius: 0.9rem; background: rgb(12 18 33 / 84%); }
.panel-heading, .detail-section-heading { display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; }
.panel-heading h3 { margin-top: 0.25rem; color: #dce3f2; }
.panel-heading > span, .detail-section-heading span { color: #6f7d97; font-size: 0.65rem; }
.center-filter { display: grid; gap: 0.3rem; margin: 0.8rem 0; color: #74819a; font-size: 0.6rem; }
.center-filter select { width: 100%; }
.history-list, .reconciliation-list { display: grid; gap: 0.4rem; }
.history-item, .reconciliation-list button { width: 100%; border: 1px solid rgb(126 145 186 / 11%); border-radius: 0.6rem; background: rgb(7 11 20 / 42%); text-align: left; }
.history-item { display: grid; gap: 0.35rem; padding: 0.65rem; }
.history-item:hover:not(:disabled), .reconciliation-list button:hover:not(:disabled), .reconciliation-list button.selected { border-color: rgb(109 142 226 / 38%); background: rgb(37 57 112 / 20%); }
.history-title { overflow: hidden; color: #cbd4e6; font-size: 0.7rem; text-overflow: ellipsis; white-space: nowrap; }
.history-meta { display: flex; justify-content: space-between; gap: 0.5rem; color: #65728a; font-size: 0.58rem; }
.history-meta b { color: #8da1c9; font-weight: 650; }
.history-meta b[data-status="waiting_approval"] { color: #e5bd73; }
.history-meta b[data-status="waiting_reconciliation"] { color: #c4b5fd; }
.history-meta b[data-status="succeeded"] { color: #80d9b9; }
.history-meta b[data-status="failed"], .history-meta b[data-status="cancelled"] { color: #df9295; }
.history-item code { overflow: hidden; color: #56637b; font-size: 0.55rem; text-overflow: ellipsis; }
.pager { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; margin-top: 0.75rem; }
.pager button, .lineage-actions button, .action-row > button:not(.primary-button) { padding: 0.45rem 0.65rem; border: 1px solid rgb(126 145 186 / 17%); border-radius: 0.5rem; background: rgb(20 29 49 / 80%); color: #9caac3; font-size: 0.6rem; }
.pager span { color: #64718a; font-size: 0.58rem; }
.reconciliation-list button { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; padding: 0.65rem; }
.reconciliation-list button span { display: grid; gap: 0.25rem; min-width: 0; }
.reconciliation-list b { overflow: hidden; color: #b8c3d8; font-size: 0.65rem; text-overflow: ellipsis; }
.reconciliation-list small { color: #607089; font-size: 0.55rem; }
.reconciliation-list em, .detail-heading em { flex: 0 0 auto; color: #e4bc75; font-size: 0.56rem; font-style: normal; }
.reconciliation-list em[data-status="resolved"], .detail-heading em[data-status="resolved"] { color: #84cdb3; }
.reconciliation-detail { display: grid; align-content: start; gap: 0.85rem; }
.ledger-invariant, .detail-section { padding: 0.8rem; border-radius: 0.65rem; background: rgb(7 11 20 / 43%); }
.ledger-invariant strong { color: #d2daea; font-size: 0.68rem; }
.ledger-invariant p, .successor-section > p, .resolved-section p { margin: 0.35rem 0 0; color: #7f8ca4; font-size: 0.62rem; line-height: 1.55; }
.detail-facts { display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem; margin: 0; }
.detail-facts div { min-width: 0; padding: 0.65rem; border-radius: 0.55rem; background: rgb(7 11 20 / 40%); }
.detail-facts dt, .detail-facts dd { margin: 0; }
.detail-facts dt { color: #65728a; font-size: 0.56rem; }
.detail-facts dd { margin-top: 0.25rem; overflow-wrap: anywhere; color: #a8b4c9; font-size: 0.6rem; }
.detail-section { display: grid; gap: 0.65rem; }
.detail-section h3 { color: #cbd4e5; }
.evidence-list { display: grid; gap: 0.4rem; margin: 0; padding: 0; list-style: none; }
.evidence-list li { display: grid; gap: 0.3rem; padding: 0.6rem; border-left: 2px solid #6a7891; border-radius: 0.5rem; background: rgb(10 16 29 / 70%); }
.evidence-list li[data-kind="commit_receipt"] { border-color: #3dba91; }
.evidence-list li[data-kind="query_failed"] { border-color: #d4696d; }
.evidence-list li > div { display: flex; justify-content: space-between; gap: 0.5rem; }
.evidence-list strong, .evidence-list time, .evidence-list code, .evidence-list span { font-size: 0.56rem; }
.evidence-list strong { color: #aebbd1; }
.evidence-list time, .evidence-list span { color: #64728a; }
.evidence-list code { color: #899ec9; }
.lineage-actions, .action-row { display: flex; flex-wrap: wrap; gap: 0.5rem; }
.resolution-section label { display: grid; gap: 0.35rem; color: #8390a6; font-size: 0.6rem; }
.resolution-section select { width: 100%; }
.resolution-section textarea { min-height: 5.5rem; font-size: 0.68rem; }
.action-confirm { margin: 0; padding: 0.55rem; border: 1px solid rgb(234 185 104 / 22%); border-radius: 0.5rem; color: #d8b879; background: rgb(75 52 21 / 16%); font-size: 0.6rem; line-height: 1.5; }
.action-row .primary-button { padding: 0.55rem 0.8rem; }
.resolved-section strong { color: #8edbc0; font-size: 0.7rem; }
.resolved-section small { color: #64718a; font-size: 0.58rem; }
.center-empty, .detail-empty { margin: 0; padding: 0.8rem; color: #65728a; font-size: 0.62rem; line-height: 1.55; }
button:disabled { cursor: not-allowed; opacity: 0.48; }

@media (max-width: 980px) {
  .center-grid { grid-template-columns: 1fr; }
}

@media (max-width: 560px) {
  .center-header { align-items: stretch; flex-direction: column; }
  .detail-facts { grid-template-columns: 1fr; }
}
</style>
