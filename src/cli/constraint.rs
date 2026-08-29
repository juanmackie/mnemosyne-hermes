//! Owner-reviewed project constraint lifecycle commands.

use clap::Subcommand;
use mnemosyne_core::{
    ConnectionMode, ConstraintProposalService, ConstraintStatus, LibsqlStorage, MemoryId,
};
use std::path::PathBuf;
use std::sync::Arc;

use super::helpers::{get_db_path, parse_namespace};

#[derive(Subcommand)]
pub enum ConstraintCommand {
    /// Create a pending constraint proposal from evidence-backed sources.
    Propose {
        /// Constraint text.
        #[arg(long)]
        text: String,
        /// Namespace the constraint applies to.
        #[arg(short, long, default_value = "global")]
        namespace: String,
        /// Human-readable scope within the namespace.
        #[arg(long, default_value = "project")]
        scope: String,
        /// Priority from 1 (low) to 10 (critical).
        #[arg(long, default_value_t = 8)]
        priority: u8,
        /// Optional RFC3339 expiry timestamp.
        #[arg(long)]
        valid_until: Option<String>,
        /// Source memory ID; repeat for multiple sources.
        #[arg(long = "source-memory", required = true)]
        source_memory: Vec<String>,
        /// Verbatim evidence quote; repeat for multiple quotes.
        #[arg(long = "evidence", required = true)]
        evidence: Vec<String>,
        /// Identity that created this proposal.
        #[arg(long)]
        proposer: String,
        /// Explicit reviewer identity allowed to decide it.
        #[arg(long)]
        owner: String,
    },
    /// List constraint proposals.
    List {
        #[arg(short, long)]
        namespace: Option<String>,
        #[arg(long)]
        status: Option<String>,
        #[arg(short, long, default_value_t = 100)]
        limit: usize,
        #[arg(short, long, default_value = "json")]
        format: String,
    },
    /// Show one constraint proposal.
    Show { id: String },
    /// Approve a pending constraint proposal.
    Approve {
        id: String,
        #[arg(long)]
        reviewer: String,
        #[arg(long)]
        note: Option<String>,
    },
    /// Reject a pending constraint proposal.
    Reject {
        id: String,
        #[arg(long)]
        reviewer: String,
        #[arg(long)]
        note: Option<String>,
    },
    /// Retire an approved constraint while preserving its audit history.
    Supersede {
        id: String,
        #[arg(long)]
        reviewer: String,
        #[arg(long)]
        note: Option<String>,
    },
    /// Export approved constraints as a human-readable Markdown projection.
    Export {
        #[arg(short, long)]
        namespace: Option<String>,
        #[arg(short, long, default_value = ".mnemosyne/CONSTRAINTS.md")]
        output: PathBuf,
    },
}

