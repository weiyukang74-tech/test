# 医保基金智能监管 · 智能复核 POC

把负面清单里的每条违规规则，拆成可核查的证据点，自动到病历和结构化数据里取证，最后给出复核结论。POC 全流程分四步：

| 步骤 | 目录 | 作用 | 调用模型 |
|---|---|---|---|
| 1. 规则拆解 | `POC/规则证据Agent` | 读负面清单，输出申诉命题（P01、P02…）、证据标签和 `target_fact` | 是 |
| 2. 查询规划 | `POC/查询规划Agent` | 按标签映射为每个证据点生成检索词（费用、检验、病程），跑完自动调用查询程序 | 是 |
| 3. 证据查询 | `POC/查询程序` | 按标签映射到六视图和病历文书取数，筛选病程，生成复核上下文 | 否 |
| 4. 复核 | `POC/复核Agent` | 读复核上下文，判断各命题是否成立，引用由程序校验 | 是 |

## 数据与映射

- `datat/南山人医-数据导出`：脱敏后的六视图导出（患者信息、诊断信息、费用明细、检验报告、手术操作、病例信息）；
- `datat/标准病历视图.xlsx`：字段字典；
- `POC/标签视图映射/规则证据标签关系映射表_完整版.xlsx`：**谁查哪些数据**的唯一依据，由标签目录生成；
- `POC/规则证据Agent/标签目录.xlsx`：一级证据域、二级证据标签及其主要/次要来源，改映射先改这里。

## 环境与配置

- Python 3 + `openpyxl`（`POC/查询程序/requirements.txt`、`POC/查询规划Agent/requirements.txt`）；
- 模型配置放在 `POC/查询规划Agent/.env`（复制 `.env.example` 后填写 `MODEL_BASE_URL`、`MODEL_API_KEY`、`MODEL_NAME` 等）。规则证据Agent 默认也读这个文件；
- 模型接口较慢时，把 `QUERY_PLANNING_MODEL_TIMEOUT_SECONDS` 调大（本机用 180 秒）。

## 运行

各步骤都可单独运行，路径默认基于仓库内的 `POC` 和 `datat`：

```powershell
# 1. 规则拆解（只处理第 36 条规则，先校验输入用 --dry-run）
cd POC/规则证据Agent
python -m rule_evidence_agent --rule-number 36

# 2. 查询规划：默认批量处理全部规则，跑完自动调用查询程序
cd ../查询规划Agent
python -m query_planning_agent --rule-id RULE-009 --plan-only   # 只生成规划

# 3. 只跑查询程序（读取 查询规划结果/ 下对应规则的计划）
cd ../查询程序
python -m evidence_retriever --rule-id RULE-009

# 4. 复核（--dry-run 只校验上下文，不调用模型）
cd ../复核Agent
python -m review_agent --dry-run --context ../查询结果/复核上下文_RULE-038.json
```

输出位置：`POC/规则证据结果/`、`POC/查询规划结果/`、`POC/查询结果/`（含复核上下文）、`POC/复核结果/`。

## 病程记录关键词筛选

病程记录数量大（单次住院可达数百篇），先按关键词筛出值得复核的文书，再交给复核 Agent。

- **标签映射是硬开关**：只有映射到 `recordType=病程记录` 的证据点才生成检索词，模型不得新增数据源；
- **查询规划生成词根**：每个证据点 1～5 个高召回词根，按医生在病程中的写法生成，不能用证据标签名、单个字或纯数字；
- **命中不足时按病历原文补充一轮**：词根命中不超过 1 篇时，把候选病程的真实片段（按文书类型分配、时间均匀取、取前中后段，最多 1.5 万字符）发给模型补充词根，补出的词必须在病历里真实命中，与原词根取并集，上限 5 个；该轮结果记录在计划的 `record_search_retry` 中，可用 `--no-record-feedback` 关闭；
- **命中即保留完整原文**：筛中的病程保留整篇 `recordContent`，并标注 `matched_propositions`、`matched_evidence_tags`、`matched_terms`；
- **安全回退**：整体零命中、必需证据覆盖不全、计划缺失或无效时，回退返回全部候选病程，并记录 `fallback_reason`；补充证据零命中时保留它自身来源的病程，不影响整条规则；
- **映射表执行方式**：`匹配方式` 列为 `recordName包含` 时按名称包含匹配（如带医师姓名的"XX主任医师查房记录"），`recordType` 支持"；"分隔的多个类型（如"病程记录；手术相关"）。

设计与验收标准见 `docs/superpowers/specs/2026-09-22-medical-record-keyword-filter-design.md`；用法细节见 `POC/查询规划Agent/README.md` 和 `POC/查询程序/README.md`。

**注意**：`POC/查询规划结果/` 下部分规则的计划是加病程筛选之前生成的，不含病程词根；这些规则重跑查询规划后才会启用筛选。

## 已知问题

- `POC/查询程序` 有 3 个旧测试用例依赖过期数据（`规则证据结果.json` 已是 44 条规则、复核上下文已按规则拆分），在 main 上同样失败；
- 部分较早生成的 `POC/查询结果/复核上下文_RULE-*.json` 缺少 `mdtrt_id`，复核Agent 会直接报错；重跑查询程序生成的新版上下文正常（如 `复核上下文_RULE-038.json`）；
- 缺少 `POC/查询规划Agent/.env` 时，规则证据Agent 和复核Agent 无法调用模型；
- 其余历史实验目录（`POC/新建文件夹/`、`POC/RELU-000/`、`POC/规则9全流程/` 等）保留作对照，不是必需运行路径。
