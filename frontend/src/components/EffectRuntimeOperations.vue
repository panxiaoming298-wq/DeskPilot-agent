<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { useEffectRuntimeOperations } from '../composables/useEffectRuntimeOperations'

const {
  snapshot,
  auditEvents,
  auditHasMore,
  alertNotifications,
  alertNotificationsHaveMore,
  deadLetters,
  loading,
  activeAction,
  message,
  error,
  reload,
  sampleMetrics,
  runRetention,
  requeueDeadLetter,
  downloadAuditExport,
} = useEffectRuntimeOperations()

const retentionDays = ref(30)
const confirmation = ref<'retention' | `requeue:${string}` | null>(null)

const newestAuditEvents = computed(() => [...auditEvents.value].reverse())
const newestAlertNotifications = computed(() => [...alertNotifications.value].reverse())
const criticalAlerts = computed(
  () => snapshot.value?.alerts.filter((alert) => alert.severity === 'critical').length ?? 0,
)

function shortDigest(value: string | null): string {
  if (!value) return '—'
  return `${value.slice(0, 10)}…${value.slice(-8)}`
}

function formatTime(value: string | null): string {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'medium',
    timeStyle: 'medium',
  }).format(new Date(value))
}

function askRetention(): void {
  if (
    activeAction.value !== null
    || !Number.isInteger(retentionDays.value)
    || retentionDays.value < 1
    || retentionDays.value > 3_650
  ) return
  confirmation.value = 'retention'
}

async function confirmRetention(): Promise<void> {
  if (confirmation.value !== 'retention') return
  await runRetention(retentionDays.value)
  confirmation.value = null
}

function askRequeue(messageId: string): void {
  if (activeAction.value !== null) return
  confirmation.value = `requeue:${messageId}`
}

async function confirmRequeue(messageId: string): Promise<void> {
  if (confirmation.value !== `requeue:${messageId}`) return
  await requeueDeadLetter(messageId)
  confirmation.value = null
}

onMounted(() => {
  void reload()
})
</script>