pub async fn handle(
    command: ConstraintCommand,
    global_db_path: Option<String>,
) -> mnemosyne_core::error::Result<()> {
    let db_path = get_db_path(global_db_path);
    let storage =
        Arc::new(LibsqlStorage::new_with_validation(ConnectionMode::Local(db_path), true).await?);
    let service = ConstraintProposalService::new(storage);

    match command {
        ConstraintCommand::Propose {
            text,
            namespace,
            scope,
            priority,
            valid_until,
            source_memory,
            evidence,
            proposer,
            owner,
        } => {
            let namespace = parse_namespace(&namespace)?;
            let source_memory_ids = source_memory
                .iter()
                .map(|id| {
                    MemoryId::from_string(id).map_err(|error| {
                        mnemosyne_core::error::MnemosyneError::InvalidId(error.to_string())
                    })
                })
                .collect::<mnemosyne_core::error::Result<Vec<_>>>()?;
            let proposal = service
                .propose(
                    &namespace,
                    &text,
                    &scope,
                    priority,
                    valid_until.as_deref(),
                    source_memory_ids,
                    evidence,
                    &proposer,
                    &owner,
                )
                .await
                .map_err(|error| mnemosyne_core::error::MnemosyneError::Other(error.to_string()))?;
            println!("{}", serde_json::to_string_pretty(&proposal)?);
        }
        ConstraintCommand::List {
            namespace,
            status,
            limit,
            format,
        } => {
            if format != "json" {
                return Err(mnemosyne_core::error::MnemosyneError::ValidationError(
                    "constraint list currently supports only --format json".into(),
                ));
            }
            let namespace = namespace.as_deref().map(parse_namespace).transpose()?;
            let status = status
                .as_deref()
                .map(ConstraintStatus::parse)
                .transpose()
                .map_err(|error| {
                    mnemosyne_core::error::MnemosyneError::ValidationError(error.to_string())
                })?;
            let proposals = service
                .list(namespace.as_ref(), status, limit)
                .await
                .map_err(|error| mnemosyne_core::error::MnemosyneError::Other(error.to_string()))?;
            println!("{}", serde_json::to_string_pretty(&proposals)?);
        }
        ConstraintCommand::Show { id } => {
            let proposal = service
                .get(&id)
                .await
                .map_err(|error| mnemosyne_core::error::MnemosyneError::Other(error.to_string()))?;
            match proposal {
                Some(proposal) => println!("{}", serde_json::to_string_pretty(&proposal)?),
                None => {
                    return Err(mnemosyne_core::error::MnemosyneError::NotFound(format!(
                        "constraint proposal {}",
                        id
                    )))
                }
            }
        }
        ConstraintCommand::Approve { id, reviewer, note } => {
            let proposal = service
                .approve(&id, &reviewer, note.as_deref())
                .await
                .map_err(|error| mnemosyne_core::error::MnemosyneError::Other(error.to_string()))?;
            println!("{}", serde_json::to_string_pretty(&proposal)?);
        }
        ConstraintCommand::Reject { id, reviewer, note } => {
            let proposal = service
                .reject(&id, &reviewer, note.as_deref())
                .await
                .map_err(|error| mnemosyne_core::error::MnemosyneError::Other(error.to_string()))?;
            println!("{}", serde_json::to_string_pretty(&proposal)?);
        }
        ConstraintCommand::Supersede { id, reviewer, note } => {
            let proposal = service
                .supersede(&id, &reviewer, note.as_deref())
                .await
                .map_err(|error| mnemosyne_core::error::MnemosyneError::Other(error.to_string()))?;
            println!("{}", serde_json::to_string_pretty(&proposal)?);
        }
        ConstraintCommand::Export { namespace, output } => {
            let namespace = namespace.as_deref().map(parse_namespace).transpose()?;
            let proposals = service
                .list(namespace.as_ref(), Some(ConstraintStatus::Approved), 1_000)
                .await
                .map_err(|error| mnemosyne_core::error::MnemosyneError::Other(error.to_string()))?;
            write_markdown_projection(&output, &proposals)?;
            eprintln!(
                "Exported {} approved constraints to {}",
                proposals.len(),
                output.display()
            );
        }
    }
    Ok(())
}

fn write_markdown_projection(
    output: &PathBuf,
    proposals: &[mnemosyne_core::ConstraintProposal],
) -> std::io::Result<()> {
    if let Some(parent) = output.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut markdown = String::from(
        "# Approved Mnemosyne Constraints\n\n<!-- Generated projection; edit through `mnemosyne constraint` so the DB remains canonical. -->\n\n",
    );
    for proposal in proposals {
        markdown.push_str(&format!(
            "- **P{}** {}\n  - ID: `{}`\n  - Scope: `{}`\n  - Namespace: `{}`\n  - Approved by: `{}`\n",
            proposal.priority,
            proposal.text.replace('\n', " "),
            proposal.id,
            proposal.scope.replace('\n', " "),
            proposal.namespace,
            proposal.approved_by.as_deref().unwrap_or("unknown")
        ));
        if !proposal.source_memory_ids.is_empty() {
            markdown.push_str(&format!(
                "  - Evidence sources: {}\n",
                proposal
                    .source_memory_ids
                    .iter()
                    .map(ToString::to_string)
                    .collect::<Vec<_>>()
                    .join(", ")
            ));
        }
        markdown.push('\n');
    }
    let temporary = output.with_extension(format!("tmp-{}", uuid::Uuid::new_v4()));
    std::fs::write(&temporary, markdown)?;
    std::fs::rename(temporary, output)
}
