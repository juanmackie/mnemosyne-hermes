//! Code and symbol extraction modules
//!
//! Provides structure-aware code symbol extraction for Rust, Python, and other languages.

pub mod code;
pub use code::{enrich_memory_with_code_symbols, extract_symbols, generate_symbol_tags, ExtractedCodeSymbol};
