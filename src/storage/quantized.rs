//! Quantized vector storage using TurboQuant (turbovec)
//!
//! Provides 8x memory compression (4-bit quantization) and SIMD-accelerated
//! search with in-kernel allowlist filtering for namespaces and memory classes.

use crate::error::{MnemosyneError, Result};
use crate::types::{MemoryClass, MemoryId, Namespace};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use turbovec::IdMapIndex;

/// Validates that an embedding coordinate slice has the expected dimension
/// and contains only finite, well-bounded values (rejecting NaN, +/-Inf, and |v| >= 1e16).
pub fn validate_vector(values: &[f32], expected_dim: usize) -> Result<()> {
    if values.len() != expected_dim {
        return Err(MnemosyneError::ValidationError(format!(
            "Embedding dim mismatch: expected {}, got {}",
            expected_dim,
            values.len()
        )));
    }
    for (i, &v) in values.iter().enumerate() {
        if !v.is_finite() || v.abs() >= 1e16 {
            return Err(MnemosyneError::ValidationError(format!(
                "Invalid coordinate at index {}: {}",
                i, v
            )));
        }
    }
    Ok(())
}

/// In-memory quantized vector index wrapping `turbovec::IdMapIndex` with
/// multi-attribute namespace and memory class allowlist indexing.
pub struct QuantizedVectorIndex {
    index: IdMapIndex,
    dim: usize,
    bit_width: usize,
    // ID mapping: MemoryId (Uuid) <-> u64 for IdMapIndex
    uuid_to_u64: HashMap<MemoryId, u64>,
    u64_to_uuid: HashMap<u64, MemoryId>,
    // In-memory allowlist indices for SIMD-accelerated block skipping
    namespace_ids: HashMap<Namespace, HashSet<u64>>,
    class_ids: HashMap<MemoryClass, HashSet<u64>>,
}

#[derive(Serialize)]
struct SerializedMetadata<'a> {
    dim: usize,
    bit_width: usize,
    uuid_to_u64: &'a HashMap<MemoryId, u64>,
    namespace_ids: Vec<(&'a Namespace, &'a HashSet<u64>)>,
    class_ids: Vec<(MemoryClass, &'a HashSet<u64>)>,
}

#[derive(Deserialize)]
struct DeserializedMetadata {
    dim: usize,
    bit_width: usize,
    uuid_to_u64: HashMap<MemoryId, u64>,
    namespace_ids: Vec<(Namespace, HashSet<u64>)>,
    class_ids: Vec<(MemoryClass, HashSet<u64>)>,
}

impl QuantizedVectorIndex {
    /// Create a new quantized vector index with specified vector dimensionality and quantization bit-width (typically 4).
    pub fn new(dim: usize, bit_width: usize) -> Result<Self> {
        if dim == 0 || dim % 8 != 0 || dim > 16384 {
            return Err(MnemosyneError::ValidationError(format!(
                "dim must be a positive multiple of 8 and <= 16384, got {}",
                dim
            )));
        }
        if !(2..=4).contains(&bit_width) {
            return Err(MnemosyneError::ValidationError(format!(
                "bit_width must be 2, 3, or 4, got {}",
                bit_width
            )));
        }
        let index = IdMapIndex::new(dim, bit_width).map_err(|e| {
            MnemosyneError::Other(format!("Failed to initialize IdMapIndex: {:?}", e))
        })?;
        Ok(Self {
            index,
            dim,
            bit_width,
            uuid_to_u64: HashMap::new(),
            u64_to_uuid: HashMap::new(),
            namespace_ids: HashMap::new(),
            class_ids: HashMap::new(),
        })
    }

    /// Add or update a vector with associated namespace and class metadata.
    pub fn add(
        &mut self,
        memory_id: MemoryId,
        embedding: &[f32],
        namespace: &Namespace,
        class: MemoryClass,
    ) -> Result<()> {
        validate_vector(embedding, self.dim)?;

        let id_u64 = match self.uuid_to_u64.get(&memory_id) {
            Some(&id) => {
                for set in self.namespace_ids.values_mut() {
                    set.remove(&id);
                }
                for set in self.class_ids.values_mut() {
                    set.remove(&id);
                }
                if self.index.contains(id) {
                    self.index.remove(id);
                }
                id
            }
            None => {
                let mut id = rand::random::<u64>();
                while id == 0 || self.u64_to_uuid.contains_key(&id) {
                    id = rand::random::<u64>();
                }
                self.uuid_to_u64.insert(memory_id, id);
                self.u64_to_uuid.insert(id, memory_id);
                id
            }
        };

        self.index
            .add_with_ids(embedding, &[id_u64])
            .map_err(|e| MnemosyneError::Database(format!("Turbovec add failed: {:?}", e)))?;

        self.namespace_ids
            .entry(namespace.clone())
            .or_default()
            .insert(id_u64);
        self.class_ids.entry(class).or_default().insert(id_u64);
        Ok(())
    }

