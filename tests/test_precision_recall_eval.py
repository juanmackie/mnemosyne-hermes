import unittest

from bench.precision_recall_eval import apply_relative_cutoff, evaluate, score_case


class PrecisionRecallEvalTests(unittest.TestCase):
    def test_relative_cutoff_preserves_order_and_reports_removed_expected_ids(self):
        case = {
            "caseId": "retrieval",
            "category": "Alias resolution",
            "expect": {"relevantBeliefs": {"shouldOnlyInclude": ["right"]}},
        }
        result = {
            "caseId": "retrieval",
            "ranked": [
                {"id": "right", "score": 1.0},
                {"id": "neighbor", "score": 0.45},
            ],
        }
        selected = apply_relative_cutoff(result["ranked"], 0.5)

        scored = score_case(case, result, selected, set())

        self.assertEqual([row["id"] for row in selected], ["right"])
        self.assertTrue(scored["passed"])
        self.assertEqual(scored["removedRelevant"], [])
        self.assertEqual(scored["retrievalRecall"], 1.0)

    def test_cutoff_can_remove_expected_belief_and_fail_required_inclusion(self):
        case = {
            "caseId": "retrieval",
            "category": "Alias resolution",
            "expect": {"relevantBeliefs": {"shouldOnlyInclude": ["right"]}},
        }
        result = {
            "caseId": "retrieval",
            "ranked": [
                {"id": "neighbor", "score": 1.0},
                {"id": "right", "score": 0.4},
            ],
        }

        scored = score_case(case, result, apply_relative_cutoff(result["ranked"], 0.5), set())

        self.assertFalse(scored["passed"])
        self.assertEqual(scored["missingRelevant"], ["right"])
        self.assertEqual(scored["retrievalRecall"], 0.0)

    def test_pinned_belief_satisfies_union_required_but_not_relevant_only_assertion(self):
        case = {
            "caseId": "pinned",
            "category": "Type routing and open questions",
            "expect": {"relevantBeliefs": {"mustInclude": ["pinned"]}},
        }
        result = {"caseId": "pinned", "ranked": [], "pinnedBeliefs": ["pinned"]}

        scored = score_case(case, result, [], {"pinned"})

        self.assertTrue(scored["passed"])
        self.assertIsNone(scored["retrievalPrecision"])
        self.assertEqual(scored["expectedRelevant"], [])

    def test_should_only_include_requires_exact_relevant_set(self):
        case = {
            "caseId": "exact",
            "category": "Scope disambiguation",
            "expect": {"relevantBeliefs": {"shouldOnlyInclude": ["a", "b"]}},
        }
        result = {"caseId": "exact", "ranked": []}

        scored = score_case(case, result, [], set())

        self.assertEqual(scored["missingRelevant"], ["a", "b"])
        self.assertEqual(len(scored["failures"]), 2)

    def test_cutoff_uses_maximum_score_without_reordering_results(self):
        ranked = [
            {"id": "lower-ranked-first", "score": 0.5},
            {"id": "score-maximum", "score": 1.0},
            {"id": "tie", "score": 1.0},
        ]

        selected = apply_relative_cutoff(ranked, 0.75)

        self.assertEqual([row["id"] for row in selected], ["score-maximum", "tie"])

    def test_nonpositive_scores_are_left_unchanged(self):
        ranked = [{"id": "first", "score": -0.5}, {"id": "second", "score": -1.0}]

        self.assertEqual(apply_relative_cutoff(ranked, 0.5), ranked)

    def test_evaluate_requires_results_for_every_case(self):
        case = [{"caseId": "one", "category": "Scope disambiguation", "expect": {}}]

        with self.assertRaisesRegex(ValueError, "case rows do not match"):
            evaluate(case, [], {}, [0.0])

    def test_engine_variant_scores_actual_rows_after_user_filter(self):
        case = {
            "caseId": "scoped",
            "category": "Scope disambiguation",
            "userId": "alice",
            "expect": {"relevantBeliefs": {"shouldOnlyInclude": ["right"]}},
        }
        baseline = {
            "scoped": {
                "caseId": "scoped",
                "ranked": [{"id": "right", "score": 0.6}],
                "pinnedBeliefs": [],
            }
        }
        beliefs = [
            {"_id": "right", "user_id": "alice"},
            {"_id": "foreign", "user_id": "bob"},
        ]
        actual_gated_rows = {0.7: {"scoped": [{"id": "foreign", "score": 1.0}]}}

        report = evaluate([case], beliefs, baseline, [0.7], actual_gated_rows)

        self.assertEqual(report["variants"][0]["summary"]["totalPasses"], 0)
        self.assertEqual(report["variants"][0]["cases"][0]["relevantBeliefs"], [])
        self.assertEqual(report["variants"][0]["cases"][0]["missingRelevant"], ["right"])


if __name__ == "__main__":
    unittest.main()
