<!--
NODE: N61 Diagnose (W_head, programmatic; AGENTIC only on 'unclassified').
      Runs after N60 Score & Pareto-Admit. The rule table is tried first and
      handles the common cases; this prompt is the escalation path and must be
      invoked only when the table returns `unclassified`.

PURPOSE: Name the bottleneck for a measured design point that the programmatic
      rule table could not classify. Diagnosis is deliberately separated from
      proposal (N10): if the same model both diagnoses and proposes, the
      diagnosis degrades into post-hoc justification for the mutation it
      already wanted to make. This node names the bottleneck and stops -- it
      does not propose a lever or a mutation.

INPUTS: the Gemmini hardware counters for this design point; the rule table
      that failed to classify them, so the model can see which predicates were
      tested and missed. Per review 3.2, IN = "counters, rule table".
      The rules the table already covers, from 2.2(e), and which therefore
      cannot be the answer here:
        conflict_stalls/cycles > 0.15                 -> bank-conflict bound
        dma_idle < 0.2 AND mac_util < 0.4             -> load imbalance
        bytes_per_token flat AND cycles up            -> index-decode bound

OUTPUTS: a bottleneck label (review 3.2, OUT = "bottleneck label") -- a single
      typed, bounded string, not prose, since it is fed back to N10 over the
      typed N61 -> N10 edge whose size is deliberately bounded. If the counters
      genuinely do not identify a bottleneck, saying so is a valid label; an
      invented one is worse than none. Where the label generalises, it is a
      candidate new row for the rule table, which is the cheapest way to stop
      paying for an LLM call on this signature twice.

SOURCE: SparseCraft_Technical_Review.md 3.1 SPLIT (node 8 "Evaluator" -> N60
      Score & Pareto-Admit + N61 Diagnose); 2.2 row (e), which identifies the
      diagnose-and-propose conflict of interest and names this the largest
      single token-cost reduction available in the loop; 3.2 node table
      ("PROGRAMMATIC; AGENTIC only on 'unclassified'") and the N61 -> N10 edge.

STATUS: stub — body not yet written
-->
