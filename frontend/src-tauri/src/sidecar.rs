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

#[cfg(test)]
use std::ffi::OsString;

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
    #[cfg(test)]
    arguments: Vec<OsString>,
    #[cfg(test)]
    environment: Vec<(OsString, OsString)>,
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
            #[cfg(test)]
            arguments: Vec::new(),
            #[cfg(test)]
            environment: Vec::new(),
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

    #[cfg(test)]
    {
        command.args(&spec.arguments);
        for (key, value) in &spec.environment {
            command.env(key, value);
        }
    }

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
    use super::{SidecarLaunchSpec, SidecarStatus, SidecarSupervisor, MAX_RESTARTS, SIDECAR_NAME};
    use serde_json::{json, Value};
    use std::{
        ffi::OsString,
        fs,
        io::{Read, Write},
        net::{TcpListener, TcpStream},
        path::{Path, PathBuf},
        process::{Command, Stdio},
        sync::{Arc, Mutex},
        thread,
        time::{Duration, Instant, SystemTime, UNIX_EPOCH},
    };

    const TEST_TOKEN: &str = "stage116b-sidecar-token-0000000000";
    const TEST_ORIGIN: &str = "http://stage116b-sidecar.local";

    fn wait_until<T>(timeout: Duration, mut probe: impl FnMut() -> Option<T>) -> T {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if let Some(value) = probe() {
                return value;
            }
            thread::sleep(Duration::from_millis(50));
        }
        panic!("timed out waiting for supervised sidecar state")
    }

    fn free_port() -> u16 {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("reserve sidecar test port");
        listener
            .local_addr()
            .expect("read sidecar test port")
            .port()
    }

    fn request_json(
        port: u16,
        method: &str,
        path: &str,
        body: Option<&Value>,
    ) -> Result<(u16, Value), String> {
        let payload = body.map(Value::to_string).unwrap_or_default();
        let mut stream = TcpStream::connect(("127.0.0.1", port))
            .map_err(|error| format!("connect failed: {error}"))?;
        stream
            .set_read_timeout(Some(Duration::from_secs(10)))
            .map_err(|error| format!("read timeout failed: {error}"))?;
        stream
            .set_write_timeout(Some(Duration::from_secs(10)))
            .map_err(|error| format!("write timeout failed: {error}"))?;
        let request = format!(
            "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {TEST_TOKEN}\r\nOrigin: {TEST_ORIGIN}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{payload}",
            payload.len()
        );
        stream
            .write_all(request.as_bytes())
            .map_err(|error| format!("request failed: {error}"))?;
        let mut response = Vec::new();
        stream
            .read_to_end(&mut response)
            .map_err(|error| format!("response failed: {error}"))?;
        let response = String::from_utf8(response)
            .map_err(|error| format!("response was not UTF-8: {error}"))?;
        let (head, response_body) = response
            .split_once("\r\n\r\n")
            .ok_or_else(|| "response had no header boundary".to_owned())?;
        let status = head
            .lines()
            .next()
            .and_then(|line| line.split_whitespace().nth(1))
            .and_then(|value| value.parse::<u16>().ok())
            .ok_or_else(|| "response status was invalid".to_owned())?;
        let value = if response_body.is_empty() {
            Value::Null
        } else {
            serde_json::from_str(response_body)
                .map_err(|error| format!("response JSON was invalid: {error}"))?
        };
        Ok((status, value))
    }

    fn expect_json(
        port: u16,
        method: &str,
        path: &str,
        body: Option<&Value>,
        expected_status: u16,
    ) -> Value {
        let (status, value) =
            request_json(port, method, path, body).expect("call supervised sidecar API");
        assert_eq!(status, expected_status, "unexpected API response: {value}");
        value
    }

    fn wait_healthy(port: u16) {
        wait_until(Duration::from_secs(60), || {
            request_json(port, "GET", "/api/v1/health", None)
                .ok()
                .filter(|(status, _)| *status == 200)
        });
    }

    fn load_sidecar_scenario(python: &Path, backend_root: &Path) -> Value {
        let script = concat!(
            "from deskpilot.application.workspace_coding_evaluation import ",
            "WorkspaceCodingGoldenSidecarSoakSuiteLoader; ",
            "print(WorkspaceCodingGoldenSidecarSoakSuiteLoader().load()",
            ".suite.model_dump_json())"
        );
        let output = Command::new(python)
            .current_dir(backend_root)
            .args(["-c", script])
            .output()
            .expect("load sidecar soak suite");
        assert!(
            output.status.success(),
            "sidecar suite loading failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        serde_json::from_slice(&output.stdout).expect("parse sidecar soak suite")
    }

    fn read_string_list(path: &Path) -> Vec<String> {
        if !path.exists() {
            return Vec::new();
        }
        serde_json::from_str(&fs::read_to_string(path).expect("read sidecar call ledger"))
            .expect("parse sidecar call ledger")
    }

    fn first_command_is_ready(workbench: &Value) -> bool {
        workbench["task_loop"]["nodes"]
            .as_array()
            .and_then(|nodes| {
                nodes
                    .iter()
                    .filter(|node| !node["command_plan_id"].is_null())
                    .min_by_key(|node| node["command_step_sequence"].as_u64())
            })
            .is_some_and(|node| node["status"] == "ready")
    }

    fn durable_command_state(workbench: &Value) -> Value {
        let mut nodes = workbench["task_loop"]["nodes"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|node| !node["command_plan_id"].is_null())
            .map(|node| {
                json!({
                    "sequence": node["command_step_sequence"],
                    "profile_id": node["command_profile_id"],
                    "status": node["status"],
                    "attempt_count": node["attempt_count"],
                    "verified_result_present": node["verified_result_present"],
                    "verified_failure_result_count": node["verified_failure_result_count"],
                })
            })
            .collect::<Vec<_>>();
        nodes.sort_by_key(|node| node["sequence"].as_u64());
        json!({
            "task_status": workbench["task"]["status"],
            "execution_status": workbench["task_loop"]["execution_status"],
            "nodes": nodes,
        })
    }

    fn observe_stable_command_state(
        port: u16,
        task_id: &str,
        expected_state: &Value,
        observation_seconds: u64,
        poll_interval_ms: u64,
    ) {
        let samples = observation_seconds * 1_000 / poll_interval_ms;
        let started = Instant::now();
        for _ in 0..samples {
            let workbench = expect_json(
                port,
                "GET",
                &format!("/api/v1/tasks/{task_id}/workbench"),
                None,
                200,
            );
            assert_eq!(durable_command_state(&workbench), *expected_state);
            thread::sleep(Duration::from_millis(poll_interval_ms));
        }
        assert!(started.elapsed() >= Duration::from_secs(observation_seconds));
    }

    fn latest_running_pid(statuses: &Arc<Mutex<Vec<SidecarStatus>>>, except: u32) -> Option<u32> {
        statuses
            .lock()
            .expect("sidecar statuses poisoned")
            .iter()
            .rev()
            .find_map(|status| match status {
                SidecarStatus::Running { process_id } if *process_id != except => Some(*process_id),
                _ => None,
            })
    }

    fn kill_process_tree(process_id: u32) {
        let system_root = std::env::var_os("SYSTEMROOT").unwrap_or_else(|| "C:\\Windows".into());
        let taskkill = PathBuf::from(system_root)
            .join("System32")
            .join("taskkill.exe");
        let status = Command::new(taskkill)
            .args(["/PID", &process_id.to_string(), "/T", "/F"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .expect("force-kill supervised sidecar");
        assert!(status.success());
    }

    fn test_environment(entries: &[(&str, String)]) -> Vec<(OsString, OsString)> {
        entries
            .iter()
            .map(|(key, value)| (OsString::from(key), OsString::from(value)))
            .collect()
    }

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

    #[cfg(windows)]
    #[test]
    fn real_supervisor_restart_recovers_public_command_task_without_browser_progress() {
        let manifest_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let backend_root = manifest_root
            .join("..")
            .join("..")
            .join("backend")
            .canonicalize()
            .expect("resolve backend root");
        let python = backend_root
            .join(".venv")
            .join("Scripts")
            .join("python.exe");
        assert!(python.is_file(), "backend virtual environment is required");
        let suite = load_sidecar_scenario(&python, &backend_root);
        let scenario = &suite["scenario"];
        let observation_seconds = scenario["observation_seconds"]
            .as_u64()
            .expect("sidecar observation seconds");
        let poll_interval_ms = scenario["poll_interval_ms"]
            .as_u64()
            .expect("sidecar poll interval");
        let max_advances = scenario["max_advances"]
            .as_u64()
            .expect("sidecar max advances");
        assert_eq!(
            scenario["supervisor_restart_budget"].as_u64(),
            Some(u64::from(MAX_RESTARTS))
        );
        assert_eq!(scenario["automatic_workbench_after_restart"], true);

        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock before epoch")
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "deskpilot-sidecar-soak-{}-{unique}",
            std::process::id()
        ));
        let workspace_root = root.join("workspace");
        let project_path = scenario["command_project_path"]
            .as_str()
            .expect("sidecar project path");
        let project = workspace_root.join(project_path);
        fs::create_dir_all(&project).expect("create sidecar workspace");
        fs::write(project.join("sample.py"), b"VALUE: int = 1\n")
            .expect("write sidecar workspace fixture");
        let control_path = root.join("workbench-runtime.txt");
        fs::write(&control_path, b"false").expect("disable automatic Workbench initially");
        let command_calls = root.join("command-calls.json");
        let provider_calls = root.join("provider-calls.json");
        let command_started = root.join("command-started.txt");
        let port = free_port();
        let profile_ids = scenario["command_profile_ids"]
            .as_array()
            .expect("sidecar command profiles")
            .iter()
            .map(|value| value.as_str().expect("command profile string"))
            .collect::<Vec<_>>();
        let environment = test_environment(&[
            (
                "DESKPILOT_DATABASE_URL",
                format!(
                    "sqlite+aiosqlite:///{}",
                    root.join("sidecar.db").to_string_lossy().replace('\\', "/")
                ),
            ),
            (
                "DESKPILOT_ARTIFACT_WORKSPACE_ROOT",
                root.join("artifacts").to_string_lossy().into_owned(),
            ),
            (
                "DESKPILOT_CONVERSATION_WORKSPACE_ROOT",
                workspace_root.to_string_lossy().into_owned(),
            ),
            ("DESKPILOT_SESSION_TOKEN", TEST_TOKEN.to_owned()),
            ("DESKPILOT_CORS_ORIGINS", format!("[\"{TEST_ORIGIN}\"]")),
            (
                "DESKPILOT_RUNNER_COMMIT_RECEIPT_DATABASE_PATH",
                root.join("receipts.db").to_string_lossy().into_owned(),
            ),
            (
                "DESKPILOT_RUNNER_WORKER_RUNTIME_ROOT",
                root.join("worker-runtime").to_string_lossy().into_owned(),
            ),
            (
                "DESKPILOT_RUNNER_APPCONTAINER_PROFILE_JOURNAL_PATH",
                root.join("appcontainer-profiles.json")
                    .to_string_lossy()
                    .into_owned(),
            ),
            (
                "DESKPILOT_MODEL_GATEWAY_POLICY",
                "{\"provider_pricing\":[{\"provider_id\":\"fake-local\"}]}".to_owned(),
            ),
            ("DESKPILOT_RESEARCH_RUNTIME_ENABLED", "false".to_owned()),
            ("DESKPILOT_FAKE_STEP_DELAY_SECONDS", "0.001".to_owned()),
            ("DESKPILOT_GOLDEN_API_PORT", port.to_string()),
            ("DESKPILOT_GOLDEN_COMMAND_PROJECT", project_path.to_owned()),
            ("DESKPILOT_GOLDEN_COMMAND_PROFILES", profile_ids.join(",")),
            ("DESKPILOT_GOLDEN_COMMAND_FAULT_MODE", "pass".to_owned()),
            ("DESKPILOT_GOLDEN_PROFILE_DRIFT", "none".to_owned()),
            (
                "DESKPILOT_GOLDEN_COMMAND_CALLS_PATH",
                command_calls.to_string_lossy().into_owned(),
            ),
            (
                "DESKPILOT_GOLDEN_PROVIDER_CALLS_PATH",
                provider_calls.to_string_lossy().into_owned(),
            ),
            (
                "DESKPILOT_GOLDEN_COMMAND_STARTED_PATH",
                command_started.to_string_lossy().into_owned(),
            ),
            (
                "DESKPILOT_GOLDEN_WORKBENCH_RUNTIME_CONTROL_PATH",
                control_path.to_string_lossy().into_owned(),
            ),
            ("PYTHONUTF8", "1".to_owned()),
        ]);
        let spec = SidecarLaunchSpec {
            executable: python,
            working_directory: backend_root,
            arguments: vec![
                OsString::from("-m"),
                OsString::from("tests.fixtures.workspace_command_fault_server"),
            ],
            environment,
        };
        let statuses = Arc::new(Mutex::new(Vec::new()));
        let observed_statuses = Arc::clone(&statuses);
        let supervisor = SidecarSupervisor::start(
            Some(spec),
            Arc::new(move |status| {
                observed_statuses
                    .lock()
                    .expect("sidecar statuses poisoned")
                    .push(status);
            }),
        );

        let first_pid = wait_until(Duration::from_secs(10), || latest_running_pid(&statuses, 0));
        wait_healthy(port);
        let mut workbench = expect_json(
            port,
            "POST",
            "/api/v1/conversation-turns",
            Some(&json!({"message": "运行 backend 的固定 Ruff 与 mypy 检查"})),
            201,
        );
        let task_id = workbench["task"]["task_id"]
            .as_str()
            .expect("sidecar task id")
            .to_owned();
        for _ in 0..max_advances {
            if first_command_is_ready(&workbench) {
                break;
            }
            workbench = expect_json(
                port,
                "POST",
                &format!("/api/v1/tasks/{task_id}/workbench:advance"),
                None,
                200,
            );
        }
        assert!(first_command_is_ready(&workbench));
        let ready_state = durable_command_state(&workbench);
        observe_stable_command_state(
            port,
            &task_id,
            &ready_state,
            observation_seconds,
            poll_interval_ms,
        );
        assert_eq!(read_string_list(&provider_calls).len(), 1);
        assert!(read_string_list(&command_calls).is_empty());

        fs::write(&control_path, b"true").expect("enable automatic Workbench on restart");
        kill_process_tree(first_pid);
        let second_pid = wait_until(Duration::from_secs(30), || {
            latest_running_pid(&statuses, first_pid)
        });
        assert_ne!(first_pid, second_pid);
        wait_healthy(port);
        let completed = wait_until(Duration::from_secs(60), || {
            request_json(
                port,
                "GET",
                &format!("/api/v1/tasks/{task_id}/workbench"),
                None,
            )
            .ok()
            .filter(|(status, workbench)| {
                *status == 200
                    && workbench["task_loop"]["execution_status"] == "succeeded"
                    && workbench["task"]["status"] == "succeeded"
            })
            .map(|(_, workbench)| workbench)
        });
        let completed_state = durable_command_state(&completed);
        observe_stable_command_state(
            port,
            &task_id,
            &completed_state,
            observation_seconds,
            poll_interval_ms,
        );
        assert_eq!(read_string_list(&provider_calls).len(), 1);
        assert_eq!(read_string_list(&command_calls), profile_ids);

        supervisor.shutdown();
        wait_until(Duration::from_secs(10), || {
            request_json(port, "GET", "/api/v1/health", None)
                .is_err()
                .then_some(())
        });
        let status_snapshot = statuses.lock().expect("sidecar statuses poisoned").clone();
        let mut running_pids = Vec::new();
        for status in &status_snapshot {
            if let SidecarStatus::Running { process_id } = status {
                if !running_pids.contains(process_id) {
                    running_pids.push(*process_id);
                }
            }
        }
        assert_eq!(
            running_pids.len(),
            scenario["expected_process_generations"]
                .as_u64()
                .expect("expected process generations") as usize
        );
        assert_eq!(
            status_snapshot
                .iter()
                .filter(|status| matches!(status, SidecarStatus::Backoff { .. }))
                .count(),
            scenario["expected_restart_count"]
                .as_u64()
                .expect("expected restart count") as usize
        );
        assert!(!status_snapshot
            .iter()
            .any(|status| matches!(status, SidecarStatus::Failed { .. })));
        assert_eq!(status_snapshot.last(), Some(&SidecarStatus::Stopped));
        fs::remove_dir_all(root).expect("remove sidecar soak directory");
    }
}
