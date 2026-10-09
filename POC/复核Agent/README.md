# 复核 Agent

本 Agent 读取查询程序生成的 `复核上下文.json`，调用与规则证据 Agent 相同的模型，对事实、申诉命题和整体案件进行复核。

## 模型

默认模型配置与规则证据 Agent 一致：

- `qwen3.6-27b`；
- 关闭思考模式；
- `temperature=0`；
- JSON对象输出。

提示词要求证据和理由使用最小必要内容，以减少输出时间；程序不设置最大输出Token，由模型服务端决定输出上限。

程序优先读取当前目录的 `.env`。如果当前目录没有 `.env`，默认复用 `POC/规则证据Agent/.env`。也可以通过 `--env-file` 指定其他配置。

## 输入输出

默认输入：

```text
D:\AI\AI_S2\POC\查询结果\复核上下文.json
```

默认输出：

```text
D:\AI\AI_S2\POC\复核结果\复核结果.json
```

## 运行

```powershell
cd D:\AI\AI_S2\POC\复核Agent
C:\Users\weyk\anaconda3\python.exe -m review_agent
```

指定复核轮次：

```powershell
C:\Users\weyk\anaconda3\python.exe -m review_agent --review-round 2
```

只校验上下文，不调用模型：

```powershell
C:\Users\weyk\anaconda3\python.exe -m review_agent --dry-run
```

完整参数：

```powershell
C:\Users\weyk\anaconda3\python.exe -m review_agent `
  --context "D:\AI\AI_S2\POC\查询结果\复核上下文.json" `
  --output "D:\AI\AI_S2\POC\复核结果\复核结果.json" `
  --review-round 1
```

## 校验

程序会在写入结果前校验：

- 所有事实和命题是否完整输出；
- 病历引用是否确实存在于对应 `recordContent`；
- 结构化字段引用是否与查询结果一致；
- 模型输出的命题表达式是否合法；
- 表达式是否覆盖全部命题；
- `overall_logic_status`是否与程序计算结果一致；
- 复核状态、案件状态和最终结论是否一致。

程序不会写死P01、P02、P03之间的关系。模型负责根据提示词输出通用逻辑表达式，程序只负责解析和计算。

每次执行完成后，命令行会显示从发起模型调用到模型结果校验完成的整体输出时间。
如果模型接口支持流式响应，还会显示模型调用的首Token耗时、Token/s、调用用时、输入Token、输出Token和总Token；接口未返回Token用量时，相关Token数会使用估算值。正常案件只执行一次完整复核调用；仅当证据引用经过程序自动纠正后仍无法验证时，额外执行一次小型引用专项修复。专项修复仍失败时，程序将受影响事实安全降级为`UNKNOWN`并继续输出结果，不会因引用错误中断。`overall_logic_status`由程序根据`expression`和`proposition_reviews`确定性计算并写入，不采用模型自行填写的值。

为减少模型输出Token，主复核调用只要求模型返回事实编号、事实判断、证据、理由、命题判断、复核状态和最终理由。案件标识、事实元数据、整体逻辑状态、案件状态及完整最终结论由程序恢复后再执行原有校验，因此落盘结果格式和判断校验规则保持不变。