<template>
  <section class="operations-shell" aria-labelledby="operations-heading">
    <header class="operations-header">
      <div>
        <span class="eyebrow">PROTECTED RUNTIME OPERATIONS</span>
        <h2 id="operations-heading">运行时运维与审计</h2>
        <p>只显示脱敏状态、fence、摘要和数据库时间；读取不会 claim、ack、修复或访问外部网络。</p>
      </div>
      <div class="header-actions">
        <button type="button" :disabled="loading || activeAction !== null" @click="reload">
          {{ loading ? '读取中…' : '刷新真值' }}
        </button>
        <button class="primary-button" type="button" :disabled="loading || activeAction !== null" @click="sampleMetrics">
          {{ activeAction === 'sample' ? '采样中…' : '采样并审计' }}
        </button>
      </div>
    </header>

    <div class="operations-feedback" aria-live="polite">
      <p v-if="message" class="success-message">{{ message }}</p>
      <p v-if="error" class="error-message" role="alert">{{ error }}</p>
    </div>

    <template v-if="snapshot">
      <div class="truth-strip">
        <span>数据库时间 <strong>{{ formatTime(snapshot.database_time) }}</strong></span>
        <span>Snapshot <code>{{ shortDigest(snapshot.snapshot_digest) }}</code></span>
        <span :data-severity="criticalAlerts ? 'critical' : 'healthy'">
          {{ snapshot.alerts.length }} alerts / {{ criticalAlerts }} critical
        </span>
      </div>

      <section class="metric-grid" aria-label="四域运行指标">
        <article>
          <span class="eyebrow">GRAPH CONTROL</span>
          <strong>{{ snapshot.graph_controls.actionable }}</strong>
          <p>actionable / {{ snapshot.graph_controls.total }} total</p>
          <small>{{ snapshot.graph_controls.unrouted }} unrouted · {{ snapshot.graph_controls.claim_expired }} expired claim</small>
        </article>
        <article>
          <span class="eyebrow">CLUSTER ADMISSION</span>
          <strong>{{ snapshot.admissions.live_granted }}</strong>
          <p>live granted / {{ snapshot.admissions.live_pending }} pending</p>
          <small>revision {{ snapshot.admissions.scheduler_revision }} · next grant {{ snapshot.admissions.next_grant_sequence }}</small>
        </article>
        <article>
          <span class="eyebrow">READY PROJECTION</span>
          <strong>{{ snapshot.ready_projection.ready_nodes }}</strong>
          <p>ready / {{ snapshot.ready_projection.projected_nodes }} projected nodes</p>
          <small>{{ snapshot.ready_projection.missing_live_graphs }} missing · {{ snapshot.ready_projection.event_drift_graphs + snapshot.ready_projection.row_count_drift_graphs }} drift</small>
        </article>
        <article>
          <span class="eyebrow">OUTBOX / INBOX</span>
          <strong>{{ snapshot.outbox.dead_lettered }}</strong>
          <p>dead letter / {{ snapshot.outbox.pending_ready }} ready</p>
          <small>{{ snapshot.outbox.in_flight }} in flight · {{ snapshot.outbox.inbox_receipts }} inbox receipts</small>
        </article>
      </section>

      <section class="operations-panel alert-panel">
        <div class="panel-heading">
          <div><span class="eyebrow">STABLE ALERT CODES</span><h3>告警</h3></div>
          <span>{{ snapshot.alerts.length }}</span>
        </div>
        <ul v-if="snapshot.alerts.length" class="alert-list">
          <li v-for="alert in snapshot.alerts" :key="`${alert.domain}:${alert.code}`" :data-severity="alert.severity">
            <strong>{{ alert.code }}</strong>
            <span>{{ alert.domain }} · {{ alert.count }}</span>
          </li>
        </ul>
        <p v-else class="empty-copy">当前快照没有运行时告警。</p>
        <div class="notification-heading">
          <span class="eyebrow">DURABLE LIFECYCLE NOTIFICATIONS</span>
          <span>{{ alertNotifications.length }} records<span v-if="alertNotificationsHaveMore"> · 有更多</span></span>
        </div>
        <ol v-if="newestAlertNotifications.length" class="notification-list">
          <li v-for="notification in newestAlertNotifications" :key="notification.notification_id" :data-transition="notification.transition">
            <span>#{{ notification.sequence }} · {{ notification.transition }}</span>
            <strong>{{ notification.alert_code }}</strong>
            <small>revision {{ notification.alert_revision }} · count {{ notification.count }} · {{ formatTime(notification.occurred_at) }}</small>
          </li>
        </ol>
        <p v-else class="empty-copy">尚未产生告警 opened / updated / resolved 通知。</p>
      </section>

      <div class="sample-grid">
        <section class="operations-panel">
          <div class="panel-heading"><div><span class="eyebrow">CONTROL MAILBOX</span><h3>Graph control 样本</h3></div><span>{{ snapshot.graph_control_samples.length }}</span></div>
          <ul v-if="snapshot.graph_control_samples.length" class="record-list">
            <li v-for="record in snapshot.graph_control_samples" :key="record.control_id">
              <div><strong>{{ record.command }}</strong><em>{{ record.status }}</em></div>
              <code>{{ record.control_id }}</code>
              <small>revision {{ record.revision }} · claim fence {{ record.claim_fencing_token }} · {{ formatTime(record.updated_at) }}</small>
            </li>
          </ul>
          <p v-else class="empty-copy">没有有界 control 样本。</p>
        </section>

        <section class="operations-panel">
          <div class="panel-heading"><div><span class="eyebrow">CAPACITY FENCE</span><h3>Admission 样本</h3></div><span>{{ snapshot.admission_samples.length }}</span></div>
          <ul v-if="snapshot.admission_samples.length" class="record-list">
            <li v-for="record in snapshot.admission_samples" :key="record.admission_id">
              <div><strong>{{ record.tool_name }}</strong><em>{{ record.status }}</em></div>
              <code>{{ record.admission_id }}</code>
              <small>fence {{ record.fencing_token }} · grant {{ record.grant_sequence ?? '—' }} · {{ formatTime(record.updated_at) }}</small>
            </li>
          </ul>
          <p v-else class="empty-copy">没有有界 admission 样本。</p>
        </section>

        <section class="operations-panel">
          <div class="panel-heading"><div><span class="eyebrow">CONTENT PROOF</span><h3>Ready projection 样本</h3></div><span>{{ snapshot.ready_projection_samples.length }}</span></div>
          <ul v-if="snapshot.ready_projection_samples.length" class="record-list">
            <li v-for="record in snapshot.ready_projection_samples" :key="record.graph_id">
              <div><strong>{{ record.graph_status }}</strong><em>r{{ record.projection_revision }}</em></div>
              <code>{{ shortDigest(record.content_digest) }}</code>
              <small>{{ record.dependency_ready_nodes }} ready / {{ record.projected_nodes }} nodes · rebuild {{ record.rebuild_count }}</small>
            </li>
          </ul>
          <p v-else class="empty-copy">没有有界 ready projection 样本。</p>
        </section>

        <section class="operations-panel outbox-panel">
          <div class="panel-heading"><div><span class="eyebrow">DELIVERY LEDGER</span><h3>Outbox 样本</h3></div><span>{{ snapshot.outbox_samples.length }}</span></div>
          <ul v-if="snapshot.outbox_samples.length" class="record-list">
            <li v-for="record in snapshot.outbox_samples" :key="record.message_id" :data-state="record.state">
              <div><strong>{{ record.topic }}</strong><em>{{ record.state }}</em></div>
              <code>{{ record.message_id }} · payload {{ shortDigest(record.payload_digest) }}</code>
              <small>attempt {{ record.attempt_count }} · fence {{ record.claim_fencing_token }} · available {{ formatTime(record.available_at) }}</small>
              <button
                v-if="record.state === 'dead_lettered'"
                type="button"
                :disabled="activeAction !== null"
                @click="askRequeue(record.message_id)"
              >重新入队</button>
              <div v-if="confirmation === `requeue:${record.message_id}`" class="inline-confirm" role="alertdialog" aria-label="确认重新入队">
                <p>只重置这条 DLQ 消息并提升 delivery fence；不会展示或修改 payload。</p>
                <button class="primary-button" type="button" :disabled="activeAction !== null" @click="confirmRequeue(record.message_id)">
                  {{ activeAction === `requeue:${record.message_id}` ? '提交中…' : '确认重新入队' }}
                </button>
                <button type="button" :disabled="activeAction !== null" @click="confirmation = null">取消</button>
              </div>
            </li>
          </ul>
          <p v-else class="empty-copy">没有有界 Outbox 样本。</p>
        </section>
      </div>

      <section class="operations-panel retention-panel">
        <div>
          <span class="eyebrow">BOUNDED RETENTION</span>
          <h3>安全派生记录清理</h3>
          <p>只清理安全终态 graph 的派生 control/admission/ready、已发布 Outbox 与旧 Inbox receipt。DLQ、TaskEvent、active/blocked graph 永不自动删除。</p>
        </div>
        <label>保留天数 <input v-model.number="retentionDays" type="number" min="1" max="3650" /></label>
        <button type="button" :disabled="activeAction !== null || retentionDays < 1 || retentionDays > 3650" @click="askRetention">运行 retention</button>
        <div v-if="confirmation === 'retention'" class="retention-confirm" role="alertdialog" aria-label="确认运行 retention">
          <strong>确认按数据库时间执行不可逆清理？</strong>
          <p>清理清单摘要和 hash-chain 审计会与删除在同一事务提交；失败不会产生部分清理。</p>
          <button class="primary-button" type="button" :disabled="activeAction !== null" @click="confirmRetention">
            {{ activeAction === 'retention' ? '提交中…' : `确认保留 ${retentionDays} 天` }}
          </button>
          <button type="button" :disabled="activeAction !== null" @click="confirmation = null">取消</button>
        </div>
      </section>

      <section class="operations-panel audit-panel">
        <div class="panel-heading">
          <div><span class="eyebrow">APPEND-ONLY HASH CHAIN</span><h3>运维审计</h3></div>
          <div class="audit-actions">
            <span>{{ auditEvents.length }} events<span v-if="auditHasMore"> · 有更多</span></span>
            <button type="button" :disabled="activeAction !== null" @click="downloadAuditExport">
              {{ activeAction === 'audit-export' ? '导出中…' : '导出冻结审计' }}
            </button>
          </div>
        </div>
        <ol v-if="newestAuditEvents.length" class="audit-list">
          <li v-for="event in newestAuditEvents" :key="event.event_id">
            <span>#{{ event.sequence }}</span>
            <div><strong>{{ event.action }}</strong><small>{{ event.actor_id }} · {{ formatTime(event.occurred_at) }}</small></div>
            <code>event {{ shortDigest(event.event_digest) }}<br />prev {{ shortDigest(event.previous_event_digest) }}</code>
          </li>
        </ol>
        <p v-else class="empty-copy">还没有运维审计事件。</p>
      </section>
    </template>

    <div v-else-if="loading" class="operations-empty">正在读取数据库运行时真值…</div>
    <div v-else class="operations-empty">运行时快照不可用；请检查上方错误后手动重试。</div>
  </section>
