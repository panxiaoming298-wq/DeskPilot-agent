import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LocalSession, TaskEvent } from '../types'
import { useTaskEvents } from './useTaskEvents'

const apiMocks = vi.hoisted(() => ({
  getLocalSession: vi.fn<() => Promise<LocalSession>>(),
  invalidateLocalSession: vi.fn(),
  buildTaskSocketUrl: vi.fn((taskId: string, afterSeq: number) =>
    `ws://deskpilot.test/api/v1/ws/tasks/${taskId}?after_seq=${afterSeq}`,
  ),
}))

vi.mock('../api', () => apiMocks)

class MockWebSocket {
  static instances: MockWebSocket[] = []

  readonly url: string
  readonly protocols: string[]
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  close = vi.fn()

  constructor(url: string | URL, protocols: string | string[] = []) {
    this.url = String(url)
    this.protocols = typeof protocols === 'string' ? [protocols] : protocols
    MockWebSocket.instances.push(this)
  }

  open() {
    this.onopen?.(new Event('open'))
  }

  message(data: unknown) {
    this.onmessage?.({ data: typeof data === 'string' ? data : JSON.stringify(data) } as MessageEvent)
  }

  fail() {
    this.onerror?.(new Event('error'))
  }

  closeFromServer(code = 1006) {
    this.onclose?.({ code } as CloseEvent)
  }
}

type TaskEventStream = ReturnType<typeof useTaskEvents>

function mountStream() {
  let stream: TaskEventStream | null = null
  const wrapper = mount(defineComponent({
    setup() {
      stream = useTaskEvents()
      return () => h('div')
    },
  }))
  if (!stream) throw new Error('event stream was not initialized')
  return { stream: stream as TaskEventStream, wrapper }
}

function mountStreams(count: number) {
  let streams: TaskEventStream[] = []
  const wrapper = mount(defineComponent({
    setup() {
      streams = Array.from({ length: count }, () => useTaskEvents())
      return () => h('div')
    },
  }))
  return { streams, wrapper }
}

function taskEvent(
  seq: number,
  type = 'agent.started',
  taskId = 'task-1',
): TaskEvent {
  return {
    event_id: `event-${seq}`,
    task_id: taskId,
    seq,
    type,
    timestamp: '2026-08-09T00:00:00Z',
    trace_id: 'trace-1',
    payload: {},
  }
}

async function connect(stream: TaskEventStream, taskId = 'task-1') {
  stream.connect(taskId)
  await flushPromises()
  return MockWebSocket.instances.at(-1)!
}

