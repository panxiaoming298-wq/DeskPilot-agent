<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { TaskControlAction, TaskStatus } from '../types'

const props = withDefaults(
  defineProps<{
    status: TaskStatus
    activeAction: TaskControlAction | null
    message: string | null
    error: string | null
    disabled: boolean
  }>(),
  {
    activeAction: null,
    message: null,
    error: null,
    disabled: false,
  },
)

const emit = defineEmits<{
  control: [action: TaskControlAction]
}>()

const cancelConfirmationOpen = ref(false)

const terminal = computed(() =>
  ['succeeded', 'failed', 'cancelled'].includes(props.status),
)
const pending = computed(() => props.activeAction !== null || props.disabled)
const pendingLabel = computed(() => {
  const labels: Record<TaskControlAction, string> = {
    pause: '正在暂停…',
    resume: '正在继续…',
    cancel: '正在取消…',
  }
  return props.activeAction ? labels[props.activeAction] : null
})

watch(
  () => props.status,
  () => {
    cancelConfirmationOpen.value = false
  },
)

function requestControl(action: TaskControlAction): void {
  if (pending.value || terminal.value) return
  emit('control', action)
}

function openCancelConfirmation(): void {
  if (pending.value || terminal.value) return
  cancelConfirmationOpen.value = true
}
</script>

<template>
  <section class="task-controls" aria-label="任务控制">
    <div v-if="!terminal" class="task-control-actions" :aria-busy="pending">
      <button
        v-if="status === 'running'"
        class="inline-button control-button"
        data-testid="pause-task"
        type="button"
        :disabled="pending"
        @click="requestControl('pause')"
      >
        暂停任务
      </button>

      <button
        v-if="status === 'paused'"
        class="primary-button control-button"
        data-testid="resume-task"
        type="button"
        :disabled="pending"
        @click="requestControl('resume')"
      >
        继续任务
      </button>

      <button
        v-if="!cancelConfirmationOpen"
        class="danger-button control-button"
        data-testid="cancel-task"
        type="button"
        :disabled="pending"
        @click="openCancelConfirmation"
      >
        取消任务
      </button>

      <div
        v-else
        class="cancel-confirmation"
        role="alertdialog"
        aria-labelledby="task-cancel-confirmation-title"
      >
        <p id="task-cancel-confirmation-title">确定取消当前任务吗？</p>
        <div class="confirmation-actions">
          <button
            class="text-button"
            data-testid="dismiss-cancel"
            type="button"
            :disabled="pending"
            @click="cancelConfirmationOpen = false"
          >
            返回
          </button>
          <button
            class="danger-button"
            data-testid="confirm-cancel"
            type="button"
            :disabled="pending"
            @click="requestControl('cancel')"
          >
            {{ activeAction === 'cancel' ? '正在取消…' : '确认取消' }}
          </button>
        </div>
      </div>
    </div>

    <p v-else class="terminal-note">任务已结束，无需继续操作。</p>

    <div class="control-feedback" aria-live="polite" aria-atomic="true">
      <p v-if="pendingLabel" class="pending-message" role="status">{{ pendingLabel }}</p>
      <p v-else-if="message" class="success-message" role="status">{{ message }}</p>
      <p v-if="error" class="error-message" role="alert">{{ error }}</p>
    </div>
  </section>
</template>

<style scoped>
.task-controls {
  display: grid;
  gap: 0.75rem;
}

.task-control-actions,
.confirmation-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.625rem;
}

.control-button {
  min-height: 2.5rem;
}

.cancel-confirmation {
  display: flex;
  flex: 1 1 100%;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 0.75rem 0.875rem;
  border: 1px solid rgb(239 68 68 / 32%);
  border-radius: 0.75rem;
  background: rgb(239 68 68 / 8%);
}

.cancel-confirmation p,
.control-feedback p,
.terminal-note {
  margin: 0;
}

.cancel-confirmation p {
  color: var(--text-primary, #e5e7eb);
  font-size: 0.875rem;
}

.control-feedback:empty {
  display: none;
}

.pending-message,
.success-message,
.error-message,
.terminal-note {
  font-size: 0.8125rem;
  line-height: 1.5;
}

.pending-message {
  color: var(--accent, #7dd3fc);
}

.success-message {
  color: var(--success, #86efac);
}

.error-message {
  color: var(--danger, #fca5a5);
}

.terminal-note {
  color: var(--text-muted, #94a3b8);
}

@media (max-width: 560px) {
  .task-control-actions,
  .cancel-confirmation,
  .confirmation-actions {
    align-items: stretch;
    flex-direction: column;
  }

  .control-button,
  .confirmation-actions,
  .confirmation-actions button {
    width: 100%;
  }
}
</style>
