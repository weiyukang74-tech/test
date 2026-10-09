# 病程记录关键词筛选 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让查询规划 Agent 只为标签已路由到病程记录的证据项生成高召回词根，并在查询程序中按规则级 OR 语义筛选完整病程，无命中或计划不完整时安全回退全量病程。

**Architecture:** 查询规划阶段从标签映射表识别 `recordType=病程记录` 的证据标签，在现有事实级查询计划中增量写入 `record_search_terms` 和 `medical_record_sources`；同一证据事实如果同时映射费用与病程，则合并成一条多源计划。查询程序在所有标签完成原始查询后执行规则级后处理：合并所有病程计划、匹配并去重文书、保留完整原文、记录命中元数据和回退原因。复核上下文对启用筛选的病程跳过公共部分提取，其他文书维持现状。

**Tech Stack:** Python 3、标准库 `re/json/unicodedata/unittest`、openpyxl、现有 OpenAI-compatible 模型客户端。

**Spec:** `docs/superpowers/specs/2026-09-22-medical-record-keyword-filter-design.md`

## Global Constraints

- 标签映射拥有绝对路由权；模型不得新增未映射的数据源。
- 仅过滤 `recordType=病程记录` 的文书。
- 模型只输出词根，程序负责正则转义、规范化和英文边界。
- 不同证据项和不同词根使用 OR 召回。
- 命中后保留整篇 `recordContent`。
- 无命中、计划缺失、计划无效或计划不完整时返回全部候选病程。
- 启用新筛选流程的病程不执行公共部分提取。
- 保持现有费用计划、旧查询规划文件和非病程数据源兼容。
- 不自动创建 Git 提交；只在用户明确要求时提交。

---

### Task 1: 标签映射与病程关键词计划

**Files:**
- Modify: `POC/查询规划Agent/query_planning_agent/mapping.py`
- Modify: `POC/查询规划Agent/query_planning_agent/prompt.py`
- Modify: `POC/查询规划Agent/query_planning_agent/service.py`
- Modify: `POC/查询规划Agent/query_planning_agent/cli.py`
- Create: `POC/查询规划Agent/tests/test_medical_record_planning.py`

**Interfaces:**
- Produces: `load_medical_record_sources(path: Path) -> dict[tuple[str, str], list[str]]`
- Produces: candidate fact field `medical_record_sources: list[str]`
- Produces: plan fields `record_search_terms: list[str]` and `medical_record_sources: list[str]`
- Preserves: expense plan `entities`, `search_fields`, `fallback_search_fields`, and `fallback_strategy`

- [ ] **Step 1: Write failing mapping tests**

Create a temporary workbook with `拆分映射` rows covering expense-only, progress-note-only, non-progress medical, and expense-plus-progress labels. Assert that only rows whose `recordType` equals `病程记录` appear in `load_medical_record_sources`, and that source labels are deduplicated in workbook order.

```python
def test_load_medical_record_sources_only_returns_progress_notes(self):
    sources = load_medical_record_sources(self.mapping_path)
    self.assertEqual(
        sources[("病理", "病理结果")],
        ["DS-M-IP-004 日常病程记录"],
    )
    self.assertNotIn(("患者", "生育状态"), sources)
```

- [ ] **Step 2: Run the mapping test and verify RED**

Run from `POC/查询规划Agent`:

```powershell
python -m unittest tests.test_medical_record_planning.MedicalRecordMappingTests -v
```

Expected: import or attribute failure because `load_medical_record_sources` does not exist.

- [ ] **Step 3: Implement the focused mapping loader**

Parse the same `拆分映射` worksheet used by the existing expense loader. Resolve the active source cell from `来源级别`, require `recordType == "病程记录"`, and return source labels keyed by `(一级标签名, 二级标签名)`.

- [ ] **Step 4: Run the mapping test and verify GREEN**

Run the same unittest command and expect all mapping tests to pass.

- [ ] **Step 5: Write failing query-planning tests**

Use a queue-backed fake model client. Cover:

```python
def test_only_mapped_progress_facts_receive_record_terms(self):
    facts = collect_rule_facts(
        self.rule,
        medical_record_sources={
            ("病理", "病理结果"): ["DS-M-IP-004 日常病程记录"]
        },
    )
    result = QueryPlanningService(self.client).plan(self.rule, facts)
    plan = result["plans"][0]
    self.assertEqual(plan["record_search_terms"], ["雌激素", "ER"])
    self.assertEqual(plan["medical_record_sources"], ["DS-M-IP-004 日常病程记录"])

def test_expense_and_progress_fields_share_one_fact_plan(self):
    result = QueryPlanningService(self.client).plan(self.rule, self.overlap_facts)
    self.assertEqual(len(result["plans"]), 1)
    self.assertTrue(result["plans"][0]["entities"])
    self.assertEqual(result["plans"][0]["record_search_terms"], ["他莫昔芬"])
```

Also assert terms are stripped, deduplicated, limited to five, and every mapped medical fact must be returned by the model.

