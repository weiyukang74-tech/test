from __future__ import annotations

import json
from typing import Any


SEARCH_FIELDS = ["hilist_name", "hosplist_name"]
FALLBACK_SEARCH_FIELDS = ["set_name"]
LAB_SEARCH_FIELDS = ["itemName", "indexName"]
LAB_FALLBACK_SEARCH_FIELDS: list[str] = []

SYSTEM_PROMPT = """你是医保查询关键词规划助手。程序已经决定哪些事实需要查询费用明细或检验报告明细。你只负责针对每个target_fact独立提取若干检索关键词，并分别进行名称规范化和同义扩展。

要求：
1. 必须为输入中的每个fact_id输出且只输出一项，不得合并不同事实。
2. 每个事实输出1至6个keyword_groups。不同keyword_groups之间是OR关系；同一组required_terms中的词必须同时命中，是AND关系。
3. source_phrase优先使用该事实的target_fact、当前命题的proposition_statement、violation_item或violation_description中的连续原文；为了名称检索，也可以使用与当前事实直接相关的规范名称或常见同义表达，但不能从其他事实借用或扩展成无关项目。
4. 如果query_domain是“费用明细”，优先提取可出现在收费项目名称中的具体对象、操作、治疗、药品、耗材或文书名称；如果query_domain是“检验报告明细”，优先提取可单独出现在itemName或indexName任一字段中的具体检验项目、指标、病原体、抗体、细胞亚群或生化指标名称。不要要求itemName和indexName拼接后才命中，也不要输出“获取”“确认”“是否”“相关”“本次”等动作词或空泛词。
5. required_terms用于处理词序、括号和限定词差异。必须保留部位、大小、侧别等会改变项目含义的限定信息。例如“大关节松动训练”输出["关节松动训练", "大关节"]。
6. alternative_terms只放有把握的常见名称、简称或词序变体；没有可靠扩展时返回空数组。不得虚构具体药名、手术名或收费编码。
7. violation_item和violation_description只作为当前事实的辅助锚点。只有与该target_fact直接相关时才可形成关键词组，不能让所有事实都只查询监管原文。
8. 不推荐收费类别，不判断合规性，不选择来源、视图或返回字段；query_domain只用于决定关键词语境。
9. 只输出JSON对象，不输出解释、Markdown或代码围栏。

输出结构：
{
  "rule_id": "...",
  "facts": [
    {
      "fact_id": "F01",
      "keyword_groups": [
        {
          "source_phrase": "原文关键词或与当前事实直接相关的规范名称",
          "required_terms": ["必要词1", "必要词2"],
          "alternative_terms": ["常见变体"]
        }
      ]
    }
  ]
}"""


MEDICAL_RECORD_SYSTEM_PROMPT = """你是医保病程记录检索词规划助手。程序已经根据证据标签决定哪些事实需要查询病程记录。你只负责为每个事实生成用于召回病程原文的高召回词根，不判断证据是否成立，也不得新增数据源。

病程记录由医生书写，包括首次病程记录、日常病程记录、上级医师查房记录、会诊记录、阶段小结、术前小结等。医生用临床用语记录病情和处理，例如“予……治疗”“请……科会诊”“拟行……术”，一般不会写收费项目名称或医保规则原文。

要求：
1. 必须为输入中的每个fact_id输出且只输出一项，不得遗漏、合并或借用其他事实的概念。
2. 每个事实输出1至5个record_search_terms，词根之间是OR关系。
3. 词根要按医生在病程中的实际写法生成。收费项目名称、医保规则用语和target_fact中的描述往往不会原样出现在病程中，不能只照抄这些名称。
4. 同一事实的词根应覆盖不同写法：核心医学名称，常见简称或英文缩写，以及医生书写时常用的上位写法。治疗或操作类事实必须包含该治疗所属类别或执行科室的常见写法，例如“血液灌流”在病程中常写作“血液净化”，“经鼻高流量湿化氧疗”常写作“吸氧”“氧疗”。
5. 优先输出能覆盖多种写法的短词根。例如“雌激素受体阳性”可输出“雌激素”和“ER”，不要只输出完整结论“ER阳性”。
6. 词根用于找到值得复核的原文，不代表条件成立；阳性、阴性和否定表达都应可被召回。
7. 不输出几乎每篇病程都会出现的通用词，例如“患者”“病历”“是否”“相关”“本次”“检查”“手术”“术前”“操作”“治疗”“症状”“体征”“变化”“切口”“通畅”“剂量”“频次”“疗程”“筛查”“护理”。需要表达这类含义时，改用带具体对象的写法，例如用“导尿管”而不是“置管”。
8. 不输出单个字、纯数字或单个英文字母，不输出正则表达式。输入中的forbidden_record_search_terms列出了本次全部二级证据标签名称（例如“严重程度”“既往治疗”）；这些名称只用于组织取证，不得出现在任何事实的record_search_terms中，即使它来自另一个事实也不允许。改用当前target_fact对应的具体临床写法，输出前逐项检查全部事实。
9. 病历中可能本来就没有某个事实的记录。不得为了命中而输出与该事实无关的宽泛词，不得虚构药名、疾病、检查、部位或治疗，没有把握的同义表达不要输出。
10. 只输出JSON对象，不输出解释、Markdown或代码围栏。

输出结构：
{
  "rule_id": "...",
  "facts": [
    {
      "fact_id": "M01",
      "record_search_terms": ["词根1", "词根2"]
    }
  ]
}"""