beforeEach(() => {
  vi.useFakeTimers()
  MockWebSocket.instances = []
  vi.stubGlobal('WebSocket', MockWebSocket)
  apiMocks.getLocalSession.mockReset()
  apiMocks.getLocalSession.mockResolvedValue({
    access_token: 'token-1',
    token_type: 'Bearer',
    websocket_protocol: 'deskpilot.events.v1',
  })
  apiMocks.invalidateLocalSession.mockReset()
  apiMocks.buildTaskSocketUrl.mockClear()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('useTaskEvents', () => {
  it('三个任务断线后各自从独立 cursor 恢复', async () => {
    const taskIds = ['task-1', 'task-2', 'task-3']
    const cursors = [3, 5, 7]
    const { streams, wrapper } = mountStreams(3)

    streams.forEach((stream, index) => stream.connect(taskIds[index]))
    await flushPromises()
    expect(MockWebSocket.instances).toHaveLength(3)

    for (const [index, socket] of MockWebSocket.instances.entries()) {
      socket.open()
      socket.message(taskEvent(cursors[index], 'agent.started', taskIds[index]))
      socket.closeFromServer()
      expect(streams[index].connectionState.value).toBe('reconnecting')
    }

    await vi.advanceTimersByTimeAsync(1_000)
    await flushPromises()
    const recovered = MockWebSocket.instances.slice(3)
    expect(recovered).toHaveLength(3)
    recovered.forEach((socket, index) => {
      expect(socket.url).toContain(`/${taskIds[index]}?after_seq=${cursors[index]}`)
      socket.open()
      expect(streams[index].connectionState.value).toBe('connected')
    })
    wrapper.unmount()
  })

  it('公开兼容连接标记并在握手成功后进入 connected', async () => {
    const { stream, wrapper } = mountStream()

    stream.connect('task-1')
    expect(stream.connectionState.value).toBe('connecting')
    expect(stream.connected.value).toBe(false)
    await flushPromises()

    const socket = MockWebSocket.instances[0]
    expect(socket.url).toContain('task-1?after_seq=0')
    expect(socket.protocols).toEqual([
      'deskpilot.events.v1',
      'deskpilot.auth.token-1',
    ])
    socket.open()

    expect(stream.connected.value).toBe(true)
    expect(stream.connectionState.value).toBe('connected')
    expect(stream.streamError.value).toBeNull()
    wrapper.unmount()
  })

  it('按 seq 排序去重，并安全忽略错误 JSON 与其它任务的事件', async () => {
    const { stream, wrapper } = mountStream()
    const socket = await connect(stream)
    socket.open()

    socket.message(taskEvent(3))
    socket.message(taskEvent(1))
    socket.message(taskEvent(3, 'agent.completed'))
    socket.message(taskEvent(2, 'agent.started', 'task-other'))
    socket.message('{not-json')

    expect(stream.events.value.map((event) => event.seq)).toEqual([1, 3])
    expect(stream.events.value[1].type).toBe('agent.started')
    expect(stream.streamError.value).toBe('收到无法识别的任务事件，已安全忽略。')
    wrapper.unmount()
  })

  it('重连携带最新 after_seq，并以 1/2/4/8 秒指数退避', async () => {
    const { stream, wrapper } = mountStream()
    const first = await connect(stream)
    first.open()
    first.message(taskEvent(7))
    first.closeFromServer()

    expect(stream.reconnectAttempt.value).toBe(1)
    expect(stream.retryDelayMs.value).toBe(1_000)
    expect(stream.connectionState.value).toBe('reconnecting')

    const delays = [1_000, 2_000, 4_000, 8_000, 8_000]
    for (const [index, delay] of delays.entries()) {
      await vi.advanceTimersByTimeAsync(delay)
      await flushPromises()
      const current = MockWebSocket.instances[index + 1]
      expect(current.url).toContain('after_seq=7')
      if (index < delays.length - 1) {
        current.closeFromServer()
        expect(stream.retryDelayMs.value).toBe(delays[index + 1])
      }
    }

    const recovered = MockWebSocket.instances.at(-1)!
    recovered.open()
    expect(stream.reconnectAttempt.value).toBe(0)
    expect(stream.retryDelayMs.value).toBe(0)
    expect(stream.recoveryMessage.value).toBe(
      '事件流已恢复，正在从 #7 后继续接收。',
    )
    wrapper.unmount()
  })

  it('允许用户立即触发排队中的重连', async () => {
    const { stream, wrapper } = mountStream()
    const first = await connect(stream)
    first.open()
    first.closeFromServer()

    stream.reconnectNow()
    await flushPromises()

    expect(MockWebSocket.instances).toHaveLength(2)
    stream.reconnectNow()
    await flushPromises()
    expect(MockWebSocket.instances).toHaveLength(2)
    expect(stream.reconnectAttempt.value).toBe(1)
    await vi.advanceTimersByTimeAsync(1_000)
    expect(MockWebSocket.instances).toHaveLength(2)
    wrapper.unmount()
  })

  it('4401 清理旧会话并经重新认证恢复', async () => {
    apiMocks.getLocalSession
      .mockResolvedValueOnce({
        access_token: 'expired-token',
        token_type: 'Bearer',
        websocket_protocol: 'deskpilot.events.v1',
      })
      .mockResolvedValueOnce({
        access_token: 'fresh-token',
        token_type: 'Bearer',
        websocket_protocol: 'deskpilot.events.v1',
      })
    const { stream, wrapper } = mountStream()
    const first = await connect(stream)
    first.open()
    first.closeFromServer(4401)

    expect(apiMocks.invalidateLocalSession).toHaveBeenCalledOnce()
    expect(stream.connectionState.value).toBe('reauthenticating')
    expect(stream.streamError.value).toContain('重新认证')

    await vi.advanceTimersByTimeAsync(1_000)
    await flushPromises()
    const second = MockWebSocket.instances[1]
    expect(second.protocols).toContain('deskpilot.auth.fresh-token')
    second.open()
    expect(stream.connectionState.value).toBe('connected')
    wrapper.unmount()
  })

  it.each([
    [4403, 'forbidden', '授权'],
    [4404, 'not_found', '不存在'],
  ] as const)('%s 永久停止为 %s 且不再排队重连', async (code, state, message) => {
    const { stream, wrapper } = mountStream()
    const socket = await connect(stream)
    socket.open()
    socket.closeFromServer(code)

    expect(stream.connectionState.value).toBe(state)
    expect(stream.streamError.value).toContain(message)
    expect(stream.retryDelayMs.value).toBe(0)

    stream.reconnectNow()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(MockWebSocket.instances).toHaveLength(1)
    wrapper.unmount()
  })

  it.each(['task.completed', 'task.failed', 'task.cancelled'])(
    '%s 到达后归档、关闭连接且不再重连',
    async (eventType) => {
      const { stream, wrapper } = mountStream()
      const socket = await connect(stream)
      socket.open()
      socket.message(taskEvent(4, eventType))

      expect(stream.connectionState.value).toBe('archived')
      expect(stream.connected.value).toBe(false)
      expect(socket.close).toHaveBeenCalledOnce()
      expect(socket.onclose).toBeNull()

      await vi.advanceTimersByTimeAsync(30_000)
      expect(MockWebSocket.instances).toHaveLength(1)
      wrapper.unmount()
    },
  )

  it('审批事件保持连接，断线后从 required 序号继续回放', async () => {
    const { stream, wrapper } = mountStream()
    const first = await connect(stream)
    first.open()
    first.message(taskEvent(4, 'approval.required'))

    expect(stream.connectionState.value).toBe('connected')
    expect(first.close).not.toHaveBeenCalled()

    first.closeFromServer()
    await vi.advanceTimersByTimeAsync(1_000)
    await flushPromises()
    const second = MockWebSocket.instances[1]
    expect(second.url).toContain('after_seq=4')
    second.open()
    second.message(taskEvent(5, 'approval.resolved'))
    expect(stream.connectionState.value).toBe('connected')
    wrapper.unmount()
  })

  it('reset 拆除全部 handler 和 timer，旧连接无法污染新状态', async () => {
    const { stream, wrapper } = mountStream()
    const socket = await connect(stream)
    socket.open()
    const staleMessage = socket.onmessage!
    const staleClose = socket.onclose!
    socket.closeFromServer()
    expect(stream.reconnectAttempt.value).toBe(1)

    stream.reset()
    expect(stream.connectionState.value).toBe('idle')
    expect(stream.events.value).toEqual([])
    expect(socket.onopen).toBeNull()
    expect(socket.onmessage).toBeNull()
    expect(socket.onerror).toBeNull()
    expect(socket.onclose).toBeNull()

    staleMessage({ data: JSON.stringify(taskEvent(9)) } as MessageEvent)
    staleClose({ code: 1006 } as CloseEvent)
    await vi.advanceTimersByTimeAsync(30_000)

    expect(stream.events.value).toEqual([])
    expect(stream.connectionState.value).toBe('idle')
    expect(MockWebSocket.instances).toHaveLength(1)
    wrapper.unmount()
  })

  it('异步会话返回时用 generation 隔离已切换的任务', async () => {
    let resolveFirst!: (session: LocalSession) => void
    let resolveSecond!: (session: LocalSession) => void
    apiMocks.getLocalSession
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveFirst = resolve
      }))
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveSecond = resolve
      }))
    const { stream, wrapper } = mountStream()

    stream.connect('task-1')
    stream.connect('task-2')
    await flushPromises()
    expect(MockWebSocket.instances).toHaveLength(0)

    resolveFirst({
      access_token: 'stale-token',
      token_type: 'Bearer',
      websocket_protocol: 'deskpilot.events.v1',
    })
    await flushPromises()
    stream.reconnectNow()
    expect(apiMocks.getLocalSession).toHaveBeenCalledTimes(2)

    resolveSecond({
      access_token: 'task-2-token',
      token_type: 'Bearer',
      websocket_protocol: 'deskpilot.events.v1',
    })
    await flushPromises()
    expect(MockWebSocket.instances).toHaveLength(1)
    expect(MockWebSocket.instances[0].url).toContain('/task-2?')
    wrapper.unmount()
  })
})