- [ ] **Step 6: Run query-planning tests and verify RED**

```powershell
python -m unittest tests.test_medical_record_planning.QueryPlanningMedicalRecordTests -v
```

Expected: failures because facts do not carry medical sources and the service does not generate medical terms.

- [ ] **Step 7: Add the medical-record prompt and response validator**

Add a separate medical-record system prompt whose output is:

```json
{
  "rule_id": "RULE-056",
  "facts": [
    {
      "fact_id": "F03",
      "record_search_terms": ["雌激素", "ER"]
    }
  ]
}
```

The prompt must require 1–5 high-recall roots, prohibit raw regex and generic action words, and state that terms retrieve candidates rather than prove evidence.

- [ ] **Step 8: Merge expense and medical results by exact fact identity**

Keep one plan per `(proposition_id, 一级证据域, 二级证据标签, target_fact)`. Build expense fields and medical fields independently, then merge them into the same plan object when both apply. Use `query_type` values `STRUCTURED`, `MEDICAL_RECORD_KEYWORD_FILTER`, or `MULTI_SOURCE` according to populated fields.

- [ ] **Step 9: Pass mapping-derived sources from the CLI**

Load medical sources once from `--mapping`, pass them to `collect_rule_facts`, and leave `--plan-only` and downstream invocation unchanged.

- [ ] **Step 10: Run all query-planning tests**

```powershell
python -m unittest discover -s tests -v
```

Expected: all query-planning tests pass; existing expense planning behavior remains unchanged.

---

### Task 2: 安全病程词根匹配器

**Files:**
- Create: `POC/查询程序/evidence_retriever/medical_record_filter.py`
- Create: `POC/查询程序/tests/test_medical_record_filter.py`

**Interfaces:**
- Produces: `is_progress_record(record: dict[str, object]) -> bool`
- Produces: `match_record_content(record_content: object, terms: list[str]) -> list[str]`
- Produces: `filter_rule_medical_records(rule_result, plan_index, required_plan_keys) -> dict[str, Any]`

- [ ] **Step 1: Write failing term-matching tests**

Cover Chinese roots, case-insensitive English, nested dictionary content, escaped regex characters, and the required ER boundary behavior:

```python
def test_er_matches_clinical_forms_but_not_her2(self):
    self.assertEqual(match_record_content("ER+，HER2阴性", ["ER"]), ["ER"])
    self.assertEqual(match_record_content("仅记录HER2阳性", ["ER"]), [])
    self.assertEqual(match_record_content("ER（90%）", ["ER"]), ["ER"])
```

- [ ] **Step 2: Run matcher tests and verify RED**

Run from `POC/查询程序`:

```powershell
python -m unittest tests.test_medical_record_filter.MedicalRecordTermMatchingTests -v
```

Expected: module import failure because the matcher does not exist.

- [ ] **Step 3: Implement content flattening and safe matching**

Recursively flatten only values from string/list/dict content. Normalize with NFKC and case folding. For ASCII alphanumeric abbreviations, enforce boundaries that reject surrounding ASCII letters or digits; for Chinese or mixed terms, use escaped literal substring matching.

- [ ] **Step 4: Run matcher tests and verify GREEN**

Run the same matcher command and expect all cases to pass.

- [ ] **Step 5: Write failing rule-level filter tests**

Construct a rule result with duplicated progress documents across two evidence entries plus an admission record. Assert:

- medical terms from different plans use OR;
- matching progress documents are retained with full content;
- duplicate candidates count once in statistics;
- non-progress documents remain untouched;
- each selected progress document records matched propositions, labels, and terms;
- zero matches return all candidates with `NO_KEYWORD_MATCH`;
- a missing or invalid plan returns all candidates with `INCOMPLETE_FILTER_PLAN`.

- [ ] **Step 6: Run rule-level filter tests and verify RED**

```powershell
python -m unittest tests.test_medical_record_filter.MedicalRecordRuleFilterTests -v
```

Expected: failures because rule-level filtering is not implemented.

- [ ] **Step 7: Implement rule-level OR filtering and conservative fallback**

Collect unique progress candidates from every evidence item, validate an exact medical plan for every required key, match each unique document against every plan, and post-filter each evidence data array using selected document identities. Store rule-level statistics under `medical_record_filter` and private per-record match metadata for the context builder.

- [ ] **Step 8: Run all filter tests**

```powershell
python -m unittest tests.test_medical_record_filter -v
```

Expected: all matcher, OR, deduplication, metadata, and fallback tests pass.

---

### Task 3: 查询流程与复核上下文集成

**Files:**
- Modify: `POC/查询程序/evidence_retriever/planning.py`
- Modify: `POC/查询程序/evidence_retriever/service.py`
- Modify: `POC/查询程序/evidence_retriever/context_builder.py`
- Modify: `POC/查询程序/tests/test_planning.py`
- Modify: `POC/查询程序/tests/test_retriever.py`

**Interfaces:**
- Produces: exact medical plan lookup without same-proposition inheritance
- Consumes: `filter_rule_medical_records(...)`
- Produces: review-context `medical_record_filter` and document match metadata

