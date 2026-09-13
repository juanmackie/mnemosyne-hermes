//! Capture execution context for the automatic memory lifecycle.
//!
//! Hermes drives several non-interactive surfaces (scheduled jobs, session
//! flushes, sub-agents, background workers, skill loops) through the same
//! provider hooks as a real user turn. Automatic capture and injection must
//! only run for interactive, user-originated turns; otherwise machine
//! chatter and assistant-authored text can become asserted user facts.
//!
//! This module is the single source of truth for that decision. The adapter
//! sends an explicit context and this module re-checks it in the shared Rust
//! ingestion path, so neither side can silently re-enable capture.

/// Named contexts that must never trigger automatic capture or injection.
pub const SKIPPED_CONTEXTS: &[&str] = &["cron", "flush", "subagent", "background", "skill_loop"];

/// Where a lifecycle call originated.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExecutionContext {
    /// An interactive turn originated by the user.
    UserTurn,
    /// Scheduled / cron job.
    Cron,
    /// Session-end flush.
    Flush,
    /// Sub-agent invocation.
    Subagent,
    /// Background worker.
    Background,
    /// Skill loop.
    SkillLoop,
    /// A context string we do not recognize.
    Unknown,
}

impl ExecutionContext {
    /// Parse a caller-supplied context. An absent or empty value means an
    /// ordinary interactive turn; unknown values are treated as interactive
    /// only because they are not one of the explicitly skipped contexts.
    pub fn parse(raw: &str) -> Self {
        match raw.trim().to_ascii_lowercase().as_str() {
            "" | "user" | "user_turn" | "user-turn" | "interactive" | "conversation" => {
                Self::UserTurn
            }
            "cron" => Self::Cron,
            "flush" => Self::Flush,
            "subagent" | "sub_agent" | "sub-agent" => Self::Subagent,
            "background" | "bg" => Self::Background,
            "skill_loop" | "skill-loop" | "skillloop" => Self::SkillLoop,
            _ => Self::Unknown,
        }
    }

    /// Stable wire name for diagnostics and provenance tags.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::UserTurn => "user_turn",
            Self::Cron => "cron",
            Self::Flush => "flush",
            Self::Subagent => "subagent",
            Self::Background => "background",
            Self::SkillLoop => "skill_loop",
            Self::Unknown => "unknown",
        }
    }

    /// Whether an automatic completed-turn capture may run.
    pub fn allows_capture(self) -> bool {
        matches!(self, Self::UserTurn | Self::Unknown)
    }

    /// Whether automatic prefetch injection may run.
    pub fn allows_injection(self) -> bool {
        matches!(self, Self::UserTurn | Self::Unknown)
    }

    /// Human-readable reason used when a call is skipped.
    pub fn skip_reason(self) -> Option<&'static str> {
        match self {
            Self::Cron => Some("automatic memory is skipped for cron execution"),
            Self::Flush => Some("automatic memory is skipped for session flush"),
            Self::Subagent => Some("automatic memory is skipped for subagent execution"),
            Self::Background => Some("automatic memory is skipped for background execution"),
            Self::SkillLoop => Some("automatic memory is skipped for skill loops"),
            Self::UserTurn | Self::Unknown => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn skip_contexts_are_not_capturable_or_injectable() {
        for name in SKIPPED_CONTEXTS {
            let ctx = ExecutionContext::parse(name);
            assert!(!ctx.allows_capture(), "{name} must not capture");
            assert!(!ctx.allows_injection(), "{name} must not inject");
            assert!(ctx.skip_reason().is_some(), "{name} must explain the skip");
        }
    }

    #[test]
    fn user_and_absent_contexts_are_interactive() {
        assert!(ExecutionContext::parse("").allows_capture());
        assert!(ExecutionContext::parse("user_turn").allows_capture());
        assert!(ExecutionContext::parse("interactive").allows_capture());
        // A context string we do not recognize is not one of the explicitly
        // skipped contexts; it falls back to interactive behavior.
        assert!(ExecutionContext::parse("telegram").allows_capture());
        assert_eq!(ExecutionContext::parse("  CRON "), ExecutionContext::Cron);
    }
}
