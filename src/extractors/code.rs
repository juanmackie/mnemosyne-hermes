//! Structure-aware code symbol extraction using Tree-Sitter and regex fallback
//!
//! Parses code snippets and source files to extract enclosing symbol names,
//! signatures, parameter types, docstrings, and breadcrumbs. Tags memories with
//! structured `#symbol:...`, `#scope:...`, and `#breadcrumb:...` metadata.

use crate::types::{MemoryNote, MemoryType};
use regex::Regex;

/// Extracted symbol representation from source code.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExtractedCodeSymbol {
    /// Identifier name (e.g., "hybrid_search", "QuantizedVectorIndex")
    pub name: String,
    /// Symbol classification: "fn", "impl", "struct", "enum", "trait", "class"
    pub kind: String,
    /// Full definition signature (e.g., "fn hybrid_search(&self, query: &str) -> Result<Vec<SearchResult>>")
    pub signature: Option<String>,
    /// Associated doc comment or docstring
    pub docstring: Option<String>,
    /// Parent enclosing scope (e.g., "LibsqlStorage")
    pub scope: Option<String>,
    /// Hierarchical breadcrumb path (e.g., "LibsqlStorage::hybrid_search")
    pub breadcrumb: String,
}

/// Extract symbols from a source code snippet or file.
pub fn extract_symbols(code: &str, language: Option<&str>) -> Vec<ExtractedCodeSymbol> {
    let lang = language.unwrap_or_else(|| detect_language_hint(code));
    match lang.to_lowercase().as_str() {
        "rs" | "rust" => extract_rust_symbols(code),
        "py" | "python" => extract_python_symbols(code),
        _ => {
            // Try both Rust and Python heuristics
            let mut syms = extract_rust_symbols(code);
            if syms.is_empty() {
                syms = extract_python_symbols(code);
            }
            syms
        }
    }
}

/// Detect language hint from code fences or content.
fn detect_language_hint(code: &str) -> &'static str {
    if code.contains("fn ") || code.contains("impl ") || code.contains("pub struct ") || code.contains("let mut ") {
        "rust"
    } else if code.contains("def ") || code.contains("class ") || code.contains("import ") && code.contains(":") {
        "python"
    } else {
        "unknown"
    }
}

#[cfg(feature = "ics-syntax")]
fn extract_rust_symbols(code: &str) -> Vec<ExtractedCodeSymbol> {
    use tree_sitter::Parser;

    let mut parser = Parser::new();
    if parser.set_language(&tree_sitter_rust::LANGUAGE.into()).is_err() {
        return extract_rust_symbols_fallback(code);
    }

    let tree = match parser.parse(code, None) {
        Some(t) => t,
        None => return extract_rust_symbols_fallback(code),
    };

    let mut symbols = Vec::new();
    let root = tree.root_node();
    walk_rust_node(root, code, None, &mut symbols);

    if symbols.is_empty() {
        extract_rust_symbols_fallback(code)
    } else {
        symbols
    }
}

#[cfg(feature = "ics-syntax")]
fn walk_rust_node(
    node: tree_sitter::Node,
    code: &str,
    current_scope: Option<&str>,
    symbols: &mut Vec<ExtractedCodeSymbol>,
) {
    let kind = node.kind();
    let mut new_scope = current_scope;

    match kind {
        "impl_item" => {
            if let Some(type_node) = node.child_by_field_name("type") {
                if let Ok(name) = type_node.utf8_text(code.as_bytes()) {
                    let name = name.trim().to_string();
                    let breadcrumb = match current_scope {
                        Some(scope) => format!("{}::{}", scope, name),
                        None => name.clone(),
                    };
                    symbols.push(ExtractedCodeSymbol {
                        name: name.clone(),
                        kind: "impl".to_string(),
                        signature: Some(format!("impl {}", name)),
                        docstring: extract_preceding_doc(node, code),
                        scope: current_scope.map(ToString::to_string),
                        breadcrumb,
                    });
                    new_scope = Some(Box::leak(name.into_boxed_str()));
                }
            }
        }
        "struct_item" | "enum_item" | "trait_item" => {
            if let Some(name_node) = node.child_by_field_name("name") {
                if let Ok(name) = name_node.utf8_text(code.as_bytes()) {
                    let item_kind = match kind {
                        "struct_item" => "struct",
                        "enum_item" => "enum",
                        _ => "trait",
                    };
                    let name = name.trim().to_string();
                    let breadcrumb = match current_scope {
                        Some(scope) => format!("{}::{}", scope, name),
                        None => name.clone(),
                    };
                    symbols.push(ExtractedCodeSymbol {
                        name: name.clone(),
                        kind: item_kind.to_string(),
                        signature: Some(format!("{} {}", item_kind, name)),
                        docstring: extract_preceding_doc(node, code),
                        scope: current_scope.map(ToString::to_string),
                        breadcrumb,
                    });
                    new_scope = Some(Box::leak(name.into_boxed_str()));
                }
            }
        }
        "function_item" => {
            if let Some(name_node) = node.child_by_field_name("name") {
                if let Ok(name) = name_node.utf8_text(code.as_bytes()) {
                    let name = name.trim().to_string();
                    let breadcrumb = match current_scope {
                        Some(scope) => format!("{}::{}", scope, name),
                        None => name.clone(),
                    };
                    let sig = node
                        .child_by_field_name("parameters")
                        .and_then(|p| p.utf8_text(code.as_bytes()).ok())
                        .map(|params| format!("fn {}{}", name, params));

                    symbols.push(ExtractedCodeSymbol {
                        name,
                        kind: "fn".to_string(),
                        signature: sig,
                        docstring: extract_preceding_doc(node, code),
                        scope: current_scope.map(ToString::to_string),
                        breadcrumb,
                    });
                }
            }
        }
        _ => {}
    }

    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        walk_rust_node(child, code, new_scope, symbols);
    }
}

