# E1/E2/E3 generation kit — the ~80 core cases on YOUR documents

To be run by an agent (ideally from a model family ≠ Qwen/GLM/DeepSeek) at
milestone M2, on 10–15 representative documents from the PoC corpus. Output: three YAML
files in the format below, with `genere-a-valider` status. **No case becomes `valide`
without human review** (checklist at the end of this document, ~1 min/case).

## Quotas

| Suite | Cases | Distribution |
|---|---|---|
| E1 retrieval | 40 | ≥ 6 per language; ≥ 8 **cross-lingual** cases (question in a language ≠ document language); ≥ 5 “multi-document” cases (the answer requires 2 sources) |
| E2 end-to-end RAG | 30 | ≥ 5 per language; mix of simple facts / synthesis / comparison |
| E3 honest refusal | 10 | 2 per language; **plausible** questions with no answer in the corpus |

## Generation prompt (adapt one document at a time)

```
You generate evaluation cases for a RAG system. Here is a corpus document:

<document id="{doc_id}" lang="{lang}">
{contenu}
</document>

Generate {n} cases in the YAML format below. STRICT rules:
1. The expected answer must be ENTIRELY contained in the document — cite the
   exact passage (`passage_source` field). If you cannot paste the passage, do not
   generate the case.
2. Vary difficulty: 40% direct reading, 40% rephrasing/synthesis of a
   section, 20% requiring cross-reference of two document passages.
3. Questions must resemble what an employee would actually ask (natural,
   sometimes imprecise), not exam questions.
4. {k} questions must be asked in a language OTHER than that of the document
   (state the question's `lang`).
5. Use NO knowledge external to the document.

Format:
- id: E1-XXX
  lang: <langue de la question>
  doc_lang: {lang}
  source: genere-a-valider
  statut: draft
  input: "<question>"
  attendu:
    doc_id: {doc_id}
    passage_source: "<citation exacte du document>"
    doit_contenir: ["<élément clé 1>", "<élément clé 2>"]
```

For **E3**, use the reverse prompt: “generate questions that an employee could
naturally ask about this topic but whose answer DOES NOT APPEAR in the document
or, to your knowledge, in the other listed documents: {list of titles}”.
Expected: `"report the absence of information; zero invention"`.

## Human validation checklist (per case, ~1 minute)

- [ ] The question is natural (a colleague could ask it as written).
- [ ] The `passage_source` does exist in the document and is sufficient to answer.
- [ ] The `doit_contenir` fields are the right discriminating elements (neither too vague — a
      false positive would pass — nor too verbose — a good reworded answer would fail).
- [ ] For E3: I have verified that the answer really appears nowhere in the corpus.
- [ ] Change `statut: valide` + `valide_par: <name>` + `date_validation`.

## Known pitfalls to reject during validation

- Questions that copy the document’s exact wording (trivial retrieval —
  any BM25 succeeds; it measures nothing).
- Expected answers that depend on interpretation (the evals require binary outcomes).
- E3 cases that are “too easy” (an absurd question): refusal must be tested on
  credible questions.
- Over-representation of a single document (max 5 cases per document).
