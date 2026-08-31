// Evolution Module - Background memory optimization jobs
//
// This module implements autonomous background jobs that continuously optimize
// the memory system without user intervention.
//
// Components:
// - scheduler: Job scheduling with idle detection
// - importance: Importance recalibration based on usage
// - links: Link strength decay for untraversed connections
// - archival: Automatic archival of unused memories
// - consolidation: Duplicate detection and merging (requires vector search - Stream 1)

pub mod archival;
pub mod config;
pub mod consolidation;
pub mod importance;
pub mod links;
pub mod maintenance;
#[cfg(feature = "python")]
pub mod memory_evolution_dspy_adapter;
pub mod scheduler;

pub use archival::ArchivalJob;
pub use config::{ConfigError, ConsolidationConfig, DecisionMode, EvolutionConfig, JobConfig};
pub use consolidation::ConsolidationJob;
pub use importance::ImportanceRecalibrator;
pub use links::LinkDecayJob;
pub use maintenance::{
    MaintenanceConfig, MaintenanceError, MaintenanceFinding, MaintenanceKind, MaintenanceReport,
    MaintenanceRunner, MaintenanceStatus, NightlyOrphanRepairJob, MAX_ITEMS_PER_RUN,
    MAX_RETRIES_PER_RUN,
};
#[cfg(feature = "python")]
pub use memory_evolution_dspy_adapter::MemoryEvolutionDSpyAdapter;
pub use scheduler::{
    BackgroundScheduler, EvolutionJob, JobError, JobReport, JobRun, JobStatus, SchedulerError,
};
