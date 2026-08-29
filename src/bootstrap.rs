//! Bounded project-context bootstrap for agent session startup.
//!
//! Bootstrap is intentionally a read-only assembly layer over Mnemosyne's
//! canonical memory store. It does not create a second database or promote
//! conversational text into policy. The response keeps constraints, facts,
//! reasoning guardrails, and response policies in separate channels and
//! carries source IDs wherever the underlying memory has provenance.

use crate::error::{MnemosyneError, Result};
use crate::orchestration::skills::{SkillMatch, SkillsDiscovery};
use crate::storage::StorageBackend;
use crate::types::{MemoryId, MemoryNote, MemoryType, Namespace, SearchResult};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

/// Version of the structured bootstrap response.
pub const BOOTSTRAP_SCHEMA_VERSION: &str = "bootstrap.v1";
const MAX_TASK_CHARS: usize = 2_000;
const MAX_AGENT_CHARS: usize = 128;
const MAX_CAPABILITY_CHARS: usize = 128;
const MAX_BUDGET_TOKENS: usize = 20_000;
const MAX_MEMORY_CANDIDATES: usize = 256;
const MAX_CONSTRAINTS: usize = 32;
const MAX_FACTS: usize = 64;
const MAX_GUARDRAILS: usize = 16;
const MAX_POLICIES: usize = 16;
const MAX_SKILLS: usize = 16;
const ABSTENTION_THRESHOLD: f32 = 0.30;

/// Inputs shared by CLI, MCP, and launcher integrations.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BootstrapRequest {
    /// Optional repository root. When omitted, the current repository is
    /// detected from the process working directory.
    pub project: Option<PathBuf>,
    /// Explicit namespace. When omitted, the project namespace is detected.
    pub namespace: Option<Namespace>,
    pub task: String,
    pub agent: Option<String>,
    pub capability: Option<String>,
    pub budget_tokens: usize,
    pub min_confidence: f32,
}

impl BootstrapRequest {
    pub fn validate(&self) -> Result<()> {
        validate_text(&self.task, "task", MAX_TASK_CHARS)?;
        if let Some(agent) = &self.agent {
            validate_text(agent, "agent", MAX_AGENT_CHARS)?;
        }
        if let Some(capability) = &self.capability {
            validate_text(capability, "capability", MAX_CAPABILITY_CHARS)?;
        }
        if self.budget_tokens == 0 || self.budget_tokens > MAX_BUDGET_TOKENS {
            return Err(MnemosyneError::ValidationError(format!(
                "budget_tokens must be between 1 and {}",
                MAX_BUDGET_TOKENS
            )));
        }
        if !self.min_confidence.is_finite() || !(0.0..=1.0).contains(&self.min_confidence) {
            return Err(MnemosyneError::ValidationError(
                "min_confidence must be a finite number between 0 and 1".into(),
            ));
        }
        if let Some(project) = &self.project {
            if !project.is_dir() {
                return Err(MnemosyneError::ValidationError(format!(
                    "project is not a directory: {}",
                    project.display()
                )));
            }
        }
        Ok(())
    }
}

/// A compact memory item suitable for a startup package.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BootstrapMemory {
    pub id: MemoryId,
    pub text: String,
    pub score: f32,
    pub confidence: f32,
    pub importance: u8,
    pub namespace: Namespace,
    pub provenance: Vec<MemoryId>,
}

/// A currently approved constraint. Legacy manually-created constraint
/// memories are treated as approved for backwards compatibility; extracted
/// constraints require an explicit `constraint_status:approved` tag.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BootstrapConstraint {
    pub id: String,
    pub text: String,
    pub priority: u8,
    pub scope: String,
    pub status: String,
    pub valid_until: Option<String>,
    pub provenance: Vec<MemoryId>,
}

/// Project-local skill metadata. Skill content is intentionally not returned
/// in bootstrap; the owning agent can load the validated file when selected.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BootstrapSkill {
    pub name: String,
    pub description: String,
    pub score: f32,
    pub path: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BootstrapBudget {
    pub requested: usize,
    pub used: usize,
    pub truncated: bool,
}

