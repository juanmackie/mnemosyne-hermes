//! Operator-facing commands for durable memory change proposals.

use clap::{Subcommand, ValueEnum};
use mnemosyne_core::{
    ConnectionMode, LibsqlStorage, MemoryId, MemoryProposalStatus, PolicyProposalService,
    ProposalProvenance, ProposalService,
};
use std::sync::Arc;

use super::helpers::{get_db_path, parse_namespace};

#[derive(Clone, Copy, Debug, ValueEnum)]
pub enum ProposalStatusArg {
    Pending,
    Accepted,
    Dismissed,
    Applied,
    Failed,
}

impl ProposalStatusArg {
    fn status(self) -> MemoryProposalStatus {
        match self {
            Self::Pending => MemoryProposalStatus::Pending,
            Self::Accepted => MemoryProposalStatus::Accepted,
            Self::Dismissed => MemoryProposalStatus::Dismissed,
            Self::Applied => MemoryProposalStatus::Applied,
            Self::Failed => MemoryProposalStatus::Failed,
        }
    }
}

#[derive(Subcommand)]
pub enum ProposalCommand {
    /// Manage owner-reviewed interaction-policy proposals
    Policy {
        #[command(subcommand)]
        command: PolicyProposalCommand,
    },

    /// Create a pending proposal for a factual memory update
    Create {
        /// Target memory UUID
        #[arg(long)]
        target: String,

        /// Proposed replacement content
        #[arg(long)]
        proposed_content: String,

        /// Owner who must review the proposal
        #[arg(long)]
        owner: String,

        /// Actor that proposed the change
        #[arg(long, default_value = "cli")]
        proposer: String,

        /// Source memory UUID; repeat for multiple sources
        #[arg(long = "source-memory", required = true)]
        source_memory_ids: Vec<String>,

        /// Evidence quote; repeat for multiple quotes
        #[arg(long = "evidence", required = true)]
        evidence_quotes: Vec<String>,

        /// Database path
        #[arg(short, long)]
        database: Option<String>,
    },

    /// List durable proposals
    List {
        /// Namespace filter
        #[arg(short, long)]
        namespace: Option<String>,

        /// Status filter
        #[arg(long, value_enum)]
        status: Option<ProposalStatusArg>,

        /// Maximum entries
        #[arg(short, long, default_value = "20")]
        limit: usize,

        /// Database path
        #[arg(short, long)]
        database: Option<String>,
    },

    /// Accept a pending proposal (apply it separately)
    Accept {
        #[arg(long)]
        id: String,
        #[arg(long)]
        reviewer: String,
        #[arg(long)]
        note: Option<String>,
        #[arg(short, long)]
        database: Option<String>,
    },

    /// Dismiss a pending proposal without changing canonical memory
    Dismiss {
        #[arg(long)]
        id: String,
        #[arg(long)]
        reviewer: String,
        #[arg(long)]
        note: Option<String>,
        #[arg(short, long)]
        database: Option<String>,
    },

    /// Apply an accepted proposal after checking its base revision
    Apply {
        #[arg(long)]
        id: String,
        #[arg(long)]
        reviewer: String,
        #[arg(short, long)]
        database: Option<String>,
    },

    /// Show one proposal including its exact diff and evidence
    Show {
        #[arg(long)]
        id: String,
        #[arg(short, long)]
        database: Option<String>,
    },
}

#[derive(Subcommand)]
pub enum PolicyProposalCommand {
    /// List pending or completed interaction-policy proposals
    List {
        #[arg(short, long)]
        namespace: Option<String>,
        #[arg(long, value_enum)]
        status: Option<ProposalStatusArg>,
        #[arg(short, long, default_value = "20")]
        limit: usize,
        #[arg(short, long)]
        database: Option<String>,
    },
    /// Show one interaction-policy proposal
    Show {
        #[arg(long)]
        id: String,
        #[arg(short, long)]
        database: Option<String>,
    },
    /// Accept a pending interaction-policy proposal
    Accept {
        #[arg(long)]
        id: String,
        #[arg(long)]
        reviewer: String,
        #[arg(long)]
        note: Option<String>,
        #[arg(short, long)]
        database: Option<String>,
    },
    /// Dismiss a pending interaction-policy proposal
    Dismiss {
        #[arg(long)]
        id: String,
        #[arg(long)]
        reviewer: String,
        #[arg(long)]
        note: Option<String>,
        #[arg(short, long)]
        database: Option<String>,
    },
    /// Apply an accepted interaction-policy proposal
    Apply {
        #[arg(long)]
        id: String,
        #[arg(long)]
        reviewer: String,
        #[arg(short, long)]
        database: Option<String>,
    },
}

impl PolicyProposalCommand {
    fn database(&self) -> Option<&String> {
        match self {
            Self::List { database, .. }
            | Self::Show { database, .. }
            | Self::Accept { database, .. }
            | Self::Dismiss { database, .. }
            | Self::Apply { database, .. } => database.as_ref(),
        }
    }
}

