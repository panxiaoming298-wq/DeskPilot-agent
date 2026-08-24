import { beforeEach, describe, expect, it, vi } from 'vitest'

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }))

vi.mock('@tauri-apps/api/core', () => ({
  invoke: invokeMock,
}))

describe('desktop runtime bridge', () => {
  beforeEach(() => {
    invokeMock.mockReset()
    delete (window as Window & { __TAURI_INTERNALS__?: object }).__TAURI_INTERNALS__
  })

  it('does nothing in the browser runtime', async () => {
    const { syncDesktopActiveTaskCount } = await import('./desktopRuntime')

    await expect(syncDesktopActiveTaskCount(2)).resolves.toBe(false)
    expect(invokeMock).not.toHaveBeenCalled()
  })

  it('updates the tray with an exact bounded active-task count', async () => {
    ;(window as Window & { __TAURI_INTERNALS__?: object }).__TAURI_INTERNALS__ = {}
    invokeMock.mockResolvedValue(undefined)
    const { syncDesktopActiveTaskCount } = await import('./desktopRuntime')

    await expect(syncDesktopActiveTaskCount(3)).resolves.toBe(true)
    expect(invokeMock).toHaveBeenCalledWith('update_active_task_count', { count: 3 })
  })

  it('rejects invalid counts and contains desktop bridge failures', async () => {
    ;(window as Window & { __TAURI_INTERNALS__?: object }).__TAURI_INTERNALS__ = {}
    invokeMock.mockRejectedValue(new Error('desktop bridge unavailable'))
    const { syncDesktopActiveTaskCount } = await import('./desktopRuntime')

    await expect(syncDesktopActiveTaskCount(4)).resolves.toBe(false)
    await expect(syncDesktopActiveTaskCount(1)).resolves.toBe(false)
    expect(invokeMock).toHaveBeenCalledTimes(1)
  })
})
