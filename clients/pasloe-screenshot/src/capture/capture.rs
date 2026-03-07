use anyhow::Result;
use std::collections::HashMap;
use tokio::sync::mpsc::Sender;
use tokio::task::JoinHandle;
use tokio::time::{Duration, sleep};
use tokio_util::sync::CancellationToken;
use tracing::{debug, error, info, warn};
use xcap::Monitor;

use crate::capture::monitor::SafeMonitor;
use crate::capture::window::SafeWindow;
use crate::config::{MonitorConfig, WindowConfig};
use crate::event::CaptureResult;

/// 统一的截图管理器
///
/// 管理多个显示器和窗口的并发截图任务
pub struct Capture {
    monitor_configs: HashMap<String, MonitorConfig>,
    window_config: Option<WindowConfig>,
    cancellation_token: CancellationToken,
    task_handles: Option<Vec<JoinHandle<()>>>,
}

impl Capture {
    pub fn new(
        monitor_configs: HashMap<String, MonitorConfig>,
        window_config: Option<WindowConfig>,
    ) -> Self {
        let mut configs = HashMap::new();

        info!("Initializing monitor configurations...");
        for (monitor_id, config) in monitor_configs {
            if config.enable {
                info!(
                    "Initialized monitor configuration for {}: {}",
                    monitor_id, &config
                );
                configs.insert(monitor_id, config);
            }
        }
        info!("Initialized monitor configurations: {}", configs.len());

        info!("Initializing window configuration...");
        if let Some(ref config) = window_config {
            if config.enable {
                info!("Initialized window configuration: {}", config);
            }
        }
        info!("Initialized window configuration");

        Self {
            monitor_configs: configs,
            window_config,
            cancellation_token: CancellationToken::new(),
            task_handles: None,
        }
    }

    /// 启动所有截图任务(包括监视器和窗口)
    ///
    /// 返回成功启动的任务数量
    pub fn start_capture(&mut self, sender: Sender<CaptureResult>) -> usize {
        let mut handles = Vec::new();

        // 启动所有监视器任务
        for (monitor_id, config) in &self.monitor_configs {
            info!("Starting monitor capture loop for {}", monitor_id);
            let monitor = match SafeMonitor::new(monitor_id.clone()) {
                Ok(m) => m,
                Err(e) => {
                    warn!("Failed to init monitor {}: {}", monitor_id, e);
                    continue;
                }
            };

            let monitor_id = monitor_id.clone();
            let sender = sender.clone();
            let config = config.clone();
            let cancel_token = self.cancellation_token.child_token();

            let handle = tokio::spawn(async move {
                Self::monitor_task(monitor, monitor_id, sender, config, cancel_token).await;
            });

            handles.push(handle);
        }

        // 启动窗口任务(如果启用)
        info!("Starting window capture loop");
        if let Some(config) = &self.window_config {
            if config.enable {
                let sender = sender.clone();
                let config = config.clone();
                let cancel_token = self.cancellation_token.child_token();

                let handle = tokio::spawn(async move {
                    Self::window_task(sender, config, cancel_token).await;
                });

                handles.push(handle);
            }
        }

        let count = handles.len();
        self.task_handles = Some(handles);
        info!("Started {} capture tasks", count);
        count
    }

    /// 单个监视器的截图任务
    async fn monitor_task(
        mut monitor: SafeMonitor,
        monitor_id: String,
        sender: Sender<CaptureResult>,
        config: MonitorConfig,
        cancel_token: CancellationToken,
    ) {
        let mut consecutive_errors = 0;
        const MAX_CONSECUTIVE_ERRORS: u32 = 10;

        info!("Monitor {} capture task started", monitor_id);

        loop {
            if cancel_token.is_cancelled() {
                info!("Monitor {} received cancellation signal", monitor_id);
                break;
            }

            match Self::monitor_capture_once(&mut monitor, &sender, &config).await {
                Ok(()) => {
                    consecutive_errors = 0;
                }
                Err(e) => {
                    error!("Capture error for monitor {}: {}", monitor_id, e);
                    consecutive_errors += 1;

                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS {
                        error!(
                            "Monitor {} exceeded max consecutive errors ({}), terminating task",
                            monitor_id, MAX_CONSECUTIVE_ERRORS
                        );
                        break;
                    }

                    tokio::select! {
                        _ = sleep(Duration::from_millis(config.interval * 3)) => {}
                        _ = cancel_token.cancelled() => {
                            info!("Monitor {} cancelled during error backoff", monitor_id);
                            break;
                        }
                    }
                    continue;
                }
            }

            tokio::select! {
                _ = sleep(Duration::from_millis(config.interval)) => {}
                _ = cancel_token.cancelled() => {
                    info!("Monitor {} cancelled during interval", monitor_id);
                    break;
                }
            }
        }

        info!("Monitor {} capture task terminated", monitor_id);
    }

