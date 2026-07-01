# Extractor Eval Report

- total_cases: 6
- operation_accuracy: 1.000
- memory_type_accuracy: 1.000
- target_memory_accuracy: 1.000
- hallucination_rate: 0.000

| case | category | ops | expected | ok |
| --- | --- | --- | --- | --- |
| explicit_async_extractor_preference | explicit | ADD | ADD | yes |
| negative_implicit_joke | negative_implicit | NOOP | NOOP | yes |
| update_from_related_memory | conflict_update | UPDATE | UPDATE | yes |
| multi_memory_current_best_merge | multi_memory | MERGE | MERGE | yes |
| relationship_signal_companionship | relationship_signal | ADD | ADD | yes |
| gradual_preference_no_overextract_single_turn | gradual_preference | NOOP | NOOP | yes |
