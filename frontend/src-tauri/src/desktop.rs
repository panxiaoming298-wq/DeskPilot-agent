use std::sync::Arc;

use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, TrayIconBuilder, TrayIconEvent},
    App, AppHandle, Manager, State, Wry,
};

use crate::sidecar::{SidecarLaunchSpec, SidecarStatus, SidecarSupervisor};

const TASK_CAPACITY: u8 = 3;
const SHOW_ITEM_ID: &str = "show-main-window";
const QUIT_ITEM_ID: &str = "quit-deskpilot";

pub struct DesktopState {
    active_tasks_item: MenuItem<Wry>,
    sidecar: SidecarSupervisor,
}

impl DesktopState {
    pub fn shutdown(&self) {
        self.sidecar.shutdown();
    }
}

#[tauri::command]
pub fn update_active_task_count(count: u8, state: State<'_, DesktopState>) -> Result<(), String> {
    if count > TASK_CAPACITY {
        return Err(format!("active task count cannot exceed {TASK_CAPACITY}"));
    }
    state
        .active_tasks_item
        .set_text(format!("活跃任务：{count} / {TASK_CAPACITY}"))
        .map_err(|error| error.to_string())
}

pub fn setup(app: &mut App<Wry>) -> Result<(), Box<dyn std::error::Error>> {
    let handle = app.handle();
    let active_tasks_item = MenuItem::with_id(
        handle,
        "active-task-count",
        format!("活跃任务：0 / {TASK_CAPACITY}"),
        false,
        None::<&str>,
    )?;
    let backend_item = MenuItem::with_id(
        handle,
        "backend-status",
        SidecarStatus::Starting { attempt: 1 }.tray_text(),
        false,
        None::<&str>,
    )?;
    let show_item = MenuItem::with_id(handle, SHOW_ITEM_ID, "打开 DeskPilot", true, None::<&str>)?;
    let quit_item = MenuItem::with_id(handle, QUIT_ITEM_ID, "明确退出", true, None::<&str>)?;
    let menu = Menu::with_items(
        handle,
        &[&active_tasks_item, &backend_item, &show_item, &quit_item],
    )?;

    let app_data = handle.path().app_local_data_dir()?;
    let launch_spec = std::env::current_exe()
        .ok()
        .and_then(|current| SidecarLaunchSpec::resolve(&current, &app_data));
    let status_handle = handle.clone();
    let status_item = backend_item.clone();
    let notifier = Arc::new(move |status: SidecarStatus| {
        let item = status_item.clone();
        let text = status.tray_text();
        let _ = status_handle.run_on_main_thread(move || {
            let _ = item.set_text(text);
        });
    });
    let sidecar = SidecarSupervisor::start(launch_spec, notifier);
    app.manage(DesktopState {
        active_tasks_item,
        sidecar,
    });

    let mut tray = TrayIconBuilder::with_id("deskpilot-main")
        .menu(&menu)
        .tooltip("DeskPilot 本地多 Agent 工作台")
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            SHOW_ITEM_ID => show_main_window(app),
            QUIT_ITEM_ID => {
                app.state::<DesktopState>().shutdown();
                app.exit(0);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if matches!(
                event,
                TrayIconEvent::DoubleClick {
                    button: MouseButton::Left,
                    ..
                }
            ) {
                show_main_window(tray.app_handle());
            }
        });
    if let Some(icon) = handle.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(handle)?;
    Ok(())
}

pub fn show_main_window(app: &AppHandle<Wry>) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

#[cfg(test)]
mod tests {
    use super::TASK_CAPACITY;

    #[test]
    fn desktop_capacity_matches_the_three_task_acceptance_floor() {
        assert_eq!(TASK_CAPACITY, 3);
    }
}
