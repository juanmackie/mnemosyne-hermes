//! Lightweight markdown/ICS highlighting without the optional tree-sitter grammars.

use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};

#[derive(Debug, Clone)]
pub struct HighlightedSpan {
    pub text: String,
    pub style: Style,
    pub source: HighlightSource,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum HighlightSource {
    Plain = 0,
    Syntax = 1,
    Semantic = 2,
}

pub struct MarkdownHighlighter {
    syntax_enabled: bool,
    semantic_enabled: bool,
}

impl MarkdownHighlighter {
    pub fn new() -> anyhow::Result<Self> {
        Ok(Self {
            syntax_enabled: false,
            semantic_enabled: true,
        })
    }

    pub fn set_syntax_enabled(&mut self, enabled: bool) {
        self.syntax_enabled = enabled;
    }

    pub fn is_syntax_enabled(&self) -> bool {
        self.syntax_enabled
    }

    pub fn set_semantic_enabled(&mut self, enabled: bool) {
        self.semantic_enabled = enabled;
    }

    pub fn is_semantic_enabled(&self) -> bool {
        self.semantic_enabled
    }

    pub fn highlight_line(&mut self, text: &str) -> Line<'static> {
        if self.semantic_enabled {
            if let Some(spans) = self.highlight_semantic_patterns(text) {
                return Line::from(spans);
            }
        }
        Line::from(vec![Span::raw(text.to_string())])
    }

    fn highlight_semantic_patterns(&self, text: &str) -> Option<Vec<Span<'static>>> {
        let mut spans = Vec::new();
        let mut last_pos = 0;
        let mut found_pattern = false;

        for (idx, ch) in text.char_indices() {
            if !matches!(ch, '#' | '@' | '?') {
                continue;
            }
            if idx > last_pos {
                spans.push(Span::raw(text[last_pos..idx].to_string()));
            }
            let end = text[idx..]
                .find(|c: char| c.is_whitespace() || matches!(c, ',' | '.' | ')'))
                .map(|pos| idx + pos)
                .unwrap_or(text.len());
            let style = match ch {
                '#' => Style::default().fg(Color::Blue).add_modifier(Modifier::BOLD),
                '@' => Style::default().fg(Color::Green).add_modifier(Modifier::BOLD),
                '?' => Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD),
                _ => Style::default(),
            };
            spans.push(Span::styled(text[idx..end].to_string(), style));
            last_pos = end;
            found_pattern = true;
        }

        if last_pos < text.len() {
            spans.push(Span::raw(text[last_pos..].to_string()));
        }
        found_pattern.then_some(spans)
    }
}