/// Structured, channel-separated startup context.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BootstrapResponse {
    pub schema_version: String,
    pub project: Option<String>,
    pub namespace: Namespace,
    pub agent: Option<String>,
    pub task: String,
    pub constraints: Vec<BootstrapConstraint>,
    pub facts: Vec<BootstrapMemory>,
    pub guardrails: Vec<BootstrapMemory>,
    pub policies: Vec<BootstrapMemory>,
    pub skills: Vec<BootstrapSkill>,
    pub abstentions: Vec<String>,
    pub budget: BootstrapBudget,
}

impl BootstrapResponse {
    /// Render a bounded human-readable form for launcher integrations. The
    /// structured response remains the canonical API; this is only a
    /// presentation adapter for clients that accept a system-prompt string.
    pub fn render_context(&self) -> String {
        if self.constraints.is_empty()
            && self.facts.is_empty()
            && self.guardrails.is_empty()
            && self.policies.is_empty()
            && self.skills.is_empty()
        {
            return String::new();
        }

        let mut out = format!(
            "# Mnemosyne Project Bootstrap\n\nNamespace: {}\n\n",
            self.namespace
        );
        if !self.constraints.is_empty() {
            out.push_str("## Approved project constraints\n\nApply these only within the stated project scope.\n\n");
            for item in &self.constraints {
                out.push_str(&format!(
                    "- [{}] {} (priority {}/10, status {})\n",
                    item.id,
                    render_text(&item.text),
                    item.priority,
                    item.status
                ));
            }
            out.push('\n');
        }
        append_memory_section(&mut out, "Factual evidence", &self.facts);
        append_memory_section(&mut out, "Failure-derived guardrails", &self.guardrails);
        append_memory_section(&mut out, "Response policies", &self.policies);
        if !self.skills.is_empty() {
            out.push_str("## Relevant project skills\n\n");
            for skill in &self.skills {
                out.push_str(&format!(
                    "- {} — {} (`{}`)\n",
                    render_text(&skill.name),
                    render_text(&skill.description),
                    render_text(&skill.path)
                ));
            }
            out.push('\n');
        }
        out.trim_end().to_string()
    }
}

