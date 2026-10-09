# 查询规划Agent

读取规则证据 JSON 和标签映射表，使用与规则证据 Agent 相同的 Qwen3.6 配置，为映射到费用明细或检验报告视图的标签生成查询实体，并为映射到 `recordType=病程记录` 的具体证据标签生成病程检索词根。规划完成后默认自动启动查询程序。

标签映射拥有数据源路由权。查询规划 Agent 只决定如何检索，不会为未映射到病程记录的证据点主动增加病程查询。

病程计划包含：

- `record_search_terms`：1 至 5 个高召回词根，不是证据结论；按医生在病程中的写法生成，治疗或操作类事实要包含所属治疗类别或执行科室的常见写法；不能直接使用二级证据标签名称（如“治疗方案”），也不能是单个字符或纯数字，否则按校验失败自动修正一次；
- `medical_record_sources`：标签映射表已经确定的病程来源；
- `query_type`：病程专用计划为 `MEDICAL_RECORD_KEYWORD_FILTER`，同时包含费用和病程计划时为 `MULTI_SOURCE`。

例如“雌激素受体阳性”可以生成 `雌激素` 和 `ER`。程序用这些词根召回原文，再由复核 Agent 判断阳性、阴性、否定和患者归属。
费用明细按收费项目字段检索，检验报告按 `itemName` 和 `indexName` 检索。

当规则描述涉及同时收费、重复收费、互斥收费或收费时间比较，但规则证据JSON未选择费用标签时，规划Agent会保持原JSON不变，自动增加费用明细查询计划，并标记为 `AUTO_SUPPLEMENTED`。

## 病程原文关键词反馈

某个病程证据点的词根在候选病程里命中的篇数不超过1篇时，程序会把候选病程的真实片段（按文书类型分配篇数、类型内按时间均匀取、轮流取文书的前中后段，最多一万五千字符）发给模型补充词根，并把结果记录在该证据点的 `record_search_retry` 中。

- 命中0篇或1篇都会触发，每个证据点最多补充3个词根，每条规则最多3个证据点；命中超过1篇的证据点不触发，原词根保留。
- 补充的词根必须能在候选病程里真正命中，命不中的、证据标签名称、单个字或纯数字都会被丢弃并记录在 `rejected_terms`。
- 原词根与新词根取并集；并集超过5个时，优先保留有命中的补充词根，其次是有命中的原词根，没命中的原词根最后舍弃并记录在 `replaced_terms`。
- 模型在片段里找不到对应写法时返回空数组，`added_terms` 为空并在 `note` 中说明，此时词根保持不变，查询程序按命中不足安全回退。
- 预检或模型调用失败只跳过反馈，不影响正常规划。

用 `--no-record-feedback` 可以关闭这轮反馈；数据目录用 `--medical-data` 指定，默认与查询程序一致。

运行时会输出查询规划模型的首Token、Token/s、模型用时和输出Token，并在查询完成后输出查询程序用时与全链路总用时。

查询规划Agent使用 `QUERY_PLANNING_MODEL_TIMEOUT_SECONDS` 设置独立超时时间。每次请求失败时会立即输出请求次数、失败阶段、耗时、异常类型、HTTP状态或底层原因，以及是否继续重试。

默认批量处理全部规则：

```powershell
cd D:\AI\AI_S2\项目汇聚\POC\查询规划Agent
C:\Users\weyk\anaconda3\python.exe -m query_planning_agent
```

仅处理单条规则：

```powershell
C:\Users\weyk\anaconda3\python.exe -m query_planning_agent --rule-id RULE-002
```

只生成规划、不执行查询：

```powershell
C:\Users\weyk\anaconda3\python.exe -m query_planning_agent --rule-id RULE-002 --plan-only
```

查询规划输出位于 `D:\AI\AI_S2\项目汇聚\POC\查询规划结果`，随后生成查询结果和复核上下文。查询规划Agent与查询程序仍是两个独立目录，可以分别修改和单独运行。