#[cfg(feature = "ics-syntax")]
fn extract_preceding_doc(node: tree_sitter::Node, code: &str) -> Option<String> {
    let mut prev = node.prev_sibling();
    let mut doc_lines = Vec::new();
    while let Some(p) = prev {
        if p.kind() == "line_comment" {
            if let Ok(text) = p.utf8_text(code.as_bytes()) {
                if text.starts_with("///") || text.starts_with("//!") {
                    doc_lines.push(text.trim().to_string());
                } else {
                    break;
                }
            }
        } else {
            break;
        }
        prev = p.prev_sibling();
    }
    if doc_lines.is_empty() {
        None
    } else {
        doc_lines.reverse();
        Some(doc_lines.join("\n"))
    }
}

#[cfg(feature = "ics-syntax")]
fn extract_python_symbols(code: &str) -> Vec<ExtractedCodeSymbol> {
    use tree_sitter::Parser;

    let mut parser = Parser::new();
    if parser.set_language(&tree_sitter_python::LANGUAGE.into()).is_err() {
        return extract_python_symbols_fallback(code);
    }

    let tree = match parser.parse(code, None) {
        Some(t) => t,
        None => return extract_python_symbols_fallback(code),
    };

    let mut symbols = Vec::new();
    let root = tree.root_node();
    walk_python_node(root, code, None, &mut symbols);

    if symbols.is_empty() {
        extract_python_symbols_fallback(code)
    } else {
        symbols
    }
}

#[cfg(feature = "ics-syntax")]
fn walk_python_node(
    node: tree_sitter::Node,
    code: &str,
    current_scope: Option<&str>,
    symbols: &mut Vec<ExtractedCodeSymbol>,
) {
    let kind = node.kind();
    let mut new_scope = current_scope;

    match kind {
        "class_definition" => {
            if let Some(name_node) = node.child_by_field_name("name") {
                if let Ok(name) = name_node.utf8_text(code.as_bytes()) {
                    let name = name.trim().to_string();
                    let breadcrumb = match current_scope {
                        Some(scope) => format!("{}.{}", scope, name),
                        None => name.clone(),
                    };
                    symbols.push(ExtractedCodeSymbol {
                        name: name.clone(),
                        kind: "class".to_string(),
                        signature: Some(format!("class {}", name)),
                        docstring: extract_python_docstring(node, code),
                        scope: current_scope.map(ToString::to_string),
                        breadcrumb,
                    });
                    new_scope = Some(Box::leak(name.into_boxed_str()));
                }
            }
        }
        "function_definition" => {
            if let Some(name_node) = node.child_by_field_name("name") {
                if let Ok(name) = name_node.utf8_text(code.as_bytes()) {
                    let name = name.trim().to_string();
                    let breadcrumb = match current_scope {
                        Some(scope) => format!("{}.{}", scope, name),
                        None => name.clone(),
                    };
                    let params = node
                        .child_by_field_name("parameters")
                        .and_then(|p| p.utf8_text(code.as_bytes()).ok())
                        .unwrap_or("()");
                    let return_type = node
                        .child_by_field_name("return_type")
                        .and_then(|r| r.utf8_text(code.as_bytes()).ok())
                        .map(|r| format!(" -> {}", r))
                        .unwrap_or_default();

                    symbols.push(ExtractedCodeSymbol {
                        name,
                        kind: "fn".to_string(),
                        signature: Some(format!("def {}{}{}", name, params, return_type)),
                        docstring: extract_python_docstring(node, code),
                        scope: current_scope.map(ToString::to_string),
                        breadcrumb,
                    });
                }
            }
        }
        _ => {}
    }

    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        walk_python_node(child, code, new_scope, symbols);
    }
}

