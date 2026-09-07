//! gRPC server setup

use crate::rpc::generated::health_service_server::HealthServiceServer;
use crate::rpc::generated::memory_service_server::MemoryServiceServer;
use crate::rpc::services::{HealthServiceImpl, MemoryServiceImpl};
use crate::services::LlmService;
use crate::storage::StorageBackend;
use anyhow::Result;
use std::sync::Arc;
use tonic::transport::Server;
use tracing::info;

pub struct RpcServer {
    health_service: HealthServiceImpl,
    memory_service: MemoryServiceImpl,
}

impl RpcServer {
    pub fn new(storage: Arc<dyn StorageBackend>, llm: Option<Arc<LlmService>>) -> Self {
        Self {
            health_service: HealthServiceImpl::new().with_storage(Arc::clone(&storage)),
            memory_service: MemoryServiceImpl::new(storage, llm),
        }
    }

    pub async fn serve(self, addr: impl Into<String>) -> Result<()> {
        let addr_str = addr.into();
        let addr: std::net::SocketAddr = addr_str.parse()?;

        // The RPC surface has no TLS or auth. Refuse non-loopback binds until
        // a protected deployment (reverse proxy w/ TLS+auth) is configured.
        if !addr.ip().is_loopback() {
            anyhow::bail!(
                "Refusing to bind unauthenticated RPC server to non-loopback \
                 address {addr}. Bind to a loopback address or front it with an \
                 authenticated TLS reverse proxy."
            );
        }

        info!("Starting mnemosyne RPC server on {}", addr);

        Server::builder()
            .add_service(HealthServiceServer::new(self.health_service))
            .add_service(MemoryServiceServer::new(self.memory_service))
            .serve(addr)
            .await?;

        Ok(())
    }
}
