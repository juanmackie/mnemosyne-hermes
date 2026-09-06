//! HTTP API server with SSE support

use super::{
    events::{Event, EventBroadcaster},
    state::{AgentInfo, ContextFile, StateManager},
};
use axum::{
    extract::State,
    http::StatusCode,
    response::{
        sse::{Event as SseEvent, KeepAlive},
        IntoResponse, Sse,
    },
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use std::{convert::Infallible, net::SocketAddr, sync::Arc};
use tokio_stream::{wrappers::BroadcastStream, StreamExt as _};
use tower_http::{cors::CorsLayer, trace::TraceLayer};
use tracing::{debug, info};

/// API server configuration
#[derive(Debug, Clone)]
pub struct ApiServerConfig {
    /// Server address
    pub addr: SocketAddr,
    /// Event channel capacity
    pub event_capacity: usize,
    /// Whether the dashboard HTTP API is enabled. Defaults to off; must be
    /// explicitly opted in per memory profile. When off, `serve` refuses to
    /// bind (the HTTP surface is experimental and unauthenticated by default).
    pub start_dashboard: bool,
    /// Optional bearer token required on state/events/mutating endpoints.
    /// When `None`, the HTTP surface is not exposed (treated as disabled).
    pub auth_token: Option<String>,
    /// Allowed CORS origins for browser clients. Empty means no browser
    /// cross-origin access (non-browser clients are unaffected).
    pub allowed_origins: Vec<String>,
}

impl Default for ApiServerConfig {
    fn default() -> Self {
        Self {
            addr: ([127, 0, 0, 1], 3000).into(),
            event_capacity: 1000,
            start_dashboard: false,
            auth_token: None,
            allowed_origins: Vec::new(),
        }
    }
}

/// API server state
#[derive(Clone)]
struct AppState {
    /// Event broadcaster
    events: EventBroadcaster,
    /// State manager
    state: Arc<StateManager>,
    /// Instance ID
    instance_id: String,
    /// Optional bearer token for state/events endpoints
    auth_token: Option<String>,
    /// Allowed CORS origins (empty => no browser cross-origin access)
    allowed_origins: Vec<String>,
}

/// API server
pub struct ApiServer {
    config: ApiServerConfig,
    events: EventBroadcaster,
    state: Arc<StateManager>,
    instance_id: String,
    /// Shutdown signal for background tasks
    shutdown_tx: tokio::sync::broadcast::Sender<()>,
    /// Heartbeat task handle for cleanup
    heartbeat_handle: Option<tokio::task::JoinHandle<()>>,
}

impl ApiServer {
    /// Create new API server
    pub fn new(config: ApiServerConfig) -> Self {
        let events = EventBroadcaster::new(config.event_capacity);
        let state = Arc::new(StateManager::new());
        let instance_id = uuid::Uuid::new_v4().to_string()[..8].to_string();

        // Subscribe StateManager to event stream for automatic state updates
        // This creates a single source of truth: events drive state
        let event_rx = events.subscribe();
        state.subscribe_to_events(event_rx);
        info!("StateManager subscribed to event stream - state will update automatically");

        // Create shutdown channel for graceful task termination
        let (shutdown_tx, _) = tokio::sync::broadcast::channel(1);

        Self {
            config,
            events,
            state,
            instance_id,
            shutdown_tx,
            heartbeat_handle: None,
        }
    }

    /// Get event broadcaster
    pub fn broadcaster(&self) -> &EventBroadcaster {
        &self.events
    }

    /// Get state manager
    pub fn state_manager(&self) -> &Arc<StateManager> {
        &self.state
    }

    /// Get instance ID
    pub fn instance_id(&self) -> &str {
        &self.instance_id
    }

    /// Build router
    fn build_router(state: AppState) -> Router {
        // Constrain CORS to explicitly allowed origins only. Empty list => no
        // browser cross-origin access (the default). Never permissive.
        let cors = {
            let builder = CorsLayer::new()
                .allow_methods([axum::http::Method::GET, axum::http::Method::POST])
                .allow_headers([
                    axum::http::header::CONTENT_TYPE,
                    axum::http::header::AUTHORIZATION,
                ]);
            if state.allowed_origins.is_empty() {
                builder.allow_origin(false)
            } else {
                builder.allow_origin(
                    state
                        .allowed_origins
                        .iter()
                        .filter_map(|o| o.parse::<axum::http::HeaderValue>().ok())
                        .collect::<Vec<_>>(),
                )
            }
        };

        // Token-check middleware applied to state/events/emit (not /health).
        let auth = axum::middleware::from_fn_with_state(state.clone(), auth_middleware);

        Router::new()
            // Event streaming
            .route("/events", get(events_handler))
            .route("/events/emit", post(emit_event_handler))
            // State endpoints
            .route("/state/agents", get(list_agents_handler))
            .route("/state/agents", post(update_agent_handler))
            .route("/state/context-files", get(list_context_files_handler))
            .route("/state/context-files", post(update_context_file_handler))
            .route("/state/stats", get(stats_handler))
            .route("/state/metrics", get(metrics_handler))
            // Time-series metrics endpoints
            .route("/metrics/agent-states", get(agent_states_series_handler))
            .route("/metrics/memory-ops", get(memory_ops_series_handler))
            .route("/metrics/skills", get(skills_series_handler))
            .route("/metrics/work", get(work_series_handler))
            // State
            .with_state(state.clone())
            // Token check on the protected routes registered so far.
            // /health is registered after this layer and stays open (liveness).
            .route_layer(auth)
            // Health check (liveness only, no auth)
            .route("/health", get(health_handler))
            .with_state(state)
            .layer(cors)
            .layer(TraceLayer::new_for_http())
    }

    /// Bearer-token gate for the experimental HTTP state/events surface.
    /// No token configured => requests pass through (surface not bound anyway
    /// unless explicitly opted in).
    async fn auth_middleware(
        State(state): State<AppState>,
        req: axum::extract::Request,
        next: axum::middleware::Next,
    ) -> impl IntoResponse {
        if let Some(expected) = &state.auth_token {
            let supplied = req
                .headers()
                .get(axum::http::header::AUTHORIZATION)
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.strip_prefix("Bearer "))
                .map(str::to_string)
                .unwrap_or_default();
            if supplied != *expected {
                return StatusCode::UNAUTHORIZED.into_response();
            }
        }
        next.run(req).await
    }

    /// Start serving with dynamic port allocation
    ///
    /// Tries the configured address first, then attempts alternative ports
    /// if the primary port is unavailable (e.g., when multiple instances are running).
    pub async fn serve(mut self) -> anyhow::Result<()> {
        // HTTP surface is experimental and off by default. Refuse to bind unless
        // explicitly opted in via the config (per memory profile).
        if !self.config.start_dashboard {
            info!("Dashboard HTTP API disabled (opt-in); not starting on {}", self.config.addr);
            return Ok(());
        }

        let state = AppState {
            events: self.events.clone(),
            state: self.state.clone(),
            instance_id: self.instance_id.clone(),
            auth_token: self.config.auth_token.clone(),
            allowed_origins: self.config.allowed_origins.clone(),
        };

        let router = Self::build_router(state.clone());

        // Publish session started event
        let _ = self
            .events
            .broadcast(Event::session_started(self.instance_id.clone()));

        // Spawn heartbeat task (every 10 seconds) with shutdown support
        let events_clone = self.events.clone();
        let instance_id_clone = self.instance_id.clone();
        let mut shutdown_rx = self.shutdown_tx.subscribe();

        let heartbeat_handle = tokio::spawn(async move {
            let mut interval = tokio::time::interval(tokio::time::Duration::from_secs(10));
            loop {
                tokio::select! {
                    _ = interval.tick() => {
                        let _ = events_clone.broadcast(Event::heartbeat(instance_id_clone.clone()));
                    }
                    _ = shutdown_rx.recv() => {
                        debug!("Heartbeat task received shutdown signal");
                        break;
                    }
                }
            }
        });

        // Store heartbeat handle for cleanup
        self.heartbeat_handle = Some(heartbeat_handle);

        // Try configured address first
        match tokio::net::TcpListener::bind(self.config.addr).await {
            Ok(listener) => {
                info!(
                    "API server [{}] listening on http://{}",
                    self.instance_id, self.config.addr
                );
                info!(
                    "Dashboard: mnemosyne-dash --api http://{}",
                    self.config.addr
                );
                axum::serve(listener, router).await?;
                return Ok(());
            }
            Err(e) if e.kind() == std::io::ErrorKind::AddrInUse => {
                debug!(
                    "Port {} in use, trying alternative ports...",
                    self.config.addr.port()
                );
            }
            Err(e) => return Err(e.into()),
        }

        // Try alternative ports (3001-3010)
        let base_port = self.config.addr.port();
        for offset in 1..=10 {
            let alt_port = base_port + offset;
            let alt_addr = SocketAddr::new(self.config.addr.ip(), alt_port);

            match tokio::net::TcpListener::bind(alt_addr).await {
                Ok(listener) => {
                    info!(
                        "API server [{}] listening on http://{}",
                        self.instance_id, alt_addr
                    );
                    info!("Dashboard: mnemosyne-dash --api http://{}", alt_addr);
                    axum::serve(listener, router).await?;
                    return Ok(());
                }
                Err(e) if e.kind() == std::io::ErrorKind::AddrInUse => {
                    continue;
                }
                Err(e) => return Err(e.into()),
            }
        }

        Err(anyhow::anyhow!(
            "All ports ({}–{}) are in use. API server unavailable for instance {}. \
             Core functionality not affected.",
            base_port,
            base_port + 10,
            self.instance_id
        ))
    }
}