    /// Remove a memory from the quantized index.
    pub fn remove(&mut self, memory_id: MemoryId) -> bool {
        if let Some(id_u64) = self.uuid_to_u64.remove(&memory_id) {
            self.u64_to_uuid.remove(&id_u64);
            for set in self.namespace_ids.values_mut() {
                set.remove(&id_u64);
            }
            for set in self.class_ids.values_mut() {
                set.remove(&id_u64);
            }
            self.index.remove(id_u64)
        } else {
            false
        }
    }

    /// Search with in-kernel SIMD allowlist filtering for namespace and memory class.
    pub fn search(
        &self,
        query: &[f32],
        limit: usize,
        namespace: Option<&Namespace>,
        class: Option<MemoryClass>,
    ) -> Result<Vec<(MemoryId, f32)>> {
        if limit == 0 || self.index.is_empty() {
            return Ok(Vec::new());
        }
        validate_vector(query, self.dim)?;

        let allowlist: Option<Vec<u64>> = match (namespace, class) {
            (Some(ns), Some(cls)) => {
                let ns_set = self.namespace_ids.get(ns);
                let cls_set = self.class_ids.get(&cls);
                match (ns_set, cls_set) {
                    (Some(a), Some(b)) => {
                        let intersection: Vec<u64> = a
                            .intersection(b)
                            .copied()
                            .filter(|id| self.index.contains(*id))
                            .collect();
                        if intersection.is_empty() {
                            return Ok(Vec::new());
                        }
                        Some(intersection)
                    }
                    _ => return Ok(Vec::new()),
                }
            }
            (Some(ns), None) => {
                let set = self.namespace_ids.get(ns);
                match set {
                    Some(s) => {
                        let allowed: Vec<u64> = s
                            .iter()
                            .copied()
                            .filter(|id| self.index.contains(*id))
                            .collect();
                        if allowed.is_empty() {
                            return Ok(Vec::new());
                        }
                        Some(allowed)
                    }
                    None => return Ok(Vec::new()),
                }
            }
            (None, Some(cls)) => {
                let set = self.class_ids.get(&cls);
                match set {
                    Some(s) => {
                        let allowed: Vec<u64> = s
                            .iter()
                            .copied()
                            .filter(|id| self.index.contains(*id))
                            .collect();
                        if allowed.is_empty() {
                            return Ok(Vec::new());
                        }
                        Some(allowed)
                    }
                    None => return Ok(Vec::new()),
                }
            }
            (None, None) => None,
        };

        let (scores, ids) = match allowlist {
            Some(ref allowed) => {
                match self
                    .index
                    .try_search_with_allowlist(query, limit, Some(allowed.as_slice()))
                {
                    Ok(res) => (res.scores, res.ids),
                    Err(turbovec::SearchError::AllowlistEmpty) => return Ok(Vec::new()),
                    Err(e) => {
                        return Err(MnemosyneError::Database(format!(
                            "Turbovec search failed: {:?}",
                            e
                        )))
                    }
                }
            }
            None => {
                let (scores, ids) = self.index.search(query, limit);
                (scores, ids)
            }
        };

        let mut results = Vec::with_capacity(ids.len());
        for (score, id_u64) in scores.into_iter().zip(ids.into_iter()) {
            if let Some(&uuid) = self.u64_to_uuid.get(&id_u64) {
                results.push((uuid, score));
            }
        }
        Ok(results)
    }

    /// Number of vectors indexed.
    pub fn len(&self) -> usize {
        self.index.len()
    }

    /// Whether the index contains zero vectors.
    pub fn is_empty(&self) -> bool {
        self.index.is_empty()
    }

    /// Vector dimensionality.
    pub fn dim(&self) -> usize {
        self.dim
    }

    /// Quantization bit-width.
    pub fn bit_width(&self) -> usize {
        self.bit_width
    }

    /// Check if a memory ID exists in the index.
    pub fn contains(&self, memory_id: &MemoryId) -> bool {
        self.uuid_to_u64.contains_key(memory_id)
    }

    /// Serialize index to in-memory bytes for LibSQL BLOB persistence.
    pub fn to_bytes(&self) -> Result<Vec<u8>> {
        let meta = SerializedMetadata {
            dim: self.dim,
            bit_width: self.bit_width,
            uuid_to_u64: &self.uuid_to_u64,
            namespace_ids: self.namespace_ids.iter().collect(),
            class_ids: self.class_ids.iter().map(|(c, s)| (*c, s)).collect(),
        };
        let meta_bytes = serde_json::to_vec(&meta)
            .map_err(|e| MnemosyneError::SerializationError(e.to_string()))?;
        let index_bytes = self.index.to_bytes();

        let mut out = Vec::with_capacity(4 + meta_bytes.len() + index_bytes.len());
        out.extend_from_slice(&(meta_bytes.len() as u32).to_le_bytes());
        out.extend_from_slice(&meta_bytes);
        out.extend_from_slice(&index_bytes);
        Ok(out)
    }

