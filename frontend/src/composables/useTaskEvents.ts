import { onUnmounted, ref } from 'vue'
import {
  buildTaskSocketUrl,
  getLocalSession,
  invalidateLocalSession,
} from '../api'
import type { LocalSession, TaskEvent, TaskStreamState } from '../types'

const TERMINAL_EVENTS = new Set(['task.completed', 'task.failed', 'task.cancelled'])
const MAX_RECONNECT_DELAY_MS = 8_000

export function useTaskEvents() {
  const events = ref<TaskEvent[]>([])
  const connected = ref(false)
  const streamError = ref<string | null>(null)
  const connectionState = ref<TaskStreamState>('idle')
  const recoveryMessage = ref<string | null>(null)
  const reconnectAttempt = ref(0)
  const retryDelayMs = ref(0)

  let socket: WebSocket | null = null
  let activeTaskId: string | null = null
  let reconnectTimer: number | null = null
  let terminalReached = false
  let permanentlyStopped = false
  let openingGeneration: number | null = null
  let generation = 0

  function connect(taskId: string) {
    const taskChanged = activeTaskId !== taskId
    stopSocket()
    if (taskChanged) events.value = []
    activeTaskId = taskId
    terminalReached = false
    permanentlyStopped = false
    streamError.value = null
    recoveryMessage.value = null
    reconnectAttempt.value = 0
    retryDelayMs.value = 0
    connectionState.value = 'connecting'
    const epoch = generation
    void openSocket(epoch, false)
  }

  async function openSocket(epoch: number, recovering: boolean) {
    if (
      epoch !== generation ||
      !activeTaskId ||
      terminalReached ||
      permanentlyStopped ||
      openingGeneration === epoch
    ) {
      return
    }

    openingGeneration = epoch
    const taskId = activeTaskId
    const afterSeq = events.value.at(-1)?.seq ?? 0

    let session: LocalSession
    try {
      session = await getLocalSession()
    } catch (error) {
      if (epoch !== generation || activeTaskId !== taskId) return
      finishOpening(epoch)
      streamError.value = error instanceof Error
        ? error.message
        : '无法建立本地安全会话。'
      scheduleReconnect(epoch, connectionState.value === 'reauthenticating')
      return
    }

    if (
      epoch !== generation ||
      activeTaskId !== taskId ||
      terminalReached ||
      permanentlyStopped
    ) {
      finishOpening(epoch)
      return
    }

    let nextSocket: WebSocket
    try {
      nextSocket = new WebSocket(buildTaskSocketUrl(taskId, afterSeq), [
        session.websocket_protocol,
        `deskpilot.auth.${session.access_token}`,
      ])
    } catch (error) {
      finishOpening(epoch)
      streamError.value = error instanceof Error
        ? error.message
        : '任务事件连接暂时不可用。'
      scheduleReconnect(epoch, connectionState.value === 'reauthenticating')
      return
    }

    socket = nextSocket

    nextSocket.onopen = () => {
      if (!isCurrentSocket(nextSocket, epoch, taskId)) return
      finishOpening(epoch)
      connected.value = true
      connectionState.value = 'connected'
      streamError.value = null
      reconnectAttempt.value = 0
      retryDelayMs.value = 0
      if (recovering) {
        recoveryMessage.value = `事件流已恢复，正在从 #${afterSeq} 后继续接收。`
      }
    }

    nextSocket.onmessage = (message) => {
      if (!isCurrentSocket(nextSocket, epoch, taskId)) return

      const event = parseTaskEvent(message.data, taskId)
      if (!event) {
        streamError.value = '收到无法识别的任务事件，已安全忽略。'
        return
      }
      if (events.value.some((existing) => existing.seq === event.seq)) return

      events.value = [...events.value, event].sort((left, right) => left.seq - right.seq)
      if (TERMINAL_EVENTS.has(event.type)) archiveSocket(nextSocket, epoch, taskId)
    }

    nextSocket.onerror = () => {
      if (!isCurrentSocket(nextSocket, epoch, taskId)) return
      streamError.value = '任务事件连接暂时不可用，正在尝试恢复。'
    }

    nextSocket.onclose = (event) => {
      if (!isCurrentSocket(nextSocket, epoch, taskId)) return
      finishOpening(epoch)
      connected.value = false
      socket = null
      detachSocket(nextSocket)

      if (event.code === 4401) {
        invalidateLocalSession()
        streamError.value = '本地会话已失效，正在重新认证。'
        connectionState.value = 'reauthenticating'
        scheduleReconnect(epoch, true)
        return
      }
      if (event.code === 4403) {
        permanentlyStopped = true
        connectionState.value = 'forbidden'
        streamError.value = '当前页面来源未获得本地事件流授权。'
        return
      }
      if (event.code === 4404) {
        permanentlyStopped = true
        connectionState.value = 'not_found'
        streamError.value = '任务不存在或已不可访问，事件流已停止。'
        return
      }
      scheduleReconnect(epoch, false)
    }
  }

  function scheduleReconnect(epoch: number, reauthenticating: boolean) {
    if (
      epoch !== generation ||
      terminalReached ||
      permanentlyStopped ||
      !activeTaskId ||
      reconnectTimer !== null
    ) {
      return
    }

    reconnectAttempt.value += 1
    retryDelayMs.value = Math.min(
      1_000 * 2 ** (reconnectAttempt.value - 1),
      MAX_RECONNECT_DELAY_MS,
    )
    connectionState.value = reauthenticating ? 'reauthenticating' : 'reconnecting'
    const delay = retryDelayMs.value
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null
      if (epoch !== generation) return
      void openSocket(epoch, true)
    }, delay)
  }

  function reconnectNow() {
    if (
      !activeTaskId ||
      terminalReached ||
      permanentlyStopped ||
      openingGeneration === generation ||
      socket !== null ||
      connected.value
    ) {
      return
    }
    if (reconnectTimer !== null) {
      window.clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    void openSocket(generation, true)
  }

  function reset() {
    stopSocket()
    events.value = []
    streamError.value = null
    recoveryMessage.value = null
    reconnectAttempt.value = 0
    retryDelayMs.value = 0
    terminalReached = false
    permanentlyStopped = false
    connectionState.value = 'idle'
  }

  function stopSocket() {
    generation += 1
    activeTaskId = null
    openingGeneration = null
    connected.value = false
    if (reconnectTimer !== null) {
      window.clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    if (socket) {
      const previousSocket = socket
      socket = null
      detachSocket(previousSocket)
      previousSocket.close()
    }
  }

  function archiveSocket(currentSocket: WebSocket, epoch: number, taskId: string) {
    if (!isCurrentSocket(currentSocket, epoch, taskId)) return
    terminalReached = true
    connected.value = false
    connectionState.value = 'archived'
    streamError.value = null
    retryDelayMs.value = 0
    if (reconnectTimer !== null) {
      window.clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    socket = null
    detachSocket(currentSocket)
    currentSocket.close()
  }

  function isCurrentSocket(
    candidate: WebSocket,
    epoch: number,
    taskId: string | null,
  ): boolean {
    return (
      epoch === generation &&
      socket === candidate &&
      activeTaskId === taskId
    )
  }

  function finishOpening(epoch: number) {
    if (openingGeneration === epoch) openingGeneration = null
  }

  onUnmounted(stopSocket)

  return {
    events,
    connected,
    streamError,
    connectionState,
    recoveryMessage,
    reconnectAttempt,
    retryDelayMs,
    connect,
    reconnectNow,
    reset,
  }
}

function detachSocket(target: WebSocket) {
  target.onopen = null
  target.onmessage = null
  target.onerror = null
  target.onclose = null
}

function parseTaskEvent(data: unknown, expectedTaskId: string): TaskEvent | null {
  let candidate: unknown
  try {
    candidate = typeof data === 'string' ? JSON.parse(data) : JSON.parse(String(data))
  } catch {
    return null
  }

  if (!candidate || typeof candidate !== 'object') return null
  const event = candidate as Partial<TaskEvent>
  if (
    typeof event.event_id !== 'string' ||
    event.task_id !== expectedTaskId ||
    !Number.isInteger(event.seq) ||
    (event.seq ?? 0) < 1 ||
    typeof event.type !== 'string' ||
    typeof event.timestamp !== 'string' ||
    typeof event.trace_id !== 'string' ||
    !event.payload ||
    typeof event.payload !== 'object' ||
    Array.isArray(event.payload)
  ) {
    return null
  }
  return event as TaskEvent
}