/// SSE events handler
///
/// Provides SSE snapshot for late-connecting clients:
/// 1. Immediately sends current state as synthetic events (agents, context files)
/// 2. Then streams real-time events
///
/// This ensures dashboard sees agents immediately even if it connects after startup.
async fn events_handler(
    State(state): State<AppState>,
) -> Sse<impl tokio_stream::Stream<Item = Result<SseEvent, Infallible>>> {
    debug!("New SSE client connected, sending state snapshot");

    // Subscribe to the live event stream FIRST, before capturing the state
    // snapshot. This prevents missing intervening events that arrive between
    // snapshot read and stream start (previous code lost them).
    let rx = state.events.subscribe();
    let live_stream = BroadcastStream::new(rx);

    // Get current state snapshot (reconciles against the live subscription's
    // observation point; StateManager projects events into this state).
    let agents = state.state.list_agents().await;
    let context_files = state.state.list_context_files().await;

    debug!(
        "Snapshot: {} agents, {} context files",
        agents.len(),
        context_files.len()
    );

    // Synthetic snapshot events for current state (replay for late joiners).
    let mut snapshot_events = Vec::new();

    // Agent heartbeat events (so StateManager sees them as alive)
    for agent in agents {
        let event = Event::heartbeat(agent.id.clone());
        if let Ok(data) = serde_json::to_string(&event) {
            snapshot_events.push(Ok(SseEvent::default().data(data).id(event.id)));
        }
    }

    // Context file events
    for file in context_files {
        let event = Event::context_modified(file.path.clone());
        if let Ok(data) = serde_json::to_string(&event) {
            snapshot_events.push(Ok(SseEvent::default().data(data).id(event.id)));
        }
    }

    debug!(
        "Sending {} snapshot events to new client",
        snapshot_events.len()
    );

    let live_event_stream = live_stream.filter_map(|result| match result {
        Ok(event) => {
            // Convert Event to SSE Event
            let data = serde_json::to_string(&event).ok()?;
            Some(Ok(SseEvent::default().data(data).id(event.id)))
        }
        // Lagged: skip the missed window; ids are UUIDs so order is preserved.
        Err(_) => None,
    });

    // Combine snapshot + live events
    let snapshot_stream = tokio_stream::iter(snapshot_events);
    let combined_stream = snapshot_stream.chain(live_event_stream);

    Sse::new(combined_stream).keep_alive(KeepAlive::default())
}