</template>

<style scoped>
.operations-shell { display: grid; gap: 1rem; }
.operations-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; padding: 1.25rem; border: 1px solid rgb(128 148 197 / 17%); border-radius: 1rem; background: linear-gradient(140deg, rgb(19 28 50 / 90%), rgb(10 16 30 / 94%)); }
.operations-header h2 { margin-top: 0.35rem; }
.operations-header p, .retention-panel p { max-width: 55rem; margin: 0.5rem 0 0; color: #7f8aa2; font-size: 0.68rem; line-height: 1.6; }
.header-actions { display: flex; flex-wrap: wrap; gap: 0.55rem; }
.header-actions button, .retention-panel > button, .record-list button, .inline-confirm button, .retention-confirm button, .audit-actions button { padding: 0.55rem 0.75rem; border: 1px solid rgb(126 145 186 / 18%); border-radius: 0.55rem; background: rgb(20 29 49 / 82%); color: #aab7cd; font-size: 0.62rem; }
.operations-feedback:empty { display: none; }
.operations-feedback p { margin: 0; padding: 0.7rem 0.85rem; border-radius: 0.65rem; font-size: 0.65rem; }
.success-message { color: #8edbc0; background: rgb(24 92 72 / 18%); }
.error-message { color: #f0a1a3; background: rgb(110 35 42 / 18%); }
.truth-strip { display: flex; flex-wrap: wrap; gap: 0.55rem; }
.truth-strip span { padding: 0.55rem 0.7rem; border: 1px solid rgb(126 145 186 / 13%); border-radius: 0.55rem; color: #72809a; background: rgb(10 16 29 / 66%); font-size: 0.58rem; }
.truth-strip strong, .truth-strip code { color: #aebad0; }
.truth-strip [data-severity="critical"] { color: #f0a1a3; border-color: rgb(212 105 109 / 32%); }
.truth-strip [data-severity="healthy"] { color: #84cdb3; }
.metric-grid, .sample-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0.75rem; }
.metric-grid article, .operations-panel { min-width: 0; padding: 1rem; border: 1px solid rgb(128 148 197 / 15%); border-radius: 0.85rem; background: rgb(12 18 33 / 84%); }
.metric-grid article { display: grid; gap: 0.35rem; }
.metric-grid article > strong { color: #dce5f5; font-size: 1.55rem; }
.metric-grid p, .metric-grid small { margin: 0; color: #76839a; font-size: 0.6rem; }
.metric-grid small { color: #596780; line-height: 1.45; }
.panel-heading { display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; }
.panel-heading h3, .retention-panel h3 { margin-top: 0.25rem; color: #d2daea; }
.panel-heading > span { color: #69768e; font-size: 0.58rem; }
.alert-list, .notification-list, .record-list, .audit-list { display: grid; gap: 0.45rem; margin: 0.75rem 0 0; padding: 0; list-style: none; }
.notification-heading { display: flex; justify-content: space-between; gap: 0.75rem; margin-top: 1rem; padding-top: 0.85rem; border-top: 1px solid rgb(128 148 197 / 12%); color: #71809b; font-size: 0.58rem; }
.notification-list li { display: grid; grid-template-columns: minmax(7rem, auto) minmax(12rem, 1fr) minmax(14rem, auto); gap: 0.65rem; align-items: center; padding: 0.6rem; border-radius: 0.5rem; background: rgb(7 11 20 / 45%); }
.notification-list li[data-transition='opened'] { border-left: 2px solid #e4a662; }
.notification-list li[data-transition='resolved'] { border-left: 2px solid #5fbea1; }
.notification-list span, .notification-list small { color: #71809b; font-size: 0.56rem; }
.notification-list strong { color: #b9c5d9; font-size: 0.62rem; }
.audit-actions { display: flex; align-items: center; gap: 0.65rem; }
.alert-list li { display: flex; justify-content: space-between; gap: 0.75rem; padding: 0.6rem; border-left: 2px solid #d3a85f; border-radius: 0.5rem; background: rgb(7 11 20 / 48%); }
.alert-list li[data-severity="critical"] { border-color: #d4696d; }
.alert-list strong, .alert-list span { font-size: 0.58rem; }
.alert-list strong { color: #bdc8da; }
.alert-list span { color: #7d899e; }
.sample-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.record-list li { display: grid; gap: 0.35rem; padding: 0.65rem; border-radius: 0.55rem; background: rgb(7 11 20 / 48%); }
.record-list li > div:first-child { display: flex; justify-content: space-between; gap: 0.5rem; }
.record-list strong { color: #b9c5d9; font-size: 0.63rem; }
.record-list em { color: #8fa2c8; font-size: 0.56rem; font-style: normal; }
.record-list code, .record-list small { overflow-wrap: anywhere; color: #64728a; font-size: 0.54rem; line-height: 1.45; }
.record-list li[data-state="dead_lettered"] { border: 1px solid rgb(212 105 109 / 24%); }
.record-list button { justify-self: start; color: #d9b777; }
.inline-confirm, .retention-confirm { padding: 0.7rem; border: 1px solid rgb(234 185 104 / 22%); border-radius: 0.55rem; background: rgb(75 52 21 / 16%); }
.inline-confirm p, .retention-confirm p { margin: 0 0 0.55rem; color: #c1a773; font-size: 0.58rem; line-height: 1.5; }
.inline-confirm button + button, .retention-confirm button + button { margin-left: 0.45rem; }
.retention-panel { display: grid; grid-template-columns: minmax(18rem, 1fr) auto auto; align-items: end; gap: 0.75rem; }
.retention-panel label { display: grid; gap: 0.35rem; color: #7d899e; font-size: 0.58rem; }
.retention-panel input { width: 7rem; }
.retention-confirm { grid-column: 1 / -1; }
.retention-confirm strong { color: #ddbf84; font-size: 0.65rem; }
.audit-list li { display: grid; grid-template-columns: 3rem minmax(12rem, 1fr) minmax(14rem, 1fr); gap: 0.65rem; align-items: center; padding: 0.65rem; border-radius: 0.5rem; background: rgb(7 11 20 / 45%); }
.audit-list > li > span { color: #71809b; font-size: 0.58rem; }
.audit-list div { display: grid; gap: 0.2rem; }
.audit-list strong { color: #b9c5d9; font-size: 0.62rem; }
.audit-list small, .audit-list code { color: #63718a; font-size: 0.54rem; line-height: 1.45; }
.empty-copy, .operations-empty { margin: 0.7rem 0 0; color: #65728a; font-size: 0.62rem; line-height: 1.55; }
.operations-empty { padding: 1rem; border: 1px solid rgb(128 148 197 / 15%); border-radius: 0.75rem; background: rgb(12 18 33 / 84%); }
button:disabled { cursor: not-allowed; opacity: 0.48; }

@media (max-width: 1050px) {
  .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

@media (max-width: 760px) {
  .operations-header { align-items: stretch; flex-direction: column; }
  .sample-grid, .retention-panel { grid-template-columns: 1fr; }
  .retention-confirm { grid-column: auto; }
  .audit-list li { grid-template-columns: 2.5rem 1fr; }
  .audit-list code { grid-column: 2; }
}

@media (max-width: 520px) {
  .metric-grid { grid-template-columns: 1fr; }
}
</style>