    /// Restore index from LibSQL BLOB.
    pub fn from_bytes(bytes: &[u8]) -> Result<Self> {
        if bytes.len() < 4 {
            return Err(MnemosyneError::SerializationError(
                "Byte payload too short".to_string(),
            ));
        }
        let meta_len = u32::from_le_bytes(bytes[0..4].try_into().unwrap()) as usize;
        if bytes.len() < 4 + meta_len {
            return Err(MnemosyneError::SerializationError(
                "Truncated metadata in byte payload".to_string(),
            ));
        }
        let meta: DeserializedMetadata = serde_json::from_slice(&bytes[4..4 + meta_len])
            .map_err(|e| MnemosyneError::SerializationError(e.to_string()))?;

        let index = IdMapIndex::from_bytes(&bytes[4 + meta_len..]).map_err(|e| {
            MnemosyneError::Other(format!("Failed to restore IdMapIndex: {:?}", e))
        })?;

        let u64_to_uuid = meta.uuid_to_u64.iter().map(|(&u, &n)| (n, u)).collect();

        Ok(Self {
            index,
            dim: meta.dim,
            bit_width: meta.bit_width,
            uuid_to_u64: meta.uuid_to_u64,
            u64_to_uuid,
            namespace_ids: meta.namespace_ids.into_iter().collect(),
            class_ids: meta.class_ids.into_iter().collect(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn generate_vector(dim: usize, seed: f32) -> Vec<f32> {
        let mut vec = Vec::with_capacity(dim);
        for i in 0..dim {
            vec.push(((i as f32 + seed) * 0.1).sin());
        }
        // Normalize
        let norm: f32 = vec.iter().map(|x| x * x).sum::<f32>().sqrt();
        if norm > 0.0 {
            for x in vec.iter_mut() {
                *x /= norm;
            }
        }
        vec
    }

    #[test]
    fn test_quantized_vector_index() {
        let dim = 64;
        let mut index = QuantizedVectorIndex::new(dim, 4).expect("init index");

        let id1 = MemoryId::new();
        let id2 = MemoryId::new();
        let ns = Namespace::Project {
            name: "test-proj".to_string(),
        };
        let v1 = generate_vector(dim, 1.0);
        let v2 = generate_vector(dim, 2.0);

        index.add(id1, &v1, &ns, MemoryClass::Knowledge).expect("add v1");
        index.add(id2, &v2, &ns, MemoryClass::Knowledge).expect("add v2");

        assert_eq!(index.len(), 2);
        assert!(index.contains(&id1));
        assert!(index.contains(&id2));

        let results = index.search(&v1, 2, None, None).expect("search v1");
        assert!(!results.is_empty());
        assert_eq!(results[0].0, id1);

        // Serialization roundtrip
        let bytes = index.to_bytes().expect("to_bytes");
        let restored = QuantizedVectorIndex::from_bytes(&bytes).expect("from_bytes");
        assert_eq!(restored.len(), 2);
        let restored_results = restored.search(&v1, 2, None, None).expect("restored search");
        assert_eq!(restored_results[0].0, id1);
    }

    #[test]
    fn test_quantized_allowlist_filtering() {
        let dim = 32;
        let mut index = QuantizedVectorIndex::new(dim, 4).expect("init index");

        let ns_a = Namespace::Project {
            name: "proj-a".to_string(),
        };
        let ns_b = Namespace::Project {
            name: "proj-b".to_string(),
        };

        let id_a = MemoryId::new();
        let id_b = MemoryId::new();
        let id_policy = MemoryId::new();

        let v_a = generate_vector(dim, 10.0);
        let v_b = generate_vector(dim, 20.0);
        let v_pol = generate_vector(dim, 30.0);

        index.add(id_a, &v_a, &ns_a, MemoryClass::Knowledge).unwrap();
        index.add(id_b, &v_b, &ns_b, MemoryClass::Knowledge).unwrap();
        index.add(id_policy, &v_pol, &ns_a, MemoryClass::InteractionPolicy).unwrap();

        // Search in ns_a only -> should match id_a and id_policy, not id_b
        let res_a = index.search(&v_b, 10, Some(&ns_a), None).unwrap();
        for (id, _) in &res_a {
            assert_ne!(*id, id_b);
        }

        // Search in ns_a with Knowledge class only -> should match id_a only
        let res_a_know = index
            .search(&v_a, 10, Some(&ns_a), Some(MemoryClass::Knowledge))
            .unwrap();
        assert_eq!(res_a_know.len(), 1);
        assert_eq!(res_a_know[0].0, id_a);
    }

    #[test]
    fn test_quantized_validation() {
        let dim = 16;
        let mut index = QuantizedVectorIndex::new(dim, 4).unwrap();
        let id = MemoryId::new();
        let ns = Namespace::Global;

        // Wrong dimension
        let short_vec = vec![0.0; 8];
        assert!(index.add(id, &short_vec, &ns, MemoryClass::Knowledge).is_err());

        // Non-finite coordinate
        let mut nan_vec = vec![0.0; 16];
        nan_vec[3] = f32::NAN;
        assert!(index.add(id, &nan_vec, &ns, MemoryClass::Knowledge).is_err());

        // Extreme coordinate
        let mut big_vec = vec![0.0; 16];
        big_vec[0] = 1e17;
        assert!(index.add(id, &big_vec, &ns, MemoryClass::Knowledge).is_err());
    }
}
