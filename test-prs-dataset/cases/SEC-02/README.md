# SEC-02 oracle

The pre-image defines an `orders` table with an `account_id` tenant key. The proposed Python module interpolates a caller-provided account ID directly into a SQL string on added line 6. With rows belonging to two accounts, the value `acct_a' OR '1'='1` produces a predicate that returns both accounts' orders. The vulnerability is SQL injection with cross-account data exposure, so the single ground-truth finding is `security` / `critical` and the derived verdict is `blocking`.

The pre-image and query are synthetic. They contain no working credentials or borrowed source. The changed file has no comments that disclose the expected review finding.