pub async fn handle(
    command: ProposalCommand,
    global_db_path: Option<String>,
) -> mnemosyne_core::error::Result<()> {
    let database = match &command {
        ProposalCommand::Policy { command } => command
            .database()
            .cloned()
            .or(global_db_path)
            .unwrap_or_else(|| get_db_path(None)),
        ProposalCommand::Create { database, .. }
        | ProposalCommand::List { database, .. }
        | ProposalCommand::Accept { database, .. }
        | ProposalCommand::Dismiss { database, .. }
        | ProposalCommand::Apply { database, .. }
        | ProposalCommand::Show { database, .. } => database
            .clone()
            .or(global_db_path)
            .unwrap_or_else(|| get_db_path(None)),
    };
    let storage = Arc::new(LibsqlStorage::new(ConnectionMode::Local(database)).await?);
    let service = ProposalService::new(storage.clone());
    let policy_service = PolicyProposalService::new(storage);

    match command {
        ProposalCommand::Policy { command } => match command {
            PolicyProposalCommand::List {
                namespace,
                status,
                limit,
                ..
            } => {
                let namespace = namespace.as_deref().map(parse_namespace).transpose()?;
                let proposals = policy_service
                    .list(
                        namespace.as_ref(),
                        status.map(ProposalStatusArg::status),
                        limit,
                    )
                    .await
                    .map_err(|error| mnemosyne_core::MnemosyneError::Other(error.to_string()))?;
                println!("{}", serde_json::to_string_pretty(&proposals)?);
            }
            PolicyProposalCommand::Show { id, .. } => {
                let proposal = policy_service
                    .get(&id)
                    .await
                    .map_err(|error| mnemosyne_core::MnemosyneError::Other(error.to_string()))?;
                println!("{}", serde_json::to_string_pretty(&proposal)?);
            }
            PolicyProposalCommand::Accept {
                id, reviewer, note, ..
            } => {
                let proposal = policy_service
                    .accept(&id, &reviewer, note.as_deref())
                    .await
                    .map_err(|error| mnemosyne_core::MnemosyneError::Other(error.to_string()))?;
                println!("{}", serde_json::to_string_pretty(&proposal)?);
            }
            PolicyProposalCommand::Dismiss {
                id, reviewer, note, ..
            } => {
                let proposal = policy_service
                    .dismiss(&id, &reviewer, note.as_deref())
                    .await
                    .map_err(|error| mnemosyne_core::MnemosyneError::Other(error.to_string()))?;
                println!("{}", serde_json::to_string_pretty(&proposal)?);
            }
            PolicyProposalCommand::Apply { id, reviewer, .. } => {
                let proposal = policy_service
                    .apply(&id, &reviewer)
                    .await
                    .map_err(|error| mnemosyne_core::MnemosyneError::Other(error.to_string()))?;
                println!("{}", serde_json::to_string_pretty(&proposal)?);
            }
        },
        ProposalCommand::Create {
            target,
            proposed_content,
            owner,
            proposer,
            source_memory_ids,
            evidence_quotes,
            ..
        } => {
            let target = MemoryId::from_string(&target)?;
            let source_memory_ids = source_memory_ids
                .iter()
                .map(|id| MemoryId::from_string(id))
                .collect::<std::result::Result<Vec<_>, _>>()?;
            let proposal = service
                .propose_update(
                    target,
                    &proposed_content,
                    &proposer,
                    &owner,
                    ProposalProvenance {
                        source_memory_ids,
                        evidence_quotes,
                    },
                )
                .await
                .map_err(|error| mnemosyne_core::MnemosyneError::Other(error.to_string()))?;
            println!("{}", serde_json::to_string_pretty(&proposal)?);
        }
        ProposalCommand::List {
            namespace,
            status,
            limit,
            ..
        } => {
            let namespace = namespace.as_deref().map(parse_namespace).transpose()?;
            let proposals = service
                .list(
                    namespace.as_ref(),
                    status.map(ProposalStatusArg::status),
                    limit,
                )
                .await
                .map_err(|error| mnemosyne_core::MnemosyneError::Other(error.to_string()))?;
            println!("{}", serde_json::to_string_pretty(&proposals)?);
        }
        ProposalCommand::Accept {
            id, reviewer, note, ..
        } => {
            let proposal = service
                .accept(&id, &reviewer, note.as_deref())
                .await
                .map_err(|error| mnemosyne_core::MnemosyneError::Other(error.to_string()))?;
            println!("{}", serde_json::to_string_pretty(&proposal)?);
        }
        ProposalCommand::Dismiss {
            id, reviewer, note, ..
        } => {
            let proposal = service
                .dismiss(&id, &reviewer, note.as_deref())
                .await
                .map_err(|error| mnemosyne_core::MnemosyneError::Other(error.to_string()))?;
            println!("{}", serde_json::to_string_pretty(&proposal)?);
        }
        ProposalCommand::Apply { id, reviewer, .. } => {
            let proposal = service
                .apply(&id, &reviewer)
                .await
                .map_err(|error| mnemosyne_core::MnemosyneError::Other(error.to_string()))?;
            println!("{}", serde_json::to_string_pretty(&proposal)?);
        }
        ProposalCommand::Show { id, .. } => {
            let proposal = service
                .get(&id)
                .await
                .map_err(|error| mnemosyne_core::MnemosyneError::Other(error.to_string()))?;
            println!("{}", serde_json::to_string_pretty(&proposal)?);
        }
    }
    Ok(())
}

// Namespace parsing is provided by cli::helpers::parse_namespace.
