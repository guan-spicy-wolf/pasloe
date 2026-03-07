use crate::config::S3Config;
use crate::event::CaptureResult;
use crate::storage::Storage;
use crate::pasloe_client::PasloeClient;
use anyhow::Result;
use async_trait::async_trait;

pub struct S3Storage {
    client: PasloeClient,
    prefix: String,
}

impl S3Storage {
    pub fn new(config: &S3Config, client: PasloeClient) -> Result<Self> {
        Ok(Self { 
            client, 
            prefix: config.prefix.clone() 
        })
    }
}

#[async_trait]
impl Storage for S3Storage {
    async fn save(&self, capture: &CaptureResult) -> Result<String> {
        let filename = format!(
            "{}{}_{}.png",
            self.prefix,
            capture.monitor_id,
            capture.timestamp.format("%Y%m%d_%H%M%S_%3f")
        );

        let mut buffer = Vec::new();
        capture.image.write_to(
            &mut std::io::Cursor::new(&mut buffer),
            image::ImageFormat::Png,
        )?;

        let presign = self.client.get_presigned_url(&filename, "image/png").await?;
        self.client.upload_image(&presign.upload_url, buffer, "image/png").await?;
        
        Ok(presign.access_url)
    }
}
