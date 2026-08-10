import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import TaskControls from './TaskControls.vue'

function mountControls(
  props: Partial<InstanceType<typeof TaskControls>['$props']> = {},
) {
  return mount(TaskControls, {
    props: {
      status: 'running',
      activeAction: null,
      message: null,
      error: null,
      disabled: false,
      ...props,
    },
  })
}

describe('TaskControls', () => {
  it('运行中可暂停，取消需要二次确认', async () => {
    const wrapper = mountControls()

    await wrapper.get('[data-testid="pause-task"]').trigger('click')
    expect(wrapper.emitted('control')).toEqual([['pause']])

    await wrapper.get('[data-testid="cancel-task"]').trigger('click')
    expect(wrapper.find('[role="alertdialog"]').exists()).toBe(true)
    expect(wrapper.emitted('control')).toHaveLength(1)

    await wrapper.get('[data-testid="confirm-cancel"]').trigger('click')
    expect(wrapper.emitted('control')).toEqual([['pause'], ['cancel']])
  })

  it('暂停后只提供继续和取消操作', async () => {
    const wrapper = mountControls({ status: 'paused' })

    expect(wrapper.find('[data-testid="pause-task"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="resume-task"]').exists()).toBe(true)

    await wrapper.get('[data-testid="resume-task"]').trigger('click')
    expect(wrapper.emitted('control')).toEqual([['resume']])
  })

  it.each(['created', 'classifying', 'waiting_approval'] as const)(
    '%s 状态只能取消',
    (status) => {
      const wrapper = mountControls({ status })

      expect(wrapper.find('[data-testid="pause-task"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="resume-task"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="cancel-task"]').exists()).toBe(true)
    },
  )

  it('命令执行期间禁用整组控制并公布进度', async () => {
    const wrapper = mountControls({ activeAction: 'pause' })

    expect(wrapper.get('[data-testid="pause-task"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="cancel-task"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[aria-busy="true"]').attributes('aria-busy')).toBe('true')
    expect(wrapper.get('[role="status"]').text()).toBe('正在暂停…')

    await wrapper.get('[data-testid="pause-task"]').trigger('click')
    expect(wrapper.emitted('control')).toBeUndefined()
  })

  it('取消处理中保留确认区并禁用按钮', async () => {
    const wrapper = mountControls()
    await wrapper.get('[data-testid="cancel-task"]').trigger('click')
    await wrapper.setProps({ activeAction: 'cancel' })

    expect(wrapper.get('[data-testid="confirm-cancel"]').text()).toBe('正在取消…')
    expect(wrapper.get('[data-testid="confirm-cancel"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="dismiss-cancel"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[role="status"]').text()).toBe('正在取消…')
  })

  it('审批决定进行时禁用任务命令，不发出并发控制', async () => {
    const wrapper = mountControls({ status: 'waiting_approval', disabled: true })

    expect(wrapper.get('[data-testid="cancel-task"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[aria-busy="true"]').attributes('aria-busy')).toBe('true')
    await wrapper.get('[data-testid="cancel-task"]').trigger('click')
    expect(wrapper.find('[role="alertdialog"]').exists()).toBe(false)
    expect(wrapper.emitted('control')).toBeUndefined()
  })

  it.each(['succeeded', 'failed', 'cancelled'] as const)(
    '%s 终态不提供任务命令',
    (status) => {
      const wrapper = mountControls({ status })

      expect(wrapper.find('button').exists()).toBe(false)
      expect(wrapper.text()).toContain('任务已结束')
    },
  )

  it('以状态与告警语义展示反馈', () => {
    const wrapper = mountControls({
      message: '任务已暂停。',
      error: '任务暂停失败。',
    })

    expect(wrapper.get('[role="status"]').text()).toBe('任务已暂停。')
    expect(wrapper.get('[role="alert"]').text()).toBe('任务暂停失败。')
  })
})
