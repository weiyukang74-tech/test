# 查询程序（POC）

本程序根据规则证据 JSON 中的一、二级标签读取确认后的关系映射表，再从 `datat/南山人医-数据导出` 下的视图 Excel 中取得结构化数据或病历原文。程序优先使用规则证据结果中的 `mdtrt_id`，不再使用写死的就诊号；仍兼容传入旧版 `脱敏病历.md` 文件。

当前规则：

1. 结构化数据先按 `medinsId + mdtrtId` 查询；
2. 诊断信息根据 `target_fact` 生成检索词，再对 `diseName` 做包含匹配，检索词不写死；
3. 患者信息只返回标签对应的字段；
4. 检验报告按查询规划中的关键词匹配 `itemName` 或 `indexName`，返回检验结果、单位、参考范围和结果状态等字段；
5. 病历先按 `medinsId + mdtrtId + recordName/recordType` 查询。recordName 按映射表的“匹配方式”执行：标为 `recordName包含` 的按名称包含匹配（如带医师姓名的“XX主任医师查房记录”），其余按名称完全相等；recordType 中用“；”分隔的多个类型（如“病程记录；手术相关”）命中任意一个即可；当查询规划为全部病程证据项提供了有效的 `record_search_terms` 时，只筛选 `recordType=病程记录` 的文书，不同证据项和不同词根采用 OR 召回，任一证据项的词根都可以命中本规则取回的全部候选病程，不限于它自己映射的病程来源；等于病程证据项二级证据标签名称（如“治疗方案”）的词根和“患者”“检查”等无区分度词语不参与匹配；
6. 命中的病程记录完整保留 `recordContent`，不再提取公共部分；入院记录、出院记录、手术记录等非病程文书保持原有处理；
7. 病程整体零命中、任一必需证据计划零命中、计划缺失、计划无效或计划不完整时，回退返回全部候选病程，并在 `medical_record_filter` 中记录原因；补充证据计划零命中时不回退整条规则，但保留该证据项自身的全部候选病程；
8. 首先执行主要来源；
9. 默认查询主次全部来源，主要来源有数据时也保留次要来源；`--secondary-mode auto` 与 `always` 均执行全部来源，`deferred` 显式仅查询主要来源；
10. 查询程序不判断医学事实和申诉命题，关键词命中不代表证据成立。

## 运行

```powershell
 cd D:\AI\AI_S2\项目汇聚\POC\查询程序
C:\Users\weyk\anaconda3\python.exe -m evidence_retriever
```

不指定 `--rule-id` 时，程序会读取规则证据结果中的全部 `rule_id`，逐条使用各自的 `mdtrt_id` 查询。每条规则分别输出 `查询结果_RULE-001.json` 和 `复核上下文_RULE-001.json`，并生成 `查询结果索引.json`。

只测试 `P01`：

```powershell
C:\Users\weyk\anaconda3\python.exe -m evidence_retriever --proposition-id P01
```

指定单条规则时：

```powershell
C:\Users\weyk\anaconda3\python.exe -m evidence_retriever `
  --rule-evidence "D:\AI\AI_S2\项目汇聚\POC\规则证据结果\规则证据结果.json" `
  --mapping "D:\AI\AI_S2\项目汇聚\POC\标签视图映射\规则证据标签关系映射表_完整版.xlsx" `
  --rule-id RULE-001 `
  --output "D:\AI\AI_S2\项目汇聚\POC\查询结果\查询结果.json" `
  --review-context-output "D:\AI\AI_S2\项目汇聚\POC\查询结果\复核上下文.json"
```

## 输出结构

输出按 `rule_id → proposition_id → evidence` 分组。每个标签只保留：

- 一级证据域；
- 二级证据标签；
- `target_fact`；
- 查询状态、来源和数据。

诊断结果中的 `matched_by.value_source` 固定标明为 `target_fact`。病历结果中的 `recordContent` 是查询到的完整原文。启用病程关键词筛选后，选中的病程保留在 `medical_documents`，并提供 `matched_propositions`、`matched_evidence_tags` 和 `matched_terms`；这些病程不会进入 `medical_document_groups`。被其他证据项词根命中的病程仍挂在取回它的证据项下，由 `matched_evidence_tags` 标明是哪个证据项的词根命中的。

规则级 `medical_record_filter` 提供过滤前候选数、过滤后选中数、计划证据数、实际命中证据数、必需证据覆盖数、是否真正执行过滤、是否回退和回退原因。`NO_KEYWORD_MATCH` 表示没有词根命中，`INCOMPLETE_FILTER_PLAN` 表示至少一个需要病程的证据项缺少有效计划，`INCOMPLETE_FILTER_COVERAGE` 表示至少一个必需证据计划零命中；这些情况都会安全返回全部候选病程。计划缺失、无效或不完整时 `enabled=false`，旧版公共部分处理保持不变。`ignored_search_terms` 列出被忽略的标签名称和无区分度词根；词根全部被忽略的计划按无效计划处理。`supplementary_evidence_fallbacks` 列出零命中后保留全部候选病程的补充证据项，这些病程不带命中字段。两个字段只在非空时输出。

查询结果写入完成后，程序会继续生成复核上下文。复核上下文以命题为中心合并规则判断依据与查询结果。重复结构化记录只在 `structured_data_pool` 中保存一次，并通过 `structured_data_refs` 引用；重复病历原文只在 `medical_documents` 中保存一次，并通过 `document_refs` 引用。

查询结果通过每条数据的 `_query_sources` 标注来源及 `source_level`（`PRIMARY`/`SECONDARY`）。生成上下文时不把这些元数据混入临床字段，而是保存每个事实的 `query_result.source_groups`，分组包含来源级别、查询状态及对应文书/结构化记录/费用摘要引用。主次来源属于事实与来源的关联，同一文书可被不同事实按不同级别引用。上下文顶层 `source_staging=true` 启用先主要、未合规再主次全部的分阶段复核。需重新运行查询程序生成带级别的新上下文；原有病例文件不会自动改写。

## 测试

```powershell
C:\Users\weyk\anaconda3\python.exe -m unittest discover -s tests -v
```
# 查询规划文件

查询程序默认读取 `D:\AI\AI_S2\项目汇聚\POC\查询规划结果\查询规划结果_RULE-XXX.json`。
如果存在对应规划，费用明细将按 `chrg_type` 和名称检索词过滤，检验报告将按 `itemName` 或 `indexName` 任一字段和关键词组过滤，不拼接两个字段。病程记录按事实级 `record_search_terms` 做规则级 OR 筛选，命中文书保留完整原文；没有可安全执行的完整筛选计划时回退全量病程。检验结果按证据所需字段返回。
如果当前事实没有专属费用查询计划，程序优先继承同一申诉命题中其他事实的争议实体。若同一命题也没有可继承实体，则标记 `QUERY_SCOPE_UNRESOLVED`，费用明细返回空数组，不再默认全量返回。

费用明细查询结果始终保留全量原始记录。构建复核上下文时，若同一费用项目记录达到摘要阈值且跨多个日期，程序按标准病历视图字典生成摘要，分别提供去重后的实际发生天数 `distinct_service_days` 和 `cnt`累计次数 `total_service_times`。只有全部字段完全相同的记录才计为技术性重复，不删除业务记录。