    /// 窗口截图任务
    async fn window_task(
        sender: Sender<CaptureResult>,
        config: WindowConfig,
        cancel_token: CancellationToken,
    ) {
        let mut window = SafeWindow::new();
        let mut consecutive_errors = 0;
        const MAX_CONSECUTIVE_ERRORS: u32 = 10;

        info!("Window capture task started");

        loop {
            if cancel_token.is_cancelled() {
                info!("Window capture received cancellation signal");
                break;
            }

            match Self::window_capture_once(&mut window, &sender, &config).await {
                Ok(captured) => {
                    consecutive_errors = 0;
                    if captured {
                        if let Some((app, title)) = window.last_window_info() {
                            debug!("Captured window: {} - {}", app, title);
                        }
                    }
                }
                Err(e) => {
                    let error_msg = e.to_string();

                    // 没有焦点窗口是正常情况,不计入连续错误
                    if error_msg.contains("No focused window found") {
                        debug!("No focused window, skipping capture");
                    } else {
                        error!("Window capture error: {}", e);
                        consecutive_errors += 1;

                        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS {
                            error!(
                                "Window capture exceeded max consecutive errors ({}), terminating task",
                                MAX_CONSECUTIVE_ERRORS
                            );
                            break;
                        }

                        tokio::select! {
                            _ = sleep(Duration::from_millis(config.interval * 3)) => {}
                            _ = cancel_token.cancelled() => {
                                info!("Window capture cancelled during error backoff");
                                break;
                            }
                        }
                        continue;
                    }
                }
            }

            tokio::select! {
                _ = sleep(Duration::from_millis(config.interval)) => {}
                _ = cancel_token.cancelled() => {
                    info!("Window capture cancelled during interval");
                    break;
                }
            }
        }

        info!("Window capture task terminated");
    }

    /// 执行一次监视器截图
    async fn monitor_capture_once(
        monitor: &mut SafeMonitor,
        sender: &Sender<CaptureResult>,
        config: &MonitorConfig,
    ) -> Result<()> {
        let result = monitor.capture_once(
            config.enforce_interval,
            config.dhash_threshold,
            config.dhash_resolution,
        )?;

        if let Some(capture_result) = result {
            sender
                .send(capture_result)
                .await
                .map_err(|e| anyhow::anyhow!("Failed to send capture result: {}", e))?;
        }

        Ok(())
    }

    /// 执行一次窗口截图
    async fn window_capture_once(
        window: &mut SafeWindow,
        sender: &Sender<CaptureResult>,
        config: &WindowConfig,
    ) -> Result<bool> {
        let result = window.capture_once(
            config.enforce_interval,
            config.dhash_threshold,
            config.dhash_resolution,
            config.enable_ocr,
        )?;

        if let Some(capture_result) = result {
            sender
                .send(capture_result)
                .await
                .map_err(|e| anyhow::anyhow!("Failed to send capture result: {}", e))?;
            Ok(true)
        } else {
            Ok(false)
        }
    }

    /// 优雅关闭所有截图任务
    ///
    /// 返回成功关闭的任务数量
    pub async fn shutdown(&mut self) -> usize {
        info!("Shutting down Capture...");

        self.cancellation_token.cancel();

        if let Some(handles) = self.task_handles.take() {
            let total = handles.len();
            let mut completed = 0;

            for handle in handles {
                if handle.await.is_ok() {
                    completed += 1;
                }
            }

            info!(
                "Capture shutdown complete: {}/{} tasks finished",
                completed, total
            );
            completed
        } else {
            warn!("No tasks to shutdown");
            0
        }
    }

    /// 检查是否有任务正在运行
    pub fn is_running(&self) -> bool {
        self.task_handles
            .as_ref()
            .map(|handles| !handles.is_empty())
            .unwrap_or(false)
    }

    /// 获取已启动的任务数量
    pub fn task_count(&self) -> usize {
        self.task_handles
            .as_ref()
            .map(|handles| handles.len())
            .unwrap_or(0)
    }

    /// 获取目前连接的显示器ID
    pub fn get_all_monitors_id() -> Result<Vec<String>> {
        let monitors = Monitor::all()?;
        let monitor_ids: Vec<String> = monitors
            .iter()
            .map(|m| {
                format!(
                    "{}_{}_{}_{}_{}",
                    m.name().unwrap_or_else(|_| "Unknown".to_string()),
                    m.width().unwrap_or(0),
                    m.height().unwrap_or(0),
                    m.x().unwrap_or(0),
                    m.y().unwrap_or(0)
                )
            })
            .collect();
        Ok(monitor_ids)
    }
}

impl Drop for Capture {
    fn drop(&mut self) {
        if self.is_running() {
            warn!("Capture dropped while tasks are still running, sending cancellation signal");
            self.cancellation_token.cancel();
        }
    }
}
