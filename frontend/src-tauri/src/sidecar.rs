use std::{
    env, fs,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        mpsc::{self, Receiver, RecvTimeoutError, Sender},
        Arc, Mutex,
    },
    thread::{self, JoinHandle},
    time::Duration,
};

const SIDECAR_NAME: &str = "deskpilot-backend-sidecar";
const MAX_RESTARTS: u8 = 3;
const POLL_INTERVAL: Duration = Duration::from_millis(250);

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SidecarStatus {
    Starting { attempt: u8 },
    Running { process_id: u32 },
    Backoff { next_attempt: u8 },
    Missing,
    Failed { exit_code: Option<i32> },
    Stopped,
}

impl SidecarStatus {
    pub fn tray_text(&self) -> String {
        match self {
            Self::Starting { .. } => "本地后端：正在启动".into(),
            Self::Running { .. } => "本地后端：受监督运行中".into(),
            Self::Backoff { .. } => "本地后端：正在安全重启".into(),
            Self::Missing => "本地后端：Sidecar 缺失".into(),
            Self::Failed { .. } => "本地后端：启动失败".into(),
            Self::Stopped => "本地后端：已停止".into(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SidecarLaunchSpec {
    executable: PathBuf,
    working_directory: PathBuf,
}

impl SidecarLaunchSpec {
    pub fn resolve(current_executable: &Path, app_data: &Path) -> Option<Self> {
        let executable_name = if cfg!(windows) {
            format!("{SIDECAR_NAME}.exe")
        } else {
            SIDECAR_NAME.to_owned()
        };
        let executable = current_executable.parent()?.join(executable_name);
        executable.is_file().then(|| Self {
            executable,
            working_directory: app_data.to_path_buf(),
        })
    }
}

enum SupervisorCommand {
    Stop,
}

type StatusNotifier = Arc<dyn Fn(SidecarStatus) + Send + Sync>;

pub struct SidecarSupervisor {
    command_tx: Sender<SupervisorCommand>,
    worker: Mutex<Option<JoinHandle<()>>>,
}

impl SidecarSupervisor {
    pub fn start(spec: Option<SidecarLaunchSpec>, notify: StatusNotifier) -> Self {
        let (command_tx, command_rx) = mpsc::channel();
        let initial_status = if spec.is_some() {
            SidecarStatus::Starting { attempt: 1 }
        } else {
            SidecarStatus::Missing
        };
        let status = Arc::new(Mutex::new(initial_status.clone()));
        notify(initial_status);

        let worker = spec.map(|launch_spec| {
            let worker_status = Arc::clone(&status);
            thread::Builder::new()
                .name("deskpilot-sidecar-supervisor".into())
                .spawn(move || supervise(launch_spec, command_rx, worker_status, notify))
                .expect("failed to start DeskPilot sidecar supervisor")
        });

        Self {
            command_tx,
            worker: Mutex::new(worker),
        }
    }

    pub fn shutdown(&self) {
        let _ = self.command_tx.send(SupervisorCommand::Stop);
        if let Some(worker) = self.worker.lock().expect("sidecar worker poisoned").take() {
            let _ = worker.join();
        }
    }
}

impl Drop for SidecarSupervisor {
    fn drop(&mut self) {
        let _ = self.command_tx.send(SupervisorCommand::Stop);
        if let Ok(worker_slot) = self.worker.get_mut() {
            if let Some(worker) = worker_slot.take() {
                let _ = worker.join();
            }
        }
    }
}

fn publish(
    status_store: &Arc<Mutex<SidecarStatus>>,
    notify: &StatusNotifier,
    status: SidecarStatus,
) {
    *status_store.lock().expect("sidecar status poisoned") = status.clone();
    notify(status);
}

fn supervise(
    spec: SidecarLaunchSpec,
    command_rx: Receiver<SupervisorCommand>,
    status: Arc<Mutex<SidecarStatus>>,
    notify: StatusNotifier,
) {
    for attempt in 1..=MAX_RESTARTS + 1 {
        publish(&status, &notify, SidecarStatus::Starting { attempt });
        let mut child = match spawn_sidecar(&spec) {
            Ok(child) => child,
            Err(_) if attempt <= MAX_RESTARTS => {
                if wait_for_retry(&command_rx, attempt + 1, &status, &notify) {
                    publish(&status, &notify, SidecarStatus::Stopped);
                    return;
                }
                continue;
            }
            Err(_) => {
                publish(&status, &notify, SidecarStatus::Failed { exit_code: None });
                return;
            }
        };

        publish(
            &status,
            &notify,
            SidecarStatus::Running {
                process_id: child.id(),
            },
        );
        let exit_code = loop {
            match command_rx.recv_timeout(POLL_INTERVAL) {
                Ok(SupervisorCommand::Stop) | Err(RecvTimeoutError::Disconnected) => {
                    terminate_process_tree(&mut child);
                    publish(&status, &notify, SidecarStatus::Stopped);
                    return;
                }
                Err(RecvTimeoutError::Timeout) => match child.try_wait() {
                    Ok(Some(exit)) => break exit.code(),
                    Ok(None) => {}
                    Err(_) => break None,
                },
            }
        };

        if attempt > MAX_RESTARTS {
            publish(&status, &notify, SidecarStatus::Failed { exit_code });
            return;
        }
        if wait_for_retry(&command_rx, attempt + 1, &status, &notify) {
            publish(&status, &notify, SidecarStatus::Stopped);
            return;
        }
    }
}

fn wait_for_retry(
    command_rx: &Receiver<SupervisorCommand>,
    next_attempt: u8,
    status: &Arc<Mutex<SidecarStatus>>,
    notify: &StatusNotifier,
) -> bool {
    publish(status, notify, SidecarStatus::Backoff { next_attempt });
    let delay = Duration::from_millis(250 * u64::from(next_attempt - 1));
    matches!(
        command_rx.recv_timeout(delay),
        Ok(SupervisorCommand::Stop) | Err(RecvTimeoutError::Disconnected)
    )
}

fn spawn_sidecar(spec: &SidecarLaunchSpec) -> std::io::Result<Child> {
    fs::create_dir_all(&spec.working_directory)?;
    let mut command = Command::new(&spec.executable);
    command
        .current_dir(&spec.working_directory)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .env_clear();

    for key in [
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "TEMP",
        "TMP",
        "LOCALAPPDATA",
        "APPDATA",
    ] {
        if let Some(value) = env::var_os(key) {
            command.env(key, value);
        }
    }
    command
        .env("PYTHONNOUSERSITE", "1")
        .env("PYTHONUNBUFFERED", "1")
        .env("DESKPILOT_EVENT_TRANSPORT", "local")
        .env("DESKPILOT_MODEL_ADMISSION_ALLOW", "false")
        .env("DESKPILOT_RESEARCH_RUNTIME_ENABLED", "false");

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000 | 0x0000_0200);
    }

    command.spawn()
}

#[cfg(windows)]
fn terminate_process_tree(child: &mut Child) {
    use std::os::windows::process::CommandExt;

    let system_root = env::var_os("SYSTEMROOT").unwrap_or_else(|| "C:\\Windows".into());
    let taskkill = PathBuf::from(system_root)
        .join("System32")
        .join("taskkill.exe");
    let status = Command::new(taskkill)
        .args(["/PID", &child.id().to_string(), "/T", "/F"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .creation_flags(0x0800_0000)
        .status();
    if status.is_err() {
        let _ = child.kill();
    }
    let _ = child.wait();
}

#[cfg(not(windows))]
fn terminate_process_tree(child: &mut Child) {
    let _ = child.kill();
    let _ = child.wait();
}

#[cfg(test)]
mod tests {
    use super::{SidecarLaunchSpec, SidecarStatus, SIDECAR_NAME};
    use std::{fs, path::PathBuf};

    #[test]
    fn status_text_never_exposes_process_details() {
        assert_eq!(
            SidecarStatus::Running { process_id: 42 }.tray_text(),
            "本地后端：受监督运行中"
        );
        assert!(!SidecarStatus::Failed { exit_code: Some(7) }
            .tray_text()
            .contains('7'));
    }

    #[test]
    fn launch_spec_only_accepts_the_fixed_sibling_binary() {
        let root =
            std::env::temp_dir().join(format!("deskpilot-sidecar-spec-{}", std::process::id()));
        let executable = root.join("deskpilot.exe");
        let data = root.join("data");
        fs::create_dir_all(&root).expect("create test directory");
        fs::write(&executable, b"desktop").expect("write desktop placeholder");
        assert!(SidecarLaunchSpec::resolve(&executable, &data).is_none());

        let sidecar_name = if cfg!(windows) {
            format!("{SIDECAR_NAME}.exe")
        } else {
            SIDECAR_NAME.to_owned()
        };
        let sidecar = root.join(sidecar_name);
        fs::write(&sidecar, b"sidecar").expect("write sidecar placeholder");
        let resolved =
            SidecarLaunchSpec::resolve(&executable, &data).expect("resolve fixed sidecar");

        assert_eq!(resolved.executable, sidecar);
        assert_eq!(resolved.working_directory, PathBuf::from(&data));
        fs::remove_dir_all(root).expect("remove test directory");
    }
}