fn append_memory_section(out: &mut String, heading: &str, items: &[BootstrapMemory]) {
    if items.is_empty() {
        return;
    }
    out.push_str(&format!("## {}\n\n", heading));
    for item in items {
        let sources = if item.provenance.is_empty() {
            String::new()
        } else {
            format!(
                "; sources: {}",
                item.provenance
                    .iter()
                    .map(ToString::to_string)
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        };
        out.push_str(&format!(
            "- [{}] {} (confidence {:.2}, score {:.2}{})\n",
            item.id,
            render_text(&item.text),
            item.confidence,
            item.score,
            sources
        ));
    }
    out.push('\n');
}

fn render_text(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('\n', " ")
        .replace('\r', " ")
        .replace('`', "'")
}

/// Build a bounded startup package from the canonical storage backend.
///
/// The function is deliberately read-only and backend-oriented so the exact
/// same selection behavior can be exposed through CLI and MCP.
pub async fn build_bootstrap(
    storage: &dyn StorageBackend,
    request: BootstrapRequest,
) -> Result<BootstrapResponse> {
    request.validate()?;
    let (namespace, project_root) = resolve_scope(request.project.as_deref(), request.namespace)?;
    let project = project_name(&namespace, project_root.as_deref());
    let query = match request.capability.as_deref() {
        Some(capability) => format!("{} {}", request.task, capability),
        None => request.task.clone(),
    };

    let mut abstentions = Vec::new();

    let approved_proposals = storage
        .list_approved_constraints(&namespace, MAX_CONSTRAINTS)
        .await?;
    let mut constraints = approved_proposals
        .into_iter()
        .map(constraint_from_proposal)
        .collect::<Result<Vec<_>>>()?
        .into_iter()
        .filter(is_current_constraint)
        .collect::<Vec<_>>();

    // Preserve compatibility with explicit constraint memories created before
    // the proposal table existed. Extracted rows remain excluded unless they
    // carry the explicit approval tag.
    let mut constraint_notes = storage
        .list_memories(
            Some(namespace.clone()),
            MAX_MEMORY_CANDIDATES,
            crate::storage::MemorySortOrder::Importance,
        )
        .await?
        .into_iter()
        .filter(|memory| is_approved_constraint(memory, request.min_confidence))
        .collect::<Vec<_>>();
    constraint_notes.sort_by(|left, right| {
        right
            .importance
            .cmp(&left.importance)
            .then_with(|| right.updated_at.cmp(&left.updated_at))
            .then_with(|| left.id.to_string().cmp(&right.id.to_string()))
    });
    for memory in constraint_notes {
        let text = compact_text(&memory);
        if !constraints.iter().any(|constraint| constraint.text == text) {
            constraints.push(BootstrapConstraint {
                id: memory.id.to_string(),
                text,
                priority: memory.importance,
                scope: namespace.to_string(),
                status: "approved".into(),
                valid_until: memory.expires_at.map(|value| value.to_rfc3339()),
                provenance: provenance_ids(&memory),
            });
        }
        if constraints.len() >= MAX_CONSTRAINTS {
            break;
        }
    }
    constraints.sort_by(|left, right| {
        right
            .priority
            .cmp(&left.priority)
            .then_with(|| left.id.cmp(&right.id))
    });
    constraints.truncate(MAX_CONSTRAINTS);
    if constraints.is_empty() {
        abstentions.push("constraints:none_approved".into());
    }

    let candidate_results = storage
        .hybrid_search_by_class(
            &query,
            Some(namespace.clone()),
            MAX_MEMORY_CANDIDATES,
            true,
            crate::types::MemoryClass::Knowledge,
        )
        .await?;

    let mut facts = Vec::new();
    let mut guardrails = Vec::new();
    for result in candidate_results {
        if result.score < ABSTENTION_THRESHOLD || result.memory.confidence < request.min_confidence
        {
            continue;
        }
        if result
            .memory
            .tags
            .iter()
            .any(|tag| tag == "reasoning_strategy")
        {
            if result
                .memory
                .tags
                .iter()
                .any(|tag| tag == "reasoning_guardrail")
            {
                guardrails.push(result);
            }
        } else if !matches!(
            result.memory.memory_type,
            MemoryType::Constraint | MemoryType::Constitution
        ) {
            facts.push(result);
        }
    }
    facts.sort_by(search_order);
    guardrails.sort_by(search_order);
    facts.truncate(MAX_FACTS);
    guardrails.truncate(MAX_GUARDRAILS);
    if facts.is_empty() {
        abstentions.push("facts:no_confident_match".into());
    }
    if guardrails.is_empty() {
        abstentions.push("guardrails:no_confident_match".into());
    }

    let mut policies = storage
        .interaction_policy_search(&query, MAX_POLICIES)
        .await?;
    policies.retain(|result| result.memory.confidence >= request.min_confidence);
    policies.sort_by(search_order);
    policies.truncate(MAX_POLICIES);
    if policies.is_empty() {
        abstentions.push("policies:no_eligible_match".into());
    }

    let skills = discover_project_skills(
        project_root.as_deref(),
        &query,
        request.min_confidence,
        &mut abstentions,
    )
    .await?;

    let facts = facts
        .into_iter()
        .map(|result| bootstrap_memory(&result))
        .collect::<Vec<_>>();
    let guardrails = guardrails
        .into_iter()
        .map(|result| bootstrap_memory(&result))
        .collect::<Vec<_>>();
    let policies = policies
        .into_iter()
        .map(|result| bootstrap_memory(&result))
        .collect::<Vec<_>>();

    let mut used = 0usize;
    let mut truncated = false;
    let constraints = fit_constraints(
        constraints,
        request.budget_tokens,
        &mut used,
        &mut truncated,
        &mut abstentions,
    );
    let facts = fit_memories(
        "facts",
        facts,
        request.budget_tokens,
        &mut used,
        &mut truncated,
        &mut abstentions,
    );
    let guardrails = fit_memories(
        "guardrails",
        guardrails,
        request.budget_tokens,
        &mut used,
        &mut truncated,
        &mut abstentions,
    );
    let policies = fit_memories(
        "policies",
        policies,
        request.budget_tokens,
        &mut used,
        &mut truncated,
        &mut abstentions,
    );
    let skills = fit_skills(
        skills,
        request.budget_tokens,
        &mut used,
        &mut truncated,
        &mut abstentions,
    );

    Ok(BootstrapResponse {
        schema_version: BOOTSTRAP_SCHEMA_VERSION.into(),
        project,
        namespace,
        agent: request.agent,
        task: request.task,
        constraints,
        facts,
        guardrails,
        policies,
        skills,
        abstentions,
        budget: BootstrapBudget {
            requested: request.budget_tokens,
            used,
            truncated,
        },
    })
}

fn resolve_scope(
    project: Option<&Path>,
    namespace: Option<Namespace>,
) -> Result<(Namespace, Option<PathBuf>)> {
    let mut detector = match project {
        Some(path) => crate::namespace::NamespaceDetector::with_base_dir(path.to_path_buf()),
        None => crate::namespace::NamespaceDetector::new(),
    };
    let project_root = if let Some(path) = project {
        Some(path.to_path_buf())
    } else {
        detector.detect_project_root()?
    };
    let namespace = match namespace {
        Some(namespace) => namespace,
        None => detector.detect_project_namespace()?,
    };
    Ok((namespace, project_root))
}

fn project_name(namespace: &Namespace, root: Option<&Path>) -> Option<String> {
    match namespace {
        Namespace::Project { name } | Namespace::Session { project: name, .. } => {
            Some(name.clone())
        }
        Namespace::Global | Namespace::Agent { .. } => root
            .and_then(|path| path.file_name())
            .and_then(|name| name.to_str())
            .map(ToOwned::to_owned),
    }
}

fn constraint_from_proposal(
    proposal: crate::storage::libsql::ConstraintProposalRecord,
) -> Result<BootstrapConstraint> {
    let source_ids: Vec<String> =
        serde_json::from_str(&proposal.source_memory_ids).map_err(|error| {
            MnemosyneError::ValidationError(format!(
                "invalid constraint proposal source IDs: {}",
                error
            ))
        })?;
    let provenance = source_ids
        .into_iter()
        .map(|id| {
            MemoryId::from_string(&id).map_err(|error| {
                MnemosyneError::ValidationError(format!(
                    "invalid constraint proposal source ID: {}",
                    error
                ))
            })
        })
        .collect::<Result<Vec<_>>>()?;
    Ok(BootstrapConstraint {
        id: proposal.id,
        text: crate::utils::string::truncate_at_char_boundary(proposal.text.trim(), 2_000),
        priority: proposal.priority,
        scope: proposal.scope,
        status: proposal.status,
        valid_until: proposal.valid_until,
        provenance,
    })
}

fn is_current_constraint(constraint: &BootstrapConstraint) -> bool {
    constraint
        .valid_until
        .as_deref()
        .map(|value| {
            chrono::DateTime::parse_from_rfc3339(value)
                .map(|timestamp| timestamp.with_timezone(&chrono::Utc) > chrono::Utc::now())
                .unwrap_or(false)
        })
        .unwrap_or(true)
}

fn is_approved_constraint(memory: &MemoryNote, min_confidence: f32) -> bool {
    if memory.memory_class != crate::types::MemoryClass::Knowledge
        || memory.is_archived
        || memory.superseded_by.is_some()
        || memory.confidence < min_confidence
        || memory
            .expires_at
            .is_some_and(|expires_at| expires_at <= chrono::Utc::now())
        || !matches!(
            memory.memory_type,
            MemoryType::Constraint | MemoryType::Constitution
        )
    {
        return false;
    }
    let status = memory
        .tags
        .iter()
        .find_map(|tag| tag.strip_prefix("constraint_status:"));
    match status {
        Some("approved") => true,
        Some(_) => false,
        // Existing manually-created constraints predate lifecycle tags. They
        // remain active for compatibility; extracted rows must be explicitly
        // tagged approved before they can enter bootstrap.
        None => !memory.tags.iter().any(|tag| tag == "extracted"),
    }
}

fn provenance_ids(memory: &MemoryNote) -> Vec<MemoryId> {
    memory
        .provenance
        .iter()
        .filter_map(|provenance| provenance.source_memory_id)
        .collect()
}

fn compact_text(memory: &MemoryNote) -> String {
    let text = if memory.content.trim().is_empty() {
        &memory.summary
    } else {
        &memory.content
    };
    crate::utils::string::truncate_at_char_boundary(text.trim(), 2_000)
}

fn bootstrap_memory(result: &SearchResult) -> BootstrapMemory {
    BootstrapMemory {
        id: result.memory.id,
        text: compact_text(&result.memory),
        score: result.score,
        confidence: result.memory.confidence,
        importance: result.memory.importance,
        namespace: result.memory.namespace.clone(),
        provenance: provenance_ids(&result.memory),
    }
}

fn search_order(left: &SearchResult, right: &SearchResult) -> std::cmp::Ordering {
    right
        .score
        .partial_cmp(&left.score)
        .unwrap_or(std::cmp::Ordering::Equal)
        .then_with(|| right.memory.importance.cmp(&left.memory.importance))
        .then_with(|| left.memory.id.to_string().cmp(&right.memory.id.to_string()))
}

async fn discover_project_skills(
    project_root: Option<&Path>,
    query: &str,
    min_confidence: f32,
    abstentions: &mut Vec<String>,
) -> Result<Vec<BootstrapSkill>> {
    let Some(root) = project_root else {
        abstentions.push("skills:no_project_root".into());
        return Ok(Vec::new());
    };
    let candidates = [root.join(".mnemosyne/skills"), root.join(".claude/skills")];
    let Some(directory) = candidates.iter().find(|path| path.is_dir()) else {
        abstentions.push("skills:no_project_skills_directory".into());
        return Ok(Vec::new());
    };
    let canonical_root = root.canonicalize().map_err(|error| {
        MnemosyneError::Io(std::io::Error::new(
            error.kind(),
            format!("failed to resolve project root: {}", error),
        ))
    })?;
    let canonical_directory = directory.canonicalize().map_err(|error| {
        MnemosyneError::Io(std::io::Error::new(
            error.kind(),
            format!("failed to resolve project skills directory: {}", error),
        ))
    })?;
    if !canonical_directory.starts_with(&canonical_root) {
        abstentions.push("skills:directory_outside_project".into());
        return Ok(Vec::new());
    }
    let mut discovery = SkillsDiscovery::new(directory.clone());
    let matches = discovery.discover_skills(query, MAX_SKILLS).await?;
    let mut skills = matches
        .into_iter()
        .filter(|skill| skill.score >= min_confidence.min(0.5))
        .map(|skill| skill_to_bootstrap(skill, root))
        .collect::<Vec<_>>();
    skills.sort_by(|left, right| {
        right
            .score
            .partial_cmp(&left.score)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| left.name.cmp(&right.name))
    });
    if skills.is_empty() {
        abstentions.push("skills:no_relevant_match".into());
    }
    Ok(skills)
}

