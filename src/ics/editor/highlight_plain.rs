//! Lightweight syntax highlighting used when the optional tree-sitter feature is off.

use super::Language;
use anyhow::Result;
use ratatui::style::Color;

#[derive(Debug, Clone)]
pub struct HighlightSpan {
    pub start: usize,
    pub end: usize,
    pub color: Color,
    pub kind: HighlightKind,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HighlightKind {
    Heading,
    Bold,
    Italic,
    CodeInline,
    CodeBlock,
    Link,
    ListMarker,
    Quote,
    Comment,
    Keyword,
    String,
    Number,
    Operator,
    Text,
}

impl HighlightKind {
    pub fn color(&self) -> Color {
        match self {
            Self::Heading => Color::Rgb(180, 180, 220),
            Self::Bold => Color::Rgb(200, 200, 200),
            Self::Italic => Color::Rgb(160, 160, 180),
            Self::CodeInline => Color::Rgb(180, 200, 180),
            Self::CodeBlock => Color::Rgb(170, 190, 170),
            Self::Link => Color::Rgb(150, 180, 210),
            Self::ListMarker => Color::Rgb(190, 170, 150),
            Self::Quote => Color::Rgb(160, 160, 160),
            Self::Comment => Color::Rgb(140, 140, 140),
            Self::Keyword => Color::Rgb(180, 160, 200),
            Self::String => Color::Rgb(180, 200, 160),
            Self::Number => Color::Rgb(200, 180, 160),
            Self::Operator => Color::Rgb(170, 170, 190),
            Self::Text => Color::White,
        }
    }
}

pub struct Highlighter {
    language: Language,
}

impl Highlighter {
    pub fn new(language: Language) -> Result<Self> {
        Ok(Self { language })
    }

    pub fn highlight(&mut self, text: &str) -> Vec<HighlightSpan> {
        let trimmed = text.trim_start();
        let start = text.len() - trimmed.len();
        let kind = if trimmed.starts_with('#') {
            HighlightKind::Heading
        } else if trimmed.starts_with('>') {
            HighlightKind::Quote
        } else if trimmed.starts_with("- ") || trimmed.starts_with("* ") {
            HighlightKind::ListMarker
        } else {
            HighlightKind::Text
        };
        if text.is_empty() {
            Vec::new()
        } else {
            vec![HighlightSpan {
                start,
                end: text.len(),
                color: kind.color(),
                kind,
            }]
        }
    }

    pub fn set_language(&mut self, language: Language) -> Result<()> {
        self.language = language;
        Ok(())
    }
}
