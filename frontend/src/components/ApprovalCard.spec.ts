import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { Approval } from '../types'
import ApprovalCard from './ApprovalCard.vue'

function makeApproval(overrides: Partial<Approval> = {}): Approval {
  return {
    approval_id: 'approval-1',
    decision_id: 'decision-1',
    task_id: 'task-1',
    call_id: 'call-1',
    status: 'pending',
    decision: null,
    preview_hash: 'sha256:preview-1',
    title: '将读取工作区磁盘使用情况',
    purpose: '用于验证任务的本地工具闭环',
    tool_name: 'computer.disk_usage',
    tool_version: '1.0.0',
    risk_level: 'R1',
    capabilities: ['filesystem.metadata.read'],
    resource_scope: [{
      kind: 'filesystem_path',
      label: 'D:\\workspace',
      operations: ['read_metadata'],
      version: 'volume:abc',
    }],
    consequences: ['会读取目标卷的容量元数据'],
    reversible: true,
    data_egress: { enabled: false, destination: null },
    policy_rule_id: 'risk-r1-ask',
    policy_revision: 'deskpilot-policy-v1',
    reason_code: 'RISK_REQUIRES_APPROVAL',
    requested_at: '2026-08-09T08:00:00Z',
    expires_at: '2026-08-09T08:05:00Z',
    resolved_at: null,
    consumed_at: null,
    resolution_reason: null,
    updated_at: '2026-08-09T08:00:00Z',
    ...overrides,
  }
}

function mountCard(overrides: Record<string, unknown> = {}) {
  return mount(ApprovalCard, {
    props: {
      approval: makeApproval(),
      loading: false,
      activeAction: null,
      message: null,
      error: null,
      disabled: false,
      ...overrides,
    },
  })
}

describe('ApprovalCard', () => {
  it('展示规范化资源、风险、后果、外发和有效期', () => {
    const wrapper = mountCard()

    expect(wrapper.text()).toContain('将读取工作区磁盘使用情况')
    expect(wrapper.text()).toContain('用于验证任务的本地工具闭环')
    expect(wrapper.text()).toContain('computer.disk_usage@1.0.0')
    expect(wrapper.text()).toContain('R1')
    expect(wrapper.text()).toContain('D:\\workspace')
    expect(wrapper.text()).toContain('read_metadata')
    expect(wrapper.text()).toContain('filesystem.metadata.read')
    expect(wrapper.text()).toContain('不会离开本机')
    expect(wrapper.text()).toContain('仅本次有效')
  })

  it('只在用户点击后发出精确决定和规范化拒绝原因', async () => {
    const wrapper = mountCard()

    await wrapper.get('[data-testid="approve-approval"]').trigger('click')
    expect(wrapper.emitted('resolve')).toEqual([['approve', undefined]])

    await wrapper.get('textarea').setValue('  目标不正确  ')
    await wrapper.get('[data-testid="reject-approval"]').trigger('click')
    expect(wrapper.emitted('resolve')).toEqual([
      ['approve', undefined],
      ['reject', '目标不正确'],
    ])
  })

  it('审批请求或其他任务命令进行时禁用整组操作', async () => {
    const wrapper = mountCard({ activeAction: 'approve' })

    expect(wrapper.get('[aria-busy="true"]').attributes('aria-busy')).toBe('true')
    expect(wrapper.get('[data-testid="approve-approval"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="reject-approval"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('textarea').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('正在确认…')

    await wrapper.get('[data-testid="reject-approval"]').trigger('click')
    expect(wrapper.emitted('resolve')).toBeUndefined()

    await wrapper.setProps({ activeAction: null, disabled: true })
    expect(wrapper.get('[data-testid="approve-approval"]').attributes('disabled')).toBeDefined()
  })

  it.each([
    ['approved', 'approved', '已同意', '已仅为本次调用授权'],
    ['rejected', 'rejected', '已拒绝', '本次操作已拒绝'],
    ['expired', null, '已过期', '旧预览不能再用于授权'],
    ['cancelled', null, '已取消', '已随任务取消'],
  ] as const)('%s 终态只读且说明执行语义', (status, decision, label, message) => {
    const wrapper = mountCard({
      approval: makeApproval({ status, decision, resolved_at: '2026-08-09T08:01:00Z' }),
    })

    expect(wrapper.text()).toContain(label)
    expect(wrapper.text()).toContain(message)
    expect(wrapper.find('textarea').exists()).toBe(false)
    expect(wrapper.find('[data-testid="approve-approval"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="reject-approval"]').exists()).toBe(false)
  })

  it.each([
    ['expired', '授权在执行前已过期'],
    ['cancelled', '授权在执行前已取消'],
  ] as const)('保留已同意审计并解释 %s 生命周期终态', (status, message) => {
    const wrapper = mountCard({
      approval: makeApproval({
        status,
        decision: 'approved',
        resolved_at: '2026-08-09T08:01:00Z',
      }),
    })

    expect(wrapper.text()).toContain('你曾同意本次操作')
    expect(wrapper.text()).toContain(message)
    expect(wrapper.text()).toContain('工具不会执行')
  })

  it('使用状态与告警语义反馈读取和操作结果', () => {
    const loading = mountCard({ approval: null, loading: true })
    expect(loading.get('[role="status"]').text()).toContain('正在读取')

    const feedback = mountCard({
      message: '已仅为本次操作授权。',
      error: '审批内容已变化。',
    })
    expect(feedback.findAll('[role="status"]').some((item) => item.text().includes('已仅为'))).toBe(true)
    expect(feedback.get('[role="alert"]').text()).toContain('审批内容已变化')
  })
})
