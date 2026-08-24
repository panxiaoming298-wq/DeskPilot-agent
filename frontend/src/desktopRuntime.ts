import { invoke } from '@tauri-apps/api/core'

export const DESKTOP_TASK_CAPACITY = 3

export function isTauriRuntime(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
}

export async function syncDesktopActiveTaskCount(count: number): Promise<boolean> {
  if (
    !isTauriRuntime()
    || !Number.isInteger(count)
    || count < 0
    || count > DESKTOP_TASK_CAPACITY
  ) {
    return false
  }

  try {
    await invoke('update_active_task_count', { count })
    return true
  } catch {
    return false
  }
}