fn skill_to_bootstrap(skill: SkillMatch, root: &Path) -> BootstrapSkill {
    let path = skill
        .metadata
        .file_path
        .strip_prefix(root)
        .unwrap_or(&skill.metadata.file_path)
        .to_string_lossy()
        .replace('\\', "/");
    BootstrapSkill {
        name: skill.metadata.name,
        description: skill.metadata.description,
        score: skill.score,
        path,
    }
}

fn estimate_tokens(text: &str) -> usize {
    text.chars().count().saturating_add(3) / 4
}

fn fit_constraints(
    items: Vec<BootstrapConstraint>,
    budget: usize,
    used: &mut usize,
    truncated: &mut bool,
    abstentions: &mut Vec<String>,
) -> Vec<BootstrapConstraint> {
    let mut out = Vec::new();
    for item in items {
        let cost = estimate_tokens(&item.text);
        if used.saturating_add(cost) > budget {
            *truncated = true;
            abstentions.push("budget_exhausted:constraints".into());
            break;
        }
        *used = used.saturating_add(cost);
        out.push(item);
    }
    out
}

fn fit_memories(
    section: &str,
    items: Vec<BootstrapMemory>,
    budget: usize,
    used: &mut usize,
    truncated: &mut bool,
    abstentions: &mut Vec<String>,
) -> Vec<BootstrapMemory> {
    let mut out = Vec::new();
    for item in items {
        let cost = estimate_tokens(&item.text);
        if used.saturating_add(cost) > budget {
            *truncated = true;
            abstentions.push(format!("budget_exhausted:{section}"));
            break;
        }
        *used = used.saturating_add(cost);
        out.push(item);
    }
    out
}

