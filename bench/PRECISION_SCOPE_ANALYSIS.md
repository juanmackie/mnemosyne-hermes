# PrecisionMemBench scope and query-length analysis

**Status:** isolated engine reproductions complete against the public PrecisionMemBench retrieval fixture. The original mnemosyne adapter's per-case output is unavailable, so the scratch results below are not claimed to reproduce its published aggregate scores.

This note investigates P1 (scope-aware lexical matching), P2 (long-query normalization), and the P3 watch list from `mnemosyne-findings-2026-10-12.md`. It does not claim benchmark-wide score changes. I inspected the public benchmark clone at commit `61a712a1754e7d27b6dee42da32a2c214b003ece`: [retrieval cases](https://github.com/tenurehq/precisionMemBench/blob/61a712a1754e7d27b6dee42da32a2c214b003ece/fixtures/retrieval.cases.json) and [belief seed](https://github.com/tenurehq/precisionMemBench/blob/61a712a1754e7d27b6dee42da32a2c214b003ece/fixtures/beliefs.seed.json). The original mnemosyne adapter and its per-case result JSON are not in that clone, so the scratch results below are not claimed to reproduce its published aggregate scores.

## What `scope` means in the engine

In the installed `mnemosyne-memory 3.15.1` engine, `scope` is an access-visibility field with the values `session` and `global`. Recall normally admits rows from the current session plus global rows. Cross-session recall is a separate runtime setting. `scope` is not a topic or domain label.

Arbitrary `metadata` is serialized into `metadata_json`. For example, the engine accepts `metadata={"domain": "novel-writing"}`, but the recall API does not expose a metadata/domain predicate. Its filters include source, dates, author identity, channel, veracity, and memory type. The `topic` argument is documented as matching the source field. The Hermes provider recall entry point also does not accept a semantic domain filter. Therefore the benchmark's novel-writing and software-code labels are not engine scopes, and assigning authorization from a vector winner would invent a policy the data model does not define.

The public fixture makes the requested scope behavior precise. `happy-writing-scope` and `conflict-scope-disambiguation-redis-writing` pass `scope=["domain:writing"]`. The [benchmark README at that commit](https://github.com/tenurehq/precisionMemBench/blob/61a712a1754e7d27b6dee42da32a2c214b003ece/README.md) says its external-provider adapter was corrected to forward active scopes and route open questions separately. The original adapter used for the findings run is unavailable here, so I cannot determine whether that correction was in effect during the run or whether the findings reflect a dropped scope.

The clone contains an untracked `probe.py` added locally for this investigation. It is not the benchmark adapter and does not establish how the findings run handled scopes. For an isolated engine replay, that local probe serializes each belief's scope list into `metadata.benchmark_scope`, sets engine `scope="session"`, flattens each belief into text, and recalls without a domain predicate. Those are choices of the exploratory probe; results below from this path characterize generic engine recall with flattened seed data only.

The reported cross-domain result is also reproducible as a lexical retrieval leak when similarly labeled memories share one engine session. In an isolated scratch database with embeddings disabled, I stored:

| Memory | Stored metadata | Engine scope |
| --- | --- | --- |
| `In the novel, chapter opening: Mara waits at the door before leaving for the coast.` | `domain=novel-writing` | `session` |
| `In Redis caching code, writing a value uses SET with an expiry.` | `domain=software-code` | `session` |

`recall("How should I open the next chapter with Mara?")` returned only the novel memory (score `0.396`, lexical score `0.7`). Adding the generic word `writing` returned both. The Redis row came from `wm_fts` with score `0.166` and lexical score `0.2`; explain output showed no embedding was available and no metadata/domain filter was applied.

This is a concrete cross-topic lexical false positive in the generic engine API, but it is not evidence that the engine violated its session/global visibility rule. The current recall API has no semantic-domain predicate. A domain-aware change would need an explicit caller-supplied domain filter over stored metadata and a clear rule for beliefs with multiple domains. A vector top hit cannot safely supply that authorization decision. Whether the findings run's adapter applied the benchmark scope remains unknown.

## Long-query reproduction

The public fixture's `long-query-rambling-kubernetes` case is 400+ characters and expects `b-kubernetes-entity` in relevant beliefs, with `b-auth-open-q` handled separately as an open question. I replayed its exact text against the exact seed using the local exploratory probe's flattening. The 43 meaningful query tokens returned six rows: `b-kubernetes-entity` (`0.28008`, lexical `0.3093`), `b-auth-depends-redis` (`0.20528`, `0.1549`), `b-comm-pushback` (`0.1912`, `0.1012`), `b-auth-open-q` (`0.18888`, `0.1753`), `b-hosting-resolved` (`0.15936`, `0.0233`), and `b-chapter-structure-q` (`0.13016`, `0.0744`). The scratch engine had embeddings disabled, so this confirms the keyword path's over-return behavior for this flattened corpus. The open question's presence is expected in the benchmark's separate `openQuestions` output, so raw engine recall cannot be compared directly to only the `relevantBeliefs` assertion.

I also used a smaller six-memory corpus to isolate the candidate-threshold behavior. With embeddings disabled:

| Query | Meaningful tokens | Results |
| --- | ---: | ---: |
| `How do I deploy Kubernetes with Helm?` | 3 | 1, the correct Kubernetes memory |
| A 27-token rambling query mentioning Kubernetes, the novel, Redis, PostgreSQL, Prometheus, travel, and asking for the Helm recipe | 27 | 6, including all five distractors |

The short query returned the Kubernetes memory at score `0.47152` and lexical score `0.85`. The 27-token query returned it at `0.2616` and lexical score `0.4167`; unrelated results ranged from `0.1376` / `0.1327` down to `0.09824` / `0.037`.

The mechanism is explicit in the engine's working-memory scoring path. For queries with at least four tokens, it collects distinct query-token matches across every candidate row. If at least two query tokens match somewhere in the corpus, `broad_multi_hit_query` lowers each row's admission threshold from `_minimum_recall_relevance()` to `1 / token_count`. At 27 tokens, a row can pass at about `0.037` lexical relevance. This lets unrelated rows through when each matches one isolated word, even though the normal broad-query threshold is `0.3`.

This points to candidate admission, not a need for an independent query-length penalty. A correction should stop corpus-wide evidence from lowering every row's threshold. A `0.75` relative score floor removed all five distractors in the small query reproduction while preserving a separate two-topic Kubernetes/PostgreSQL query whose second correct result scored `0.835` of the first. This is a targeted regression check, not a calibrated production default. The full 77-case score should be rerun before making benchmark-wide accuracy claims.

## P3 watch list

The public fixture includes exact cases, but the original adapter's case output is still missing. I replayed its seed using the local exploratory probe's serialization into a temporary database. These are observations from the current keyword-only engine under that local flattening, not explanations of the report's aggregate category totals:

- **Relation expansion:** `relation-type-expands-participants` expects the relation belief, its `b-redis-code` participant, and `b-auth-open-q` routed separately. In my local flattened-seed replay, no participant edges were created, and raw recall includes the relation and open-question rows but not the participant. `relation-expansion-respects-scope-filter` returns `b-redis-writing` in that replay. These observations do not establish whether the original benchmark adapter created relation edges or forwarded scope, so the reported category cause remains unknown.
- **Counter-signal retrieval:** `negative-guidance-mongoose` returns the expected `b-mongo-raw-driver`, alongside two extra rows. `counter-signal-jest-surfaces-vitest` returns no rows in the keyword-only scratch replay even though `jest` is in the stored aliases. That is a verified miss in this configuration, but the reported run used embeddings and my local flattened replay may differ from its adapter; the two aggregate cases do not establish its cause or production impact.
- **Ranking stability:** the composition case ranks its expected belief first, while `ranking-orderedbefore-alias-beats-content` ranks `b-error-handling` above the expected alias result `b-functional-pipeline`. The fixture labels this as BM25 IDF behavior, which is a ranking expectation, not evidence of nondeterminism. The empty-query recency case cannot be tested by generic recall because pinned-fact ordering is a separate feature. No repeat traces establish an actual stability defect.
- **Type routing/open questions:** `type-isolation-open-question-not-in-relevant` returns the open-question row in generic recall, while the benchmark requires that row to appear only in a separate `openQuestions` field. The raw engine recall method has no output channels for pinned facts, active questions, or relevant beliefs. My local probe only stores pinned/type/resolution labels as metadata. The original adapter may have handled them separately; without it, this does not establish an adapter or engine fault.

The confirmed narrow findings are generic lexical cross-topic retrieval and keyword-only Jest counter-signal miss in the local scratch replay, plus long-query over-return for the flattened seed. Whether the findings run's adapter forwarded scope, created relation edges, or routed output types is unknown. Cause attribution for its aggregate scores remains uncertain without the original adapter and raw per-case output.

## Reproduction conditions

The probes used the installed 3.15.1 engine as read-only code, an explicit scratch database under a unique temporary directory, a separate temporary `HERMES_HOME`, and `MNEMOSYNE_EMBEDDINGS_OFF=1`. They did not touch the installed Hermes database, provider configuration, or virtual environment. The engine's `explain=True` output reported the candidate source, score, lexical score, filters, and embedding availability for each query.

`tests/test_engine_recall_scope.py` locks down the two useful retrieval contracts: the writing query drops the lexical code-domain candidate when the opt-in score floor is enabled; and a noisy multi-topic query sheds weaker candidates while an explicit two-topic query still returns both relevant rows. It does not claim that the metadata labels are engine authorization scopes.
