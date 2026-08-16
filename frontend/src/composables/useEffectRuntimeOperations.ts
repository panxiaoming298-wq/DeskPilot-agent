import { computed, ref } from 'vue'

import {
  ApiProblemError,
  createIdempotencyKey,
  getEffectRuntimeAlertNotifications,
  getEffectRuntimeAudit,
  getEffectRuntimeAuditExport,
  getEffectRuntimeOperations,
  requeueOutboxDeadLetter,
  runEffectRuntimeRetention,
  sampleEffectRuntimeMetrics,
} from '../api'
import type {
  EffectRuntimeAuditEvent,
  EffectRuntimeOperationsSnapshot,
  OperationsAlertNotification,
  OutboxOperationsRead,
} from '../types'

type OperationsAction = 'sample' | 'retention' | 'audit-export' | `requeue:${string}`

interface RetentionRetry {
  retentionDays: number
  key: string
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiProblemError) return error.message
  if (error instanceof Error && error.message) return error.message
  return '请求未完成'
}

export function useEffectRuntimeOperations() {
  const snapshot = ref<EffectRuntimeOperationsSnapshot | null>(null)
  const auditEvents = ref<EffectRuntimeAuditEvent[]>([])
  const auditHasMore = ref(false)
  const alertNotifications = ref<OperationsAlertNotification[]>([])
  const alertNotificationsHaveMore = ref(false)
  const loading = ref(false)
  const activeAction = ref<OperationsAction | null>(null)
  const message = ref<string | null>(null)
  const error = ref<string | null>(null)

  const deadLetters = computed<OutboxOperationsRead[]>(
    () => snapshot.value?.outbox_samples.filter(
      (record) => record.state === 'dead_lettered',
    ) ?? [],
  )

  let loadGeneration = 0
  let retentionRetry: RetentionRetry | null = null
  const requeueKeys = new Map<string, string>()

  function mergeAudit(event: EffectRuntimeAuditEvent): void {
    const bySequence = new Map(
      auditEvents.value.map((item) => [item.sequence, item]),
    )
    bySequence.set(event.sequence, event)
    auditEvents.value = [...bySequence.values()].sort(
      (left, right) => left.sequence - right.sequence,
    )
  }

  function mergeAlertNotifications(notifications: OperationsAlertNotification[]): void {
    const bySequence = new Map(
      alertNotifications.value.map((item) => [item.sequence, item]),
    )
    for (const notification of notifications) {
      bySequence.set(notification.sequence, notification)
    }
    alertNotifications.value = [...bySequence.values()].sort(
      (left, right) => left.sequence - right.sequence,
    )
  }

  async function reload(): Promise<void> {
    const generation = ++loadGeneration
    loading.value = true
    message.value = null
    error.value = null
    try {
      const [nextSnapshot, auditPage, alertPage] = await Promise.all([
        getEffectRuntimeOperations(),
        getEffectRuntimeAudit(),
        getEffectRuntimeAlertNotifications(),
      ])
      if (generation !== loadGeneration) return
      snapshot.value = nextSnapshot
      auditEvents.value = auditPage.events
      auditHasMore.value = auditPage.has_more
      alertNotifications.value = alertPage.notifications
      alertNotificationsHaveMore.value = alertPage.has_more
    } catch (caught) {
      if (generation !== loadGeneration) return
      error.value = `加载运行时运维真值失败：${errorMessage(caught)}`
    } finally {
      if (generation === loadGeneration) loading.value = false
    }
  }

  async function refreshAfterConfirmedMutation(): Promise<void> {
    try {
      const [nextSnapshot, auditPage, alertPage] = await Promise.all([
        getEffectRuntimeOperations(),
        getEffectRuntimeAudit(),
        getEffectRuntimeAlertNotifications(),
      ])
      snapshot.value = nextSnapshot
      auditEvents.value = auditPage.events
      auditHasMore.value = auditPage.has_more
      alertNotifications.value = alertPage.notifications
      alertNotificationsHaveMore.value = alertPage.has_more
    } catch (caught) {
      error.value = `操作已由服务端确认，但刷新运维真值失败：${errorMessage(caught)}`
    }
  }

  async function sampleMetrics(): Promise<boolean> {
    if (activeAction.value !== null) return false
    activeAction.value = 'sample'
    message.value = null
    error.value = null
    try {
      const result = await sampleEffectRuntimeMetrics()
      snapshot.value = result.snapshot
      mergeAudit(result.audit_event)
      mergeAlertNotifications(result.alert_notifications)
      const notificationSummary = result.alert_notifications.length
        ? `，生成 ${result.alert_notifications.length} 条告警生命周期通知`
        : ''
      message.value = `已按数据库时间采样指标并追加 hash-chain 审计${notificationSummary}。`
      return true
    } catch (caught) {
      error.value = `指标采样失败：${errorMessage(caught)}`
      return false
    } finally {
      activeAction.value = null
    }
  }

  async function runRetention(retentionDays: number): Promise<boolean> {
    if (
      activeAction.value !== null
      || !Number.isInteger(retentionDays)
      || retentionDays < 1
      || retentionDays > 3_650
    ) return false
    if (!retentionRetry || retentionRetry.retentionDays !== retentionDays) {
      retentionRetry = {
        retentionDays,
        key: createIdempotencyKey(),
      }
    }
    activeAction.value = 'retention'
    message.value = null
    error.value = null
    try {
      const result = await runEffectRuntimeRetention(
        retentionDays,
        retentionRetry.key,
      )
      mergeAudit(result.audit_event)
      retentionRetry = null
      const total = Object.values(result.counts).reduce((sum, count) => sum + count, 0)
      message.value = `Retention 已提交：清理 ${total} 条安全派生记录。`
      await refreshAfterConfirmedMutation()
      return true
    } catch (caught) {
      error.value = `Retention 执行失败：${errorMessage(caught)}`
      return false
    } finally {
      activeAction.value = null
    }
  }

  async function requeueDeadLetter(messageId: string): Promise<boolean> {
    if (
      activeAction.value !== null
      || !deadLetters.value.some((record) => record.message_id === messageId)
    ) return false
    const key = requeueKeys.get(messageId) ?? createIdempotencyKey()
    requeueKeys.set(messageId, key)
    activeAction.value = `requeue:${messageId}`
    message.value = null
    error.value = null
    try {
      const result = await requeueOutboxDeadLetter(messageId, key)
      if (result.message_id !== messageId) {
        error.value = 'DLQ requeue 响应与当前消息不匹配，已忽略。'
        return false
      }
      mergeAudit(result.audit_event)
      requeueKeys.delete(messageId)
      message.value = 'DLQ 消息已提升 fence 并重新进入待发布队列。'
      await refreshAfterConfirmedMutation()
      return true
    } catch (caught) {
      error.value = `DLQ requeue 失败：${errorMessage(caught)}`
      return false
    } finally {
      activeAction.value = null
    }
  }

  async function downloadAuditExport(): Promise<boolean> {
    if (activeAction.value !== null) return false
    activeAction.value = 'audit-export'
    message.value = null
    error.value = null
    try {
      let cursor: string | null = null
      let exportId: string | null = null
      let databaseTime: string | null = null
      let throughSequence: number | null = null
      let throughEventDigest: string | null = null
      let expectedSequence = 1
      let pages = 0
      const pageDigests: string[] = []
      const events: EffectRuntimeAuditEvent[] = []
      do {
        const page = await getEffectRuntimeAuditExport(cursor)
        pages += 1
        if (pages > 10_000) throw new Error('审计导出页数超过安全上限')
        if (exportId === null) {
          exportId = page.export_id
          databaseTime = page.database_time
          throughSequence = page.through_sequence
          throughEventDigest = page.through_event_digest
        } else if (
          page.export_id !== exportId
          || page.database_time !== databaseTime
          || page.through_sequence !== throughSequence
          || page.through_event_digest !== throughEventDigest
        ) {
          throw new Error('审计导出冻结身份发生变化')
        }
        for (const event of page.events) {
          if (event.sequence !== expectedSequence) {
            throw new Error('审计导出序号不连续')
          }
          events.push(event)
          expectedSequence += 1
        }
        pageDigests.push(page.page_digest)
        if (page.has_more !== (page.next_cursor !== null)) {
          throw new Error('审计导出游标与分页状态不一致')
        }
        cursor = page.next_cursor
      } while (cursor !== null)
      if (
        exportId === null
        || databaseTime === null
        || throughSequence === null
        || expectedSequence - 1 !== throughSequence
        || (throughSequence > 0
          && events.at(-1)?.event_digest !== throughEventDigest)
      ) {
        throw new Error('审计导出终点证明不完整')
      }
      const bundle = {
        schema_version: 'deskpilot.effect-runtime-audit-export-bundle.v1',
        export_id: exportId,
        database_time: databaseTime,
        through_sequence: throughSequence,
        through_event_digest: throughEventDigest,
        page_digests: pageDigests,
        events,
      }
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' }),
      )
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `deskpilot-effect-runtime-audit-${throughSequence}.json`
      anchor.click()
      URL.revokeObjectURL(url)
      message.value = `已导出冻结至 #${throughSequence} 的 ${events.length} 条脱敏审计。`
      return true
    } catch (caught) {
      error.value = `审计导出失败：${errorMessage(caught)}`
      return false
    } finally {
      activeAction.value = null
    }
  }

  function dismissFeedback(): void {
    message.value = null
    error.value = null
  }

  return {
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
    dismissFeedback,
  }
}
