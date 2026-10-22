# System prompt policy

A sentence may enter a system prompt only if it would apply, unchanged, to at least
three unrelated domains. A named-case instruction fails this test. A general rule
to verify technical procedures against specialist sources applies equally to
photography, manufacturing and cooking. Reviewers must record three applications
for each new behavioural instruction before accepting it.

When a corpus case fails, the fix is either a generalised behaviour that passes
this test, a tool or data fix, or an honest note that the model cannot do it.
Never the case itself. Never hide case instructions in another prompt or runtime
system message. No prompt may name public cases, their distinctive vocabulary,
reference answers or desired scores. Preserve useful-content assertions.

The corpus in `/opt/atlas-src/corpus` is the public evaluation set, used for
iteration and known to be seen by the agent. Its cases may be quoted in journeys
and reports, never in prompts. Public scores are development signals, not a
measurement of generalisation. Only the owner performs the independent private
assessment. The agent neither inspects nor uses that assessment's cases.

The chat prompt's ordered layers are disposition, capabilities/tools,
language/form, safety/evidence. The disposition is the verbatim owner text.
Capabilities describe actual operations and budgets; no case-specific exception.
Tests enforce size, verbatim text and banned corpus vocabulary. Review enforces
the semantic three-domain test: lexical checks alone cannot prove generality.

M25 review: checking precedents applies to institutional design, engineering and
education; testing source claims applies to history, economics and physics;
checking procedures and units applies to cooking, medicine and manufacturing;
source distrust/privacy applies to all three. No evaluation case is embedded.

Evidence reconciliation review: identifying what each number measures before
comparing it applies unchanged to clinical rates (populations), climate records
(measurement periods) and education statistics (denominators). Explain relevant
distinctions in the answer rather than silently discarding a different measure.

Numerical reconciliation names each measure in the resolved language and explains
definitions, stages and disagreements. The identical rule applies to clinical
incidence versus prevalence, climate baselines and educational cohort outcomes.
It adds no domain names to the prompt and does not prescribe a case answer.

Translation consistency: resolved answer language governs prose and headings;
source-language duplication requires a bilingual request. This applies unchanged
to a workplace policy, a recipe and a museum description.

The same prohibition covers JSON/YAML prompt assets and automatic query rewriting.
Named reference-site templates and benchmark-specific query additions are removed;
automatic discovery preserves the user's question. Technical quantities and explicit
identifiers trigger discovery across device specifications, medical timing and
industrial process durations. Demo inputs live under tests/journeys, not prompts.

Finalization comparisons of quantities, workload and boundary conditions apply
unchanged to manufacturing, transport and energy. Domain-native notation applies
to mathematics, music and software without prescribing any corpus-specific format.
Finalization repeats only the latest user's text, preserving full tool evidence as
untrusted data; this addresses a transport-envelope continuation observed in live
research, and applies to product research, historical sources and lab measurements.


Quantitative discovery must check ambiguous definitions or stages, including
predicted versus observed values. This applies unchanged to clinical outcomes,
manufacturing tolerances and education targets; it names no corpus subject,
prescribes no reference figure and preserves the original user's query.

Temporal evidence review: checking whether a past prediction actually materialized applies unchanged to an election forecast, a scheduled satellite launch and a planned clinical trial. Consultation timestamps establish the present; an old announcement is not an observed outcome.

## First documented patched-score trade-off

The owner accepted J34's quality regression on2026-09-21: removing its case
patch lost the distinction between offer and first-market price in three
fresh captures. It remains FAILING in reports/M25.md. This is the first
documented trade-off between a patched public score and a general behaviour.
The general evidence-reconciliation rule remains; the case patch does not return.

Document claims carry the retrieved chunk identifier so users can resolve the
source. This applies unchanged to policies, technical manuals and historical archives.

Terse work requests: infer a reasonable deliverable from context, provide it and
briefly state necessary assumptions. This applies unchanged to an educational
activity, a purchasing comparison and an engineering procedure. No named API or
corpus question is prescribed.

Budget finalization is conditional on actual tool evidence. Before a tool result,
keep the user's work request and disposition intact; do not recast it as a report
on research already performed. Tool disabling and monetary reservations remain
in force. This applies unchanged to educational checklists, business letters and
industrial procedures.

Owner addition (2026-09-21): match the answer to the question's size. Direct bounded
requests receive their deliverable immediately; open design questions warrant
precedents. This applies unchanged to code, translation and calculation. The
verbatim disposition regression includes the authorized paragraph from MISSION.

### Source availability (2026-09-21)
Analysis uses existing evidence. Missing input is requested; permission to select a
public source permits retrieval, not fabrication. Original material is created when
requested. The same rule applies to contracts, experimental records and accounting.
A live public request exposed fabrication of a missing source before a command-size
refusal. The command ceiling remains unchanged; the correction addresses evidence.

Delegated source selection is performed within the user's constraints rather than
returned to the user. An absent upload does not cancel authorization to choose a
public source. This applies equally to legal, experimental and financial sources.

### Request completeness and source identity (2026-09-21)
Complete all stages in the current request, in order; history remains context.
Confirm that retrieved facts concern the exact subject before attributing them.
A similarly named subject is not interchangeable. A delegated public source must
be read with the available search/fetch tools, not merely recalled. These rules
apply unchanged to historical people, industrial products and scientific studies.
All owner disposition paragraphs remain verbatim. No corpus term is added.

Explicit scenario premises govern conditional calculations; checking their external
validity is a separate task. This rule applies unchanged to energy forecasts,
logistics estimates and population simulations. It prevents old training memory
from replacing the user's assumptions, while retaining research for missing facts
and requested verification. Public measurement must establish its actual effect.

### Owner certification scope (2026-09-21)
The owner certifies M25 on the public Qwen and GLM profiles, retaining their
6.0 and 5.1 thresholds. DeepSeek's measured regression is a finding, not a
milestone failure. Its scores must still be complete and valid; the exemption
does not invent scores or excuse technical errors. No per-model disposition
or case patch is introduced. The longer disposition's suitability for smaller
models remains a later research question.