MEDICAL_RECORD_RETRY_SYSTEM_PROMPT = """你是医保病程记录检索词助手。某个证据点现有的检索词根在该患者病程记录里命中很少或没有命中，说明可能遗漏了病历里的其他写法。下面给出这个证据点、现有词根，以及该患者病程记录的真实片段。

你的任务只是按原文“改写和补充”，不是重新总结病历：
1. 检索目标不变：词根必须服务于上面这个证据点，用来找出能证明或核对它的病程；不得因为片段里出现了别的医学内容就改变检索目标。
2. 只从片段里真实出现的写法中挑选，优先选择原文中能对应这个证据点的写法。
3. 最多新增3个词根；不要重复现有词根；每个词根都要能在该患者病程里真正命中，命中不了的会被程序丢弃。
4. 如果片段里确实找不到与这个证据点相关的写法，返回JSON对象 {"added_record_search_terms": []}，不得直接返回顶层空数组 []，不要用泛词或无关词凑数。
5. 不输出单个字、纯数字或单个英文字母，也不输出证据标签名称。
6. 只输出JSON对象，格式为 {"added_record_search_terms": ["词根1", "词根2"]}，不输出解释、Markdown或代码围栏。"""


def build_user_prompt(
    rule_id: str,
    violation_item: str,
    violation_description: str,
    facts: list[dict[str, Any]],
) -> str:
    payload = {
        "rule_id": rule_id,
        "violation_item": violation_item,
        "violation_description": violation_description,
        "facts": facts,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def build_repair_prompt(
    rule_id: str,
    violation_item: str,
    violation_description: str,
    facts: list[dict[str, Any]],
    previous_result: dict[str, Any],
    validation_error: str,
) -> str:
    return (
        build_user_prompt(
            rule_id,
            violation_item,
            violation_description,
            facts,
        )
        + "\n\n上一次输出未通过程序校验。请根据校验错误重新输出完整JSON对象。"
        + "source_phrase可以是原文关键词或与当前事实直接相关的规范名称，但不得引入无关项目。"
        + "不要解释，不要输出Markdown。\n"
        + json.dumps(
            {
                "validation_error": validation_error,
                "previous_result": previous_result,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def build_medical_record_user_prompt(
    rule_id: str,
    rule_text: str,
    facts: list[dict[str, Any]],
) -> str:
    return json.dumps(
        {
            "rule_id": rule_id,
            "rule_text": rule_text,
            "facts": facts,
            "forbidden_record_search_terms": sorted(
                {
                    str(fact.get("二级证据标签", "")).strip()
                    for fact in facts
                    if str(fact.get("二级证据标签", "")).strip()
                }
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def build_medical_record_retry_prompt(
    rule_id: str,
    rule_text: str,
    fact: dict[str, Any],
    current_terms: list[str],
    excerpts: str,
) -> str:
    return json.dumps(
        {
            "rule_id": rule_id,
            "rule_text": rule_text,
            "待证明证据点": fact,
            "current_record_search_terms": current_terms,
            "病程真实片段": excerpts,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def build_medical_record_repair_prompt(
    rule_id: str,
    rule_text: str,
    facts: list[dict[str, Any]],
    previous_result: dict[str, Any],
    validation_error: str,
    forbidden_record_search_terms: list[str] | None = None,
) -> str:
    payload = json.loads(build_medical_record_user_prompt(rule_id, rule_text, facts))
    if forbidden_record_search_terms is not None:
        payload["forbidden_record_search_terms"] = forbidden_record_search_terms
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n\n上一次输出未通过程序校验。请根据校验错误重新输出完整JSON对象。"
        + "本次输入facts只包含需要修正的事实，只输出这些fact_id；其他已通过事实由程序保留，不得新增。"
        + "校验错误只报告遇到的第一处问题，并不代表其他事实已经通过。"
        + "必须检查全部facts的全部record_search_terms，删除所有与forbidden_record_search_terms中名称相同的词根，"
        + "不得只修正validation_error指出的那一项，也不得在其他事实中保留同类错误。"
        + "每个事实的剩余词根必须服务于该target_fact；若删除后为空，只能补充有依据的具体临床写法，不得编造或用泛词凑数。"
        + "每个事实必须包含1至5个病程检索词根，不得解释，不得输出Markdown。\n"
        + json.dumps(
            {
                "validation_error": validation_error,
                "previous_result": previous_result,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
