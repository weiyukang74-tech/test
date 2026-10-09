# 规则证据 Agent（POC）

本 Agent 使用模型调用完成：

1. 理解医保违规描述；
2. 形成监管主张、申诉路径和申诉目标；
3. 生成申诉命题；
4. 为命题匹配必需证据标签和补充证据标签；
5. 输出证据任务和程序核验项。

只输出一份结果：

```text
D:\AI\AI_S2\POC\规则证据结果\规则证据结果.json
```

不会生成查询请求 JSON，也不会执行医院文书查询。

默认输入为 `项目汇聚\datat\南山人医-数据导出\住院负面清单_处理.xlsx` 的“关闭明细”工作表。程序按“违规内容”非空记录动态读取，不写死记录条数；也可以通过 `--input` 指定其他支持的负面清单文件。

## 标签目录

标签目录位于 `标签目录.xlsx`。程序只使用：

```text
一级证据域
二级证据标签
```

结果中的标签只包含一级证据域、二级证据标签、`target_fact` 和 `necessity`，不输出主要来源和次要来源。

## 运行

```powershell
cd D:\AI\AI_S2\POC\规则证据Agent
C:\Users\weyk\anaconda3\python.exe -m rule_evidence_agent --env-file .env
```

只校验输入：

```powershell
C:\Users\weyk\anaconda3\python.exe -m rule_evidence_agent --dry-run
```

只处理前 N 条：

```powershell
C:\Users\weyk\anaconda3\python.exe -m rule_evidence_agent --env-file .env --limit 1
```

只处理按有效规则顺序计数的第 N 条：

```powershell
C:\Users\weyk\anaconda3\python.exe -m rule_evidence_agent --env-file .env --rule-number 36
```

医院导出格式中，程序会把每条规则的 `mdtrt_id`、`medins_id` 和 `住院门诊号` 写入汇总结果；`mdtrt_id` 为空的负面清单行会跳过。

已有 `rule_id` 默认跳过，不覆盖。

## 测试

```powershell
C:\Users\weyk\anaconda3\python.exe -m unittest discover -s tests -v
```