fn fit_skills(
    items: Vec<BootstrapSkill>,
    budget: usize,
    used: &mut usize,
    truncated: &mut bool,
    abstentions: &mut Vec<String>,
) -> Vec<BootstrapSkill> {
    let mut out = Vec::new();
    for item in items {
        let cost = estimate_tokens(&format!("{} {} {}", item.name, item.description, item.path));
        if used.saturating_add(cost) > budget {
            *truncated = true;
            abstentions.push("budget_exhausted:skills".into());
            break;
        }
        *used = used.saturating_add(cost);
        out.push(item);
    }
    out
}

fn validate_text(value: &str, name: &str, max: usize) -> Result<()> {
    if value.trim().is_empty() || value.chars().count() > max {
        return Err(MnemosyneError::ValidationError(format!(
            "{} must contain 1..={} characters",
            name, max
        )));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Utc;

    fn note(tags: Vec<&str>, memory_type: MemoryType) -> MemoryNote {
        MemoryNote {
            id: MemoryId::new(),
            namespace: Namespace::Project {
                name: "demo".into(),
            },
            created_at: Utc::now(),
            updated_at: Utc::now(),
            content: "constraint text".into(),
            summary: "constraint".into(),
            keywords: vec![],
            tags: tags.into_iter().map(String::from).collect(),
            context: String::new(),
            memory_type,
            memory_class: crate::types::MemoryClass::Knowledge,
            provenance: None,
            importance: 8,
            confidence: 0.9,
            links: vec![],
            related_files: vec![],
            related_entities: vec![],
            access_count: 0,
            last_accessed_at: Utc::now(),
            expires_at: None,
            is_archived: false,
            superseded_by: None,
            embedding: None,
            embedding_model: String::new(),
        }
    }

    #[test]
    fn extracted_constraints_require_explicit_approval() {
        assert!(!is_approved_constraint(
            &note(vec!["extracted"], MemoryType::Constraint),
            0.5
        ));
        assert!(is_approved_constraint(
            &note(
                vec!["extracted", "constraint_status:approved"],
                MemoryType::Constraint
            ),
            0.5
        ));
    }

    #[test]
    fn legacy_manual_constraints_remain_compatible() {
        assert!(is_approved_constraint(
            &note(vec!["manual"], MemoryType::Constraint),
            0.5
        ));
        assert!(!is_approved_constraint(
            &note(vec!["constraint_status:rejected"], MemoryType::Constraint),
            0.5
        ));
    }

    #[test]
    fn budget_never_exceeds_request() {
        let items = vec![BootstrapMemory {
            id: MemoryId::new(),
            text: "a very long memory".into(),
            score: 1.0,
            confidence: 1.0,
            importance: 10,
            namespace: Namespace::Global,
            provenance: vec![],
        }];
        let mut used = 0;
        let mut truncated = false;
        let mut abstentions = Vec::new();
        let selected = fit_memories(
            "facts",
            items,
            1,
            &mut used,
            &mut truncated,
            &mut abstentions,
        );
        assert!(selected.is_empty());
        assert!(used <= 1);
        assert!(truncated);
    }

    #[test]
    fn request_rejects_invalid_budget_and_confidence() {
        let request = BootstrapRequest {
            project: None,
            namespace: None,
            task: "review auth".into(),
            agent: None,
            capability: None,
            budget_tokens: 0,
            min_confidence: 0.5,
        };
        assert!(request.validate().is_err());
        let request = BootstrapRequest {
            budget_tokens: 100,
            min_confidence: 2.0,
            ..request
        };
        assert!(request.validate().is_err());
    }
}