/// Emit event handler (for remote MCP servers to forward events)
async fn emit_event_handler(State(state): State<AppState>, Json(event): Json<Event>) -> StatusCode {
    debug!(
        "Received event from remote MCP server: {:?}",
        event.event_type
    );

    match state.events.broadcast(event) {
        Ok(_) => {
            debug!("Event broadcast successful");
            StatusCode::ACCEPTED
        }
        Err(_) => {
            // No subscribers - expected when dashboard not connected
            debug!("No subscribers for forwarded event (dashboard not connected)");
            StatusCode::ACCEPTED
        }
    }
}

/// List agents handler
async fn list_agents_handler(State(state): State<AppState>) -> Json<Vec<AgentInfo>> {
    let agents = state.state.list_agents().await;
    Json(agents)
}

/// Update agent handler
#[derive(Debug, Deserialize)]
struct UpdateAgentRequest {
    agent: AgentInfo,
}

async fn update_agent_handler(
    State(state): State<AppState>,
    Json(req): Json<UpdateAgentRequest>,
) -> StatusCode {
    let agent_id = req.agent.id.clone();
    state.state.update_agent(req.agent).await;

    // Broadcast agent updated event
    let event = Event::agent_started(agent_id);
    let _ = state.events.broadcast(event);

    StatusCode::OK
}