- [ ] **Step 1: Write failing planning compatibility tests**

Assert that expense resolution still inherits usable entities, while medical filtering uses only an exact fact plan and never inherits another label's `record_search_terms`.

- [ ] **Step 2: Run planning tests and verify RED**

```powershell
python -m unittest tests.test_planning -v
```

Expected: the new exact medical-plan API is absent.

- [ ] **Step 3: Add exact medical plan validation**

Add a helper that returns a plan only when it exactly matches the fact key and has 1–5 valid `record_search_terms`. Do not change the existing expense `resolve_query_plan` inheritance behavior.

- [ ] **Step 4: Write failing retrieval integration tests**

Use synthetic route data or patch the in-memory medical dataset so a rule has:

- one progress document containing `ER+`;
- one containing only `HER2`;
- one unrelated progress document;
- one non-progress medical document.

Assert the final query result retains the `ER+` progress document and the non-progress document, filters the other progress documents, and emits counts and matched terms. Add separate tests for zero-match and incomplete-plan fallback.

- [ ] **Step 5: Run retrieval integration tests and verify RED**

```powershell
python -m unittest tests.test_retriever.RetrievalServiceTests -v
```

Expected: progress records remain unfiltered and no filter statistics exist.

- [ ] **Step 6: Integrate the rule-level post-filter into RetrievalService.run**

For each selected rule, collect exact keys for included tags whose mapped routes contain `record_type == "病程记录"`. Retrieve all evidence through the existing route logic, then call the rule-level filter once before adding the rule result to the output. Ensure medical-only plans never trigger insertion of the synthetic expense route.

- [ ] **Step 7: Write failing context-builder tests**

Assert that a filtered progress document is stored once with full `recordContent` plus `matched_propositions`, `matched_evidence_tags`, and `matched_terms`; it must not appear inside `medical_document_groups`. Assert non-progress documents continue through the existing grouping behavior and old query results without `medical_record_filter` retain legacy behavior.

- [ ] **Step 8: Run context tests and verify RED**

```powershell
python -m unittest tests.test_retriever.RetrievalServiceTests.test_review_context_preserves_filtered_progress_documents -v
```

Expected: context builder groups the progress record and omits match metadata.

- [ ] **Step 9: Preserve selected progress documents in the context builder**

Copy filter match metadata into `MedicalDocumentCollector`. When `medical_record_filter.enabled` is true, split collected documents by `recordType`: return progress documents raw, pass only non-progress documents to `group_medical_documents`, and attach rule-level filter statistics to the context.

- [ ] **Step 10: Run the complete query-program suite**

```powershell
python -m unittest discover -s tests -v
```

Expected: all existing and new tests pass.

---

### Task 4: 文档与全链路验证

**Files:**
- Modify: `POC/查询规划Agent/README.md`
- Modify: `POC/查询程序/README.md`
- Create: `POC/对比验证/病程关键词筛选/` runtime outputs only when corresponding rule data is available

**Interfaces:**
- Documents: new plan fields, fallback semantics, full-document behavior, and inspection locations
- Verifies: `RULE-056` when its rule evidence and patient data are available

- [ ] **Step 1: Update operational documentation**

Document that label mapping controls eligibility, `record_search_terms` are retrieval roots rather than proof, matching is rule-level OR, full records are retained, and fallback reasons are visible in `medical_record_filter`.

- [ ] **Step 2: Run focused static validation**

```powershell
python -m compileall query_planning_agent
python -m unittest discover -s tests -v
```

Run separately in both program directories and expect no compilation or test failures.

- [ ] **Step 3: Generate a plan-only result for an available rule**

Run the query-planning CLI with an available rule whose tags map to progress notes. Inspect that only mapped medical facts have `record_search_terms`, and overlapping expense-plus-progress facts contain both `entities` and medical fields in one plan.

- [ ] **Step 4: Run end-to-end retrieval on available local data**

Inspect the generated query result and review context for:

- candidate and selected document counts;
- exact matched terms and evidence tags;
- full `recordContent` preservation;
- no progress document in `medical_document_groups`;
- correct fallback status when applicable.

- [ ] **Step 5: Validate RULE-056 when its inputs exist**

Confirm `ER+` and Chinese estrogen-receptor expressions are recalled, `HER2` alone does not match `ER`, negative ER statements remain recallable, and tamoxifen/menopause/progression terms remain attached to their own labels. If `RULE-056` rule evidence or case data is absent, report that external input gap and retain deterministic synthetic coverage.

- [ ] **Step 6: Produce A/B statistics**

For multiple high-volume rules, compare unfiltered versus filtered candidate counts, selected counts, fallback rate, context characters, and reference tokens. Treat any missing known key evidence as a release blocker regardless of Token savings.

- [ ] **Step 7: Final regression and worktree review**

```powershell
git diff --check
git status --short --branch
```

Confirm only intended source, test, documentation, design, plan, and validation-output files changed. Do not stage or commit unless requested.
