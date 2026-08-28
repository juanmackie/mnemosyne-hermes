//! Network layer for optional distributed agent communication.

pub mod router;

#[cfg(feature = "distributed")]
pub mod endpoint;
#[cfg(feature = "distributed")]
pub mod protocol;
#[cfg(feature = "distributed")]
pub mod transport;

#[cfg(feature = "distributed")]
pub use endpoint::{AgentEndpoint, AgentKeypair};
#[cfg(feature = "distributed")]
pub use protocol::AgentProtocol;
pub use router::MessageRouter;

#[cfg(feature = "distributed")]
mod distributed_layer;
#[cfg(feature = "distributed")]
pub use distributed_layer::NetworkLayer;

#[cfg(not(feature = "distributed"))]
/// Local-only network facade. Distributed Iroh transport is opt-in.
pub struct NetworkLayer {
    router: std::sync::Arc<MessageRouter>,
}

#[cfg(not(feature = "distributed"))]
impl NetworkLayer {
    pub async fn new() -> crate::error::Result<Self> {
        Ok(Self {
            router: std::sync::Arc::new(MessageRouter::new()),
        })
    }

    pub fn router(&self) -> std::sync::Arc<MessageRouter> {
        self.router.clone()
    }

    pub async fn start(&self) -> crate::error::Result<()> {
        tracing::debug!("Distributed transport disabled; using local-only network facade");
        Ok(())
    }

    pub async fn stop(&self) -> crate::error::Result<()> {
        Ok(())
    }

    pub async fn node_id(&self) -> Option<String> {
        None
    }

    pub async fn create_invite(&self) -> crate::error::Result<String> {
        Err(crate::error::MnemosyneError::Other(
            "Distributed networking is disabled; rebuild with --features distributed".into(),
        ))
    }

    pub async fn join_peer(&self, _ticket: &str) -> crate::error::Result<String> {
        Err(crate::error::MnemosyneError::Other(
            "Distributed networking is disabled; rebuild with --features distributed".into(),
        ))
    }
}

#[cfg(not(feature = "distributed"))]
pub struct AgentEndpoint;
#[cfg(not(feature = "distributed"))]
pub struct AgentKeypair;
#[cfg(not(feature = "distributed"))]
pub struct AgentProtocol;
