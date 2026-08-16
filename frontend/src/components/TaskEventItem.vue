<script setup lang="ts">
import { computed } from 'vue'
import type { TaskEvent } from '../types'

const props = defineProps<{ event: TaskEvent }>()

const eventMeta: Record<string, { label: string; tone: string }> = {
  'task.created': { label: '任务已创建', tone: 'neutral' },
  'task.status_changed': { label: '状态已更新', tone: 'active' },
  'plan.proposed': { label: '计划已生成', tone: 'active' },
  'step.started': { label: '步骤开始', tone: 'active' },
  'tool.requested': { label: '工具调用已请求', tone: 'warning' },
  'tool.started': { label: '工具执行中', tone: 'warning' },
  'tool.completed': { label: '工具执行完成', tone: 'success' },
  'tool.failed': { label: '工具执行失败', tone: 'danger' },
  'tool.cancelled': { label: '工具执行已取消', tone: 'danger' },
  'tool.unknown': { label: '工具结果待核对', tone: 'danger' },
  'effect.branch.decided': { label: '受信条件分支已决定', tone: 'active' },
  'effect.node.skipped': { label: '未选分支已跳过', tone: 'neutral' },
  'approval.required': { label: '需要用户审批', tone: 'warning' },
  'approval.expired': { label: '审批已过期', tone: 'danger' },
  'approval.invalidated': { label: '审批授权已失效', tone: 'danger' },
  'step.completed': { label: '步骤已验证', tone: 'success' },
  'task.completed': { label: '任务完成', tone: 'success' },
  'task.failed': { label: '任务失败', tone: 'danger' },
  'task.cancelled': { label: '任务已取消', tone: 'danger' },
}

const meta = computed(() => {
  if (props.event.type === 'approval.resolved') {
    const status = props.event.payload.status ?? props.event.payload.decision
    if (status === 'approved' || status === 'approve') {
      return { label: '审批已同意', tone: 'success' }
    }
    if (
      status === 'rejected' ||
      status === 'reject' ||
      status === 'cancelled' ||
      status === 'expired'
    ) {
      return {
        label: status === 'expired'
          ? '审批已过期'
          : status === 'cancelled'
            ? '审批已取消'
            : '审批已拒绝',
        tone: 'danger',
      }
    }
    return { label: '审批已处理', tone: 'active' }
  }

  return eventMeta[props.event.type] ?? {
    label: props.event.type,
    tone: 'neutral',
  }
})

const time = computed(() =>
  new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(new Date(props.event.timestamp)),
)

const payload = computed(() => {
  const text = JSON.stringify(props.event.payload, null, 2)
  return text.length > 800 ? `${text.slice(0, 800)}\n…` : text
})
</script>

<template>
  <li class="event-item" :data-tone="meta.tone">
    <span class="event-dot" aria-hidden="true" />
    <div class="event-card">
      <div class="event-heading">
        <div>
          <strong>{{ meta.label }}</strong>
          <code>#{{ event.seq }}</code>
        </div>
        <time :datetime="event.timestamp">{{ time }}</time>
      </div>
      <p class="event-type">{{ event.type }}</p>
      <details>
        <summary>查看结构化数据</summary>
        <pre>{{ payload }}</pre>
      </details>
    </div>
  </li>
</template>