/// List context files handler
async fn list_context_files_handler(State(state): State<AppState>) -> Json<Vec<ContextFile>> {
    let files = state.state.list_context_files().await;
    Json(files)
}

/// Update context file handler
#[derive(Debug, Deserialize)]
struct UpdateContextFileRequest {
    file: ContextFile,
}

async fn update_context_file_handler(
    State(state): State<AppState>,
    Json(req): Json<UpdateContextFileRequest>,
) -> StatusCode {
    state.state.update_context_file(req.file.clone()).await;

    // Broadcast context modified event
    let event = Event::context_modified(req.file.path);
    let _ = state.events.broadcast(event);

    StatusCode::OK
}

/// Stats handler
async fn stats_handler(State(state): State<AppState>) -> impl IntoResponse {
    let stats = state.state.stats().await;
    Json(stats)
}

/// Metrics handler (returns time-series metrics snapshot)
async fn metrics_handler(State(state): State<AppState>) -> impl IntoResponse {
    let metrics = state.state.metrics_snapshot().await;
    Json(metrics)
}

/// Agent states time-series handler
async fn agent_states_series_handler(State(state): State<AppState>) -> impl IntoResponse {
    let metrics = state.state.metrics().await;
    let series = metrics.agent_states_series().clone();
    Json(series)
}

/// Memory operations time-series handler
async fn memory_ops_series_handler(State(state): State<AppState>) -> impl IntoResponse {
    let metrics = state.state.metrics().await;
    let series = metrics.memory_ops_series().clone();
    Json(series)
}

/// Skills time-series handler
async fn skills_series_handler(State(state): State<AppState>) -> impl IntoResponse {
    let metrics = state.state.metrics().await;
    let series = metrics.skills_series().clone();
    Json(series)
}

/// Work progress time-series handler
async fn work_series_handler(State(state): State<AppState>) -> impl IntoResponse {
    let metrics = state.state.metrics().await;
    let series = metrics.work_series().clone();
    Json(series)
}

/// Health check handler
#[derive(Debug, Serialize)]
struct HealthResponse {
    status: String,
    version: String,
    instance_id: String,
    subscribers: usize,
}

async fn health_handler(State(state): State<AppState>) -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "ok".to_string(),
        version: env!("CARGO_PKG_VERSION").to_string(),
        instance_id: state.instance_id.clone(),
        subscribers: state.events.subscriber_count(),
    })
}

impl Drop for ApiServer {
    fn drop(&mut self) {
        // Send shutdown signal to background tasks
        let _ = self.shutdown_tx.send(());

        // Abort heartbeat task if it's still running
        if let Some(handle) = self.heartbeat_handle.take() {
            handle.abort();
            debug!("ApiServer dropped - heartbeat task aborted");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_server_creation() {
        let config = ApiServerConfig::default();
        let server = ApiServer::new(config);
        // StateManager subscribes to event stream on creation
        assert_eq!(server.broadcaster().subscriber_count(), 1);
    }

    #[tokio::test]
    async fn test_health_endpoint() {
        let state = AppState {
            events: EventBroadcaster::default(),
            state: Arc::new(StateManager::new()),
            instance_id: "test-instance".to_string(),
            auth_token: None,
            allowed_origins: Vec::new(),
        };

        let response = health_handler(State(state)).await;
        assert_eq!(response.0.status, "ok");
        assert_eq!(response.0.instance_id, "test-instance");
    }

    #[tokio::test]
    async fn test_dashboard_opt_in_disabled_by_default() {
        // Default config must NOT start the HTTP surface.
        let config = ApiServerConfig::default();
        assert!(!config.start_dashboard);
        assert!(config.auth_token.is_none());
        assert!(config.allowed_origins.is_empty());
        let server = ApiServer::new(config);
        // serve() returns early without binding when disabled
        let _ = server.serve().await.unwrap();
    }
}
