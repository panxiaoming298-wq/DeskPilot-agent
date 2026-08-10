import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { TaskEvent } from '../types'
import TaskEventItem from './TaskEventItem.vue'

function makeEvent(
  type: string,
  payload: Record<string, unknown> = { reason: 'user_requested' },
): TaskEvent {
  return {
    event_id: 'event-12',
    task_id: 'task-1',
    seq: 12,
    type,
    timestamp: '2026-08-09T00:00:00Z',
    trace_id: 'trace-1',
    payload,
  }
}

describe('TaskEventItem', () => {
  it('以取消终态标签和危险色展示 task.cancelled', () => {
    const wrapper = mount(TaskEventItem, {
      props: { event: makeEvent('task.cancelled') },
    })

    expect(wrapper.text()).toContain('任务已取消')
    expect(wrapper.text()).toContain('task.cancelled')
    expect(wrapper.attributes('data-tone')).toBe('danger')
  })

  it.each([
    ['tool.failed', '工具执行失败'],
    ['tool.cancelled', '工具执行已取消'],
    ['tool.unknown', '工具结果待核对'],
  ])('以危险色展示工具终态 %s', (type, label) => {
    const wrapper = mount(TaskEventItem, {
      props: { event: makeEvent(type) },
    })

    expect(wrapper.text()).toContain(label)
    expect(wrapper.text()).toContain(type)
    expect(wrapper.attributes('data-tone')).toBe('danger')
  })

  it.each([
    ['approval.required', {}, '需要用户审批', 'warning'],
    ['approval.resolved', { status: 'approved' }, '审批已同意', 'success'],
    ['approval.resolved', { status: 'rejected' }, '审批已拒绝', 'danger'],
    ['approval.resolved', { decision: 'cancelled' }, '审批已取消', 'danger'],
    ['approval.expired', {}, '审批已过期', 'danger'],
    ['approval.invalidated', {}, '审批授权已失效', 'danger'],
  ] as const)('以明确文字和色调展示 %s', (type, payload, label, tone) => {
    const wrapper = mount(TaskEventItem, {
      props: { event: makeEvent(type, payload) },
    })

    expect(wrapper.text()).toContain(label)
    expect(wrapper.attributes('data-tone')).toBe(tone)
  })

  it('未知事件仍显示稳定的原始类型', () => {
    const wrapper = mount(TaskEventItem, {
      props: { event: makeEvent('agent.custom_event') },
    })

    expect(wrapper.text()).toContain('agent.custom_event')
    expect(wrapper.attributes('data-tone')).toBe('neutral')
  })
})