#[cfg(feature = "ics-syntax")]
fn extract_python_docstring(node: tree_sitter::Node, code: &str) -> Option<String> {
    if let Some(body) = node.child_by_field_name("body") {
        if let Some(first_stmt) = body.child(0) {
            if first_stmt.kind() == "expression_statement" {
                if let Some(str_node) = first_stmt.child(0) {
                    if str_node.kind() == "string" {
                        if let Ok(text) = str_node.utf8_text(code.as_bytes()) {
                            return Some(text.trim().to_string());
                        }
                    }
                }
            }
        }
    }
    None
}

#[cfg(not(feature = "ics-syntax"))]
fn extract_rust_symbols(code: &str) -> Vec<ExtractedCodeSymbol> {
    extract_rust_symbols_fallback(code)
}

#[cfg(not(feature = "ics-syntax"))]
fn extract_python_symbols(code: &str) -> Vec<ExtractedCodeSymbol> {
    extract_python_symbols_fallback(code)
}

fn extract_rust_symbols_fallback(code: &str) -> Vec<ExtractedCodeSymbol> {
    let mut symbols = Vec::new();

    // Regex for struct, enum, trait, impl
    let type_regex = Regex::new(r#"(?m)(?:pub\s+)?(struct|enum|trait)\s+([a-zA-Z0-9_]+)"#).unwrap();
    let impl_regex = Regex::new(r#"(?m)impl(?:<[^>]+>)?\s+(?:[a-zA-Z0-9_:]+\s+for\s+)?([a-zA-Z0-9_]+)"#).unwrap();
    let fn_regex = Regex::new(r#"(?m)(?:pub\s+)?(?:async\s+)?fn\s+([a-zA-Z0-9_]+)\s*(?:<[^>]+>)?\s*(\([^\)]*\))(?:\s*->\s*([^{;]+))?"#).unwrap();

    let mut current_impl: Option<String> = None;

    for line in code.lines() {
        let trimmed = line.trim();
        if let Some(caps) = impl_regex.captures(trimmed) {
            let name = caps[1].to_string();
            current_impl = Some(name.clone());
            symbols.push(ExtractedCodeSymbol {
                name: name.clone(),
                kind: "impl".to_string(),
                signature: Some(format!("impl {}", name)),
                docstring: None,
                scope: None,
                breadcrumb: name,
            });
            continue;
        }

        if let Some(caps) = type_regex.captures(trimmed) {
            let kind = caps[1].to_string();
            let name = caps[2].to_string();
            symbols.push(ExtractedCodeSymbol {
                name: name.clone(),
                kind: kind.clone(),
                signature: Some(format!("{} {}", kind, name)),
                docstring: None,
                scope: None,
                breadcrumb: name,
            });
            continue;
        }

        if let Some(caps) = fn_regex.captures(trimmed) {
            let name = caps[1].to_string();
            let params = caps[2].to_string();
            let _ret = caps.get(3).map(|m| format!(" -> {}", m.as_str().trim())).unwrap_or_default();
            let scope = current_impl.clone();
            let breadcrumb = match &scope {
                Some(s) => format!("{}::{}", s, name),
                None => name.clone(),
            };
            symbols.push(ExtractedCodeSymbol {
                name: name.clone(),
                kind: "fn".to_string(),
                signature: Some(format!("fn {}{}{}", name, params, _ret)),
                docstring: None,
                scope,
                breadcrumb,
            });
        }
    }

    symbols
}

fn extract_python_symbols_fallback(code: &str) -> Vec<ExtractedCodeSymbol> {
    let mut symbols = Vec::new();
    let class_regex = Regex::new(r#"(?m)^class\s+([a-zA-Z0-9_]+)(?:\([^)]*\))?:"#).unwrap();
    let fn_regex = Regex::new(r#"(?m)^(?:\s+)?(?:async\s+)?def\s+([a-zA-Z0-9_]+)\s*(\([^\)]*\))(?:\s*->\s*([^:]+))?:"#).unwrap();

    let mut current_class: Option<String> = None;

    for line in code.lines() {
        if let Some(caps) = class_regex.captures(line) {
            let name = caps[1].to_string();
            current_class = Some(name.clone());
            symbols.push(ExtractedCodeSymbol {
                name: name.clone(),
                kind: "class".to_string(),
                signature: Some(format!("class {}:", name)),
                docstring: None,
                scope: None,
                breadcrumb: name,
            });
            continue;
        }

        if let Some(caps) = fn_regex.captures(line) {
            let name = caps[1].to_string();
            let params = caps[2].to_string();
            let _ret = caps.get(3).map(|m| format!(" -> {}", m.as_str().trim())).unwrap_or_default();
            let is_method = line.starts_with("    ") || line.starts_with("\t");
            let scope = if is_method { current_class.clone() } else { None };
            let breadcrumb = match &scope {
                Some(s) => format!("{}.{}", s, name),
                None => name.clone(),
            };
            symbols.push(ExtractedCodeSymbol {
                name: name.clone(),
                kind: "fn".to_string(),
                signature: Some(format!("def {}{}:", name, params)),
                docstring: None,
                scope,
                breadcrumb,
            });
        }
    }

    symbols
}

/// Generate structured tag strings from extracted symbols.
pub fn generate_symbol_tags(symbols: &[ExtractedCodeSymbol]) -> Vec<String> {
    let mut tags = Vec::new();
    for sym in symbols {
        tags.push(format!("#symbol:{}", sym.name));
        tags.push(format!("#kind:{}", sym.kind));
        if let Some(ref scope) = sym.scope {
            tags.push(format!("#scope:{}", scope));
        }
        tags.push(format!("#breadcrumb:{}", sym.breadcrumb));
    }
    tags.sort();
    tags.dedup();
    tags
}

/// Inspect memory note content; if it represents an Architecture/Decision or contains
/// code snippets, extract code symbols and enrich the memory's tags.
pub fn enrich_memory_with_code_symbols(memory: &mut MemoryNote) {
    let should_extract = matches!(
        memory.memory_type,
        MemoryType::ArchitectureDecision | MemoryType::CodePattern | MemoryType::BugFix
    ) || memory.content.contains("```");

    if !should_extract {
        return;
    }

    let symbols = extract_symbols(&memory.content, None);
    if !symbols.is_empty() {
        let new_tags = generate_symbol_tags(&symbols);
        for tag in new_tags {
            if !memory.tags.contains(&tag) {
                memory.tags.push(tag);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_rust_symbol_extraction() {
        let code = r#"
/// High performance vector index
impl QuantizedVectorIndex {
    /// Search with allowlist
    pub fn search(&self, query: &[f32], limit: usize) -> Result<Vec<(MemoryId, f32)>> {
        Ok(vec![])
    }
}
"#;
        let symbols = extract_symbols(code, Some("rust"));
        assert!(!symbols.is_empty());
        let fn_sym = symbols.iter().find(|s| s.name == "search").expect("found search fn");
        assert_eq!(fn_sym.kind, "fn");
        assert_eq!(fn_sym.scope.as_deref(), Some("QuantizedVectorIndex"));
        assert_eq!(fn_sym.breadcrumb, "QuantizedVectorIndex::search");

        let tags = generate_symbol_tags(&symbols);
        assert!(tags.contains(&"#symbol:search".to_string()));
        assert!(tags.contains(&"#scope:QuantizedVectorIndex".to_string()));
    }

    #[test]
    fn test_python_symbol_extraction() {
        let code = r#"
class MemoryRetriever:
    def retrieve(self, query: str, limit: int = 10) -> list:
        pass
"#;
        let symbols = extract_symbols(code, Some("python"));
        assert!(!symbols.is_empty());
        let fn_sym = symbols.iter().find(|s| s.name == "retrieve").expect("found retrieve fn");
        assert_eq!(fn_sym.kind, "fn");
        assert_eq!(fn_sym.scope.as_deref(), Some("MemoryRetriever"));
        assert_eq!(fn_sym.breadcrumb, "MemoryRetriever.retrieve");
    }

    #[test]
    fn test_enrich_memory_tags() {
        use chrono::Utc;
        use crate::types::{MemoryId, MemoryClass};
        let mut note = MemoryNote {
            id: MemoryId::new(),
            namespace: crate::types::Namespace::Global,
            created_at: Utc::now(),
            updated_at: Utc::now(),
            content: "fn compute_rrf_ranking(k: f32) -> f32 { 1.0 / k }".to_string(),
            summary: "RRF rank function".to_string(),
            keywords: vec![],
            tags: vec![],
            context: String::new(),
            memory_type: MemoryType::ArchitectureDecision,
            memory_class: MemoryClass::default(),
            provenance: None,
            importance: 5,
            confidence: 1.0,
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
        };
        enrich_memory_with_code_symbols(&mut note);
        assert!(note.tags.iter().any(|t| t == "#symbol:compute_rrf_ranking"));
    }
}
