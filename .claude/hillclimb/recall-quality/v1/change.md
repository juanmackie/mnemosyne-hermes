OR the query's content tokens instead of AND-ing every token

`_fts_match_expression` emitted every whitespace-separated token as a quoted
phrase, space-joined. Space-joined FTS5 terms are an implicit AND, so a question
only matched if the corpus contained every one of its words: "what's my email
address again?" needed `what`, `what's` and `again` as well as `email`. Every
natural-language query returned empty - hit@5 0.000 across all 81 positive cases.

Terms are now OR-ed, and stopwords are dropped first. Both halves matter, and the
screen separates them: OR over *all* tokens reaches 0.852 but drops negative
specificity to 0.200, because "my", "i" and "what" occur in a large share of
memories; OR over content tokens reaches 0.926 at 0.600.

Tokens are split with `[^\W_]+` rather than `query.split()`, so "hermes-dashboard"
becomes the two terms the tokenizer actually sees.

Costs recorded, not hidden: negative specificity falls 1.000 -> 0.600 on train and
no-junk@5 falls to 0.111, i.e. this trades over-retrieval for recall. Recovering
that without giving the recall back is the next round's job.
