use crate::config::LocalConfig;
use crate::event::CaptureResult;
use crate::storage::Storage;
use anyhow::{Context, Result};
use async_trait::async_trait;
use std::path::PathBuf;
use tokio::fs;

pub struct LocalStorage {
    base_path: PathBuf,
}

impl LocalStorage {
    pub fn new(config: &LocalConfig) -> Result<Self> {
        let base_path = PathBuf::from(&config.path);
        std::fs::create_dir_all(&base_path).context("Failed to create local storage directory")?;
        Ok(Self { base_path })
    }
}

#[async_trait]
impl Storage for LocalStorage {
    async fn save(&self, capture: &CaptureResult) -> Result<String> {
        let filename = format!(
            "{}_{}.png",
            capture.monitor_id,
            capture.timestamp.format("%Y%m%d_%H%M%S_%3f")
        );
        let file_path = self.base_path.join(&filename);

        let buffer = {
            let mut buf = Vec::new();
            capture
                .image
                .write_to(&mut std::io::Cursor::new(&mut buf), image::ImageFormat::Png)?;
            buf
        };

        fs::write(&file_path, buffer).await?;
        Ok(format!("file://{}", file_path.display()))
    }
}
