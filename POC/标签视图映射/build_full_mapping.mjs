import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const inputPath = "D:/AI/AI_S2/POC/规则证据Agent/标签目录.xlsx";
const outputPath = "D:/AI/AI_S2/POC/标签视图映射/规则证据标签关系映射表_完整版.xlsx";
const previewDir = "D:/AI/AI_S2/POC/标签视图映射/.full_mapping_preview";

const sourceDefinitions = {
  "DS-S-001": {
    name: "患者信息", file: "患者信息.xlsx", view: "患者信息", documentType: "具体信息",
    recordName: "", recordType: "", matchMode: "按mdtrtId+medinsId查询", status: "可直接查询", note: "结构化视图"
  },
  "DS-S-002": {
    name: "费用明细", file: "费用明细.xlsx", view: "费用明细", documentType: "具体信息",
    recordName: "", recordType: "", matchMode: "按mdtrtId+medinsId查询", status: "可直接查询", note: "默认返回该就诊全量费用明细"
  },
  "DS-S-003": {
    name: "诊断信息", file: "诊断信息.xlsx", view: "诊断信息", documentType: "具体信息",
    recordName: "", recordType: "", matchMode: "按mdtrtId+medinsId查询", status: "视图存在但当前无数据", note: "已有视图文件，当前导出仅含表头"
  },
  "DS-S-004": {
    name: "检验报告", file: "检验报告.xlsx", view: "检验报告", documentType: "具体信息",
    recordName: "", recordType: "", matchMode: "按mdtrtId+medinsId查询", status: "视图存在但当前无数据", note: "已有视图文件，当前导出仅含表头"
  },
  "DS-S-005": {
    name: "检查报告", file: "", view: "", documentType: "具体信息",
    recordName: "", recordType: "", matchMode: "", status: "当前六视图无独立对应", note: "不将检验报告视图误当作检查报告"
  },
  "DS-S-006": {
    name: "手术操作", file: "手术操作.xlsx", view: "手术操作", documentType: "具体信息",
    recordName: "", recordType: "", matchMode: "按mdtrtId+medinsId查询", status: "视图存在但当前无数据", note: "已有视图文件，当前导出仅含表头"
  },
  "DS-S-007": {
    name: "病案首页", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "无纸化病历归档清单", recordType: "病案首页", matchMode: "recordType精确匹配", status: "recordType可匹配，recordName语义不完全一致", note: "实际recordName仅见‘无纸化病历归档清单’，不声称为真实病案首页原文"
  },
  "DS-S-008": {
    name: "病案费用类型", file: "费用明细.xlsx", view: "费用明细", documentType: "具体信息",
    recordName: "", recordType: "", matchMode: "按mdtrtId+medinsId查询", status: "兼容映射", note: "当前六视图无独立病案费用类型视图，暂从费用明细取证"
  },
  "DS-S-009": {
    name: "重症监护信息", file: "", view: "", documentType: "具体信息",
    recordName: "", recordType: "", matchMode: "", status: "当前六视图无独立对应", note: "可通过病历文书间接取证，但不冒充结构化视图"
  },
  "DS-S-010": {
    name: "输血品种", file: "费用明细.xlsx", view: "费用明细", documentType: "具体信息",
    recordName: "", recordType: "", matchMode: "按mdtrtId+medinsId查询", status: "兼容映射", note: "当前无独立输血品种视图，仅能从费用项目间接取证"
  },
  "DS-S-011": {
    name: "结算记录", file: "患者信息.xlsx", view: "患者信息", documentType: "具体信息",
    recordName: "", recordType: "", matchMode: "按mdtrtId+medinsId查询", status: "兼容映射", note: "当前无独立结算记录视图，仅可使用患者信息中的结算相关字段"
  },

  "DS-M-IP-001": {
    name: "住院病案首页", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "无纸化病历归档清单", recordType: "病案首页", matchMode: "recordType精确匹配", status: "recordType可匹配，recordName语义不完全一致", note: "当前数据没有真实住院病案首页recordName"
  },
  "DS-M-IP-002": {
    name: "入院记录", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "", recordType: "入院记录", matchMode: "recordType精确匹配", status: "可直接查询", note: "recordName含入院记录、再次入院记录、中西医及眼科变体"
  },
  "DS-M-IP-003": {
    name: "首次病程记录", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "首次病程记录", recordType: "病程记录", matchMode: "recordName包含", status: "可直接查询", note: "包含首次病程记录（中西医科）"
  },
  "DS-M-IP-004": {
    name: "日常病程记录", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "日常病程记录", recordType: "病程记录", matchMode: "recordName精确匹配", status: "可直接查询", note: ""
  },
  "DS-M-IP-005": {
    name: "上级医师查房记录", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "查房记录", recordType: "病程记录", matchMode: "recordName包含", status: "可模糊查询", note: "recordName中包含医师姓名和职称"
  },
  "DS-M-IP-006": {
    name: "阶段小结", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "阶段小结", recordType: "病程记录", matchMode: "recordName精确匹配", status: "可直接查询", note: ""
  },
  "DS-M-IP-007": {
    name: "会诊记录", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "会诊记录", recordType: "病程记录", matchMode: "recordName精确匹配", status: "可直接查询", note: ""
  },
  "DS-M-IP-008": {
    name: "抢救记录", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "抢救记录", recordType: "病程记录", matchMode: "recordName精确匹配", status: "可直接查询", note: ""
  },
  "DS-M-IP-009": {
    name: "术前小结", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "术前小结", recordType: "病程记录", matchMode: "recordName精确匹配", status: "可直接查询", note: ""
  },
  "DS-M-IP-010": {
    name: "术前讨论记录", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "术前病例讨论", recordType: "病程记录；手术相关", matchMode: "recordName包含", status: "语义兼容匹配", note: "实际recordName为主诊组/手术组/全科术前病例讨论记录及结论记录"
  },
  "DS-M-IP-011": {
    name: "麻醉评估与麻醉记录", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "", recordType: "", matchMode: "", status: "未匹配到目标原文", note: "当前仅见麻醉知情同意书，不是麻醉评估或执行记录"
  },
  "DS-M-IP-012": {
    name: "手术记录", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "手术记录", recordType: "手术相关", matchMode: "recordName包含", status: "可模糊查询", note: "包含手术记录新、手术记录（新）及剖宫产手术记录"
  },
  "DS-M-IP-013": {
    name: "有创操作记录", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "操作记录", recordType: "病程记录", matchMode: "recordName包含", status: "可模糊查询", note: "包含腰椎穿刺术、支气管镜等操作记录"
  },
  "DS-M-IP-014": {
    name: "输血记录", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "", recordType: "", matchMode: "", status: "未匹配到目标原文", note: "当前仅见输血治疗同意书，不是输血执行记录"
  },
  "DS-M-IP-015": {
    name: "护理记录单", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "", recordType: "", matchMode: "", status: "未匹配到目标原文", note: ""
  },
  "DS-M-IP-016": {
    name: "体温单与生命体征监测记录", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "", recordType: "", matchMode: "", status: "未匹配到目标原文", note: ""
  },
  "DS-M-IP-017": {
    name: "检验报告单", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "", recordType: "", matchMode: "", status: "未匹配到目标原文", note: "结构化检验报告视图可作独立来源，但当前也为空表"
  },
  "DS-M-IP-018": {
    name: "影像检查报告", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "", recordType: "", matchMode: "", status: "未匹配到目标原文", note: ""
  },
  "DS-M-IP-019": {
    name: "病理报告", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "", recordType: "", matchMode: "", status: "未匹配到目标原文", note: "当前仅见术中冰冻快速病理诊断知情同意书，不是病理报告"
  },
  "DS-M-IP-020": {
    name: "其他辅助检查报告", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "", recordType: "", matchMode: "", status: "未匹配到目标原文", note: ""
  },
  "DS-M-IP-021": {
    name: "出院记录或出院小结", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "出院记录", recordType: "出院记录", matchMode: "recordName包含", status: "可直接查询", note: "包含出院记录（产科）"
  },
  "DS-M-IP-022": {
    name: "死亡记录", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "死亡记录", recordType: "出院记录", matchMode: "recordName精确匹配", status: "可直接查询", note: ""
  },
  "DS-M-OP-001": {
    name: "门诊病历", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    variants: [
      { recordName: "初诊病历记录", recordType: "初诊病历记录", matchMode: "recordName+recordType精确匹配", status: "可直接查询", note: "门诊病历初诊类" },
      { recordName: "复诊病历记录", recordType: "复诊病历记录", matchMode: "recordName+recordType精确匹配", status: "可直接查询", note: "门诊病历复诊类" }
    ]
  },
  "DS-M-OP-002": {
    name: "检验报告单", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "", recordType: "", matchMode: "", status: "未匹配到目标原文", note: ""
  },
  "DS-M-OP-003": {
    name: "医学影像检查报告", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "", recordType: "", matchMode: "", status: "未匹配到目标原文", note: ""
  },
  "DS-M-OP-004": {
    name: "其他辅助检查报告", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "", recordType: "", matchMode: "", status: "未匹配到目标原文", note: ""
  },
  "DS-M-OP-005": {
    name: "测评量表", file: "病例信息.xlsx", view: "病例信息", documentType: "病历",
    recordName: "带状疱疹性神经痛医院焦虑抑郁情绪测量表", recordType: "知情告知", matchMode: "recordName精确匹配", status: "部分匹配", note: "当前仅匹配到1种特定量表，不代表测评量表全覆盖"
  }
};

function sourceVariants(definition) {
  return definition.variants ?? [{
    recordName: definition.recordName ?? "",
    recordType: definition.recordType ?? "",
    matchMode: definition.matchMode ?? "",
    status: definition.status ?? "",
    note: definition.note ?? ""
  }];
}

function parseSources(value) {
  if (value === null || value === undefined) return [];
  return String(value)
    .split(/\r?\n+/)
    .map((item) => item.trim())
    .filter((item) => item && item !== "—" && item !== "-");
}

function parseSourceLabel(label) {
  const match = label.match(/^(DS-(?:S|M-(?:IP|OP))-\d{3})(.*)$/);
  if (!match) throw new Error(`无法解析来源: ${label}`);
  return { code: match[1], name: match[2].trim() };
}

function columnName(index) {
  let result = "";
  let value = index;
  while (value > 0) {
    value -= 1;
    result = String.fromCharCode(65 + (value % 26)) + result;
    value = Math.floor(value / 26);
  }
  return result;
}

function styleDataSheet(sheet, lastRow, lastColumn, widths, statusColumn) {
  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(4);
  sheet.freezePanes.freezeColumns(Math.min(6, lastColumn));
  const endColumn = columnName(lastColumn);
  const used = sheet.getRange(`A1:${endColumn}${lastRow}`);
  used.format.font = { name: "Arial", size: 10, color: "#1F2937" };
  used.format.verticalAlignment = "center";
  sheet.getRange(`A1:${endColumn}1`).format.font = { name: "Arial", size: 15, bold: true, color: "#163A5F" };
  sheet.getRange(`A2:${endColumn}2`).format.font = { name: "Arial", size: 10, italic: true, color: "#5B6573" };
  sheet.getRange(`A4:${endColumn}4`).format = {
    fill: "#1F4E78",
    font: { name: "Arial", size: 10, bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "inside", style: "thin", color: "#D9E2F3" }
  };
  if (lastRow >= 5) {
    sheet.getRange(`A5:${endColumn}${lastRow}`).format.wrapText = true;
    sheet.getRange(`A5:${endColumn}${lastRow}`).format.borders = {
      insideHorizontal: { style: "thin", color: "#E5E7EB" },
      bottom: { style: "thin", color: "#C9D2DC" }
    };
  }
  widths.forEach((width, index) => {
    sheet.getRange(`${columnName(index + 1)}1:${columnName(index + 1)}${lastRow}`).format.columnWidth = width;
  });
  sheet.getRange(`A1:${endColumn}${lastRow}`).format.autofitRows();
  sheet.getRange(`A4:${endColumn}4`).format.rowHeight = 34;
  if (statusColumn && lastRow >= 5) {
    const statusRange = sheet.getRange(`${statusColumn}5:${statusColumn}${lastRow}`);
    statusRange.conditionalFormats.add("containsText", {
      text: "未匹配",
      format: { fill: "#FDECEC", font: { color: "#B42318", bold: true } }
    });
    statusRange.conditionalFormats.add("containsText", {
      text: "当前六视图无",
      format: { fill: "#FFF4E5", font: { color: "#9A6700", bold: true } }
    });
    statusRange.conditionalFormats.add("containsText", {
      text: "可直接查询",
      format: { fill: "#ECFDF3", font: { color: "#027A48" } }
    });
  }
}

const sourceWorkbook = await SpreadsheetFile.importXlsx(await FileBlob.load(inputPath));
const sourceSheet = sourceWorkbook.worksheets.getItemAt(0);
const sourceRows = sourceSheet.getUsedRange(true).values;
const headers = sourceRows[0].map((value) => String(value ?? "").trim());
const headerIndex = Object.fromEntries(headers.map((header, index) => [header, index]));
const requiredHeaders = ["标签ID", "一级证据域", "二级证据标签", "主要来源", "次要来源"];
for (const header of requiredHeaders) {
  if (!(header in headerIndex)) throw new Error(`标签目录缺少字段: ${header}`);
}

const tags = sourceRows.slice(1)
  .filter((row) => row[headerIndex["标签ID"]])
  .map((row) => ({
    tagId: String(row[headerIndex["标签ID"]] ?? "").trim(),
    primaryTag: String(row[headerIndex["一级证据域"]] ?? "").trim(),
    secondaryTag: String(row[headerIndex["二级证据标签"]] ?? "").trim(),
    primarySources: parseSources(row[headerIndex["主要来源"]]),
    secondarySources: parseSources(row[headerIndex["次要来源"]])
  }));

const mappingRows = [];
let mappingSequence = 1;
for (const tag of tags) {
  for (const [level, sources] of [["主要来源", tag.primarySources], ["次要来源", tag.secondarySources]]) {
    for (const sourceLabel of sources) {
      const parsed = parseSourceLabel(sourceLabel);
      const definition = sourceDefinitions[parsed.code];
      if (!definition) throw new Error(`缺少来源视图字典: ${parsed.code}`);
      if (definition.name !== parsed.name) throw new Error(`来源名称不一致: ${sourceLabel} / ${definition.name}`);
      for (const variant of sourceVariants(definition)) {
        mappingRows.push([
          `MAP-${String(mappingSequence).padStart(4, "0")}`,
          tag.tagId,
          "运行时传入",
          "运行时传入",
          tag.primaryTag,
          tag.secondaryTag,
          level,
          level === "主要来源" ? sourceLabel : "",
          level === "次要来源" ? sourceLabel : "",
          definition.file ?? "",
          definition.view ?? "",
          definition.documentType ?? "",
          variant.recordName ?? "",
          variant.recordType ?? "",
          variant.matchMode ?? "",
          variant.status ?? definition.status ?? "",
          variant.note ?? definition.note ?? ""
        ]);
        mappingSequence += 1;
      }
    }
  }
}

const dictionaryRows = [];
for (const code of Object.keys(sourceDefinitions).sort()) {
  const definition = sourceDefinitions[code];
  for (const variant of sourceVariants(definition)) {
    dictionaryRows.push([
      code,
      definition.name,
      definition.file ?? "",
      definition.view ?? "",
      definition.documentType ?? "",
      definition.documentType === "具体信息" ? "mdtrtId + medinsId" : "mdtrtId + medinsId + recordName/recordType",
      variant.recordName ?? "",
      variant.recordType ?? "",
      variant.matchMode ?? "",
      variant.status ?? definition.status ?? "",
      variant.note ?? definition.note ?? ""
    ]);
  }
}

const mappingCounts = new Map();
for (const row of mappingRows) mappingCounts.set(row[1], (mappingCounts.get(row[1]) ?? 0) + 1);
const coverageRows = tags.map((tag) => [
  tag.tagId,
  tag.primaryTag,
  tag.secondaryTag,
  tag.primarySources.length,
  tag.secondarySources.length,
  mappingCounts.get(tag.tagId) ?? 0,
  (mappingCounts.get(tag.tagId) ?? 0) > 0 ? "已覆盖" : "未覆盖"
]);

const workbook = Workbook.create();

const mappingSheet = workbook.worksheets.add("拆分映射");
mappingSheet.tabColor = "#1F4E78";
mappingSheet.getRange("A1").values = [["规则证据标签关系映射表（完整版）"]];
mappingSheet.getRange("A2").values = [["每个标签的每个主要/次要来源独立成行；mdtrtId和medinsId由查询程序在运行时传入。"]];
const mappingHeaders = ["mapping_id", "标签ID", "mdtrtId", "medinsId", "一级标签名", "二级标签名", "来源级别", "主要来源", "次要来源", "对应数据文件", "对应的视图", "文书类型", "匹配到的recordName", "recordType", "匹配方式", "匹配状态", "备注"];
mappingSheet.getRange("A4").write([mappingHeaders, ...mappingRows]);
const mappingLastRow = 4 + mappingRows.length;
const mappingTable = mappingSheet.tables.add(`A4:Q${mappingLastRow}`, true, "TagSourceViewMapping");
mappingTable.style = "TableStyleMedium2";
styleDataSheet(mappingSheet, mappingLastRow, mappingHeaders.length, [15, 15, 14, 16, 13, 16, 13, 25, 25, 18, 16, 12, 26, 18, 20, 26, 38], "P");

const dictionarySheet = workbook.worksheets.add("来源视图字典");
dictionarySheet.tabColor = "#5B9BD5";
dictionarySheet.getRange("A1").values = [["来源—视图—文书匹配字典"]];
dictionarySheet.getRange("A2").values = [["字典仅覆盖标签目录实际引用的38种来源；匹配状态依据当前真实六视图导出数据。"]];
const dictionaryHeaders = ["来源编码", "来源名称", "对应数据文件", "对应的视图", "文书类型", "查询主键", "匹配到的recordName", "recordType", "匹配方式", "匹配状态", "备注"];
dictionarySheet.getRange("A4").write([dictionaryHeaders, ...dictionaryRows]);
const dictionaryLastRow = 4 + dictionaryRows.length;
const dictionaryTable = dictionarySheet.tables.add(`A4:K${dictionaryLastRow}`, true, "SourceViewDictionary");
dictionaryTable.style = "TableStyleMedium2";
styleDataSheet(dictionarySheet, dictionaryLastRow, dictionaryHeaders.length, [16, 24, 18, 16, 12, 28, 30, 20, 24, 30, 44], "J");

const statsSheet = workbook.worksheets.add("覆盖统计");
statsSheet.tabColor = "#A5A5A5";
statsSheet.showGridLines = false;
statsSheet.getRange("A1").values = [["映射覆盖统计"]];
statsSheet.getRange("A1:G1").format.font = { name: "Arial", size: 15, bold: true, color: "#163A5F" };
const uniqueReferencedSources = new Set();
for (const tag of tags) {
  for (const source of [...tag.primarySources, ...tag.secondarySources]) uniqueReferencedSources.add(parseSourceLabel(source).code);
}
const statusCounts = new Map();
for (const row of dictionaryRows) statusCounts.set(row[9], (statusCounts.get(row[9]) ?? 0) + 1);
const summaryRows = [
  ["指标", "数值"],
  ["标签总数", tags.length],
  ["已覆盖标签", coverageRows.filter((row) => row[6] === "已覆盖").length],
  ["未覆盖标签", coverageRows.filter((row) => row[6] !== "已覆盖").length],
  ["标签覆盖率", tags.length ? coverageRows.filter((row) => row[6] === "已覆盖").length / tags.length : 0],
  ["标签引用来源类型", uniqueReferencedSources.size],
  ["展开映射行数", mappingRows.length]
];
statsSheet.getRange("A3").write(summaryRows);
statsSheet.getRange("B7").format.numberFormat = "0.00%";
statsSheet.getRange("A3:B9").format.font = { name: "Arial", size: 10 };
statsSheet.getRange("A3:B3").format = { fill: "#1F4E78", font: { name: "Arial", size: 10, bold: true, color: "#FFFFFF" }, horizontalAlignment: "center" };
statsSheet.getRange("A3:B9").format.borders = { preset: "all", style: "thin", color: "#D9E2F3" };

const statusSummary = [["来源匹配状态", "数量"], ...[...statusCounts.entries()].sort((a, b) => a[0].localeCompare(b[0], "zh-CN"))];
statsSheet.getRange("D3").write(statusSummary);
const statusSummaryLastRow = 2 + statusSummary.length;
statsSheet.getRange(`D3:E${statusSummaryLastRow}`).format.font = { name: "Arial", size: 10 };
statsSheet.getRange("D3:E3").format = { fill: "#5B9BD5", font: { name: "Arial", size: 10, bold: true, color: "#FFFFFF" }, horizontalAlignment: "center" };
statsSheet.getRange(`D3:E${statusSummaryLastRow}`).format.borders = { preset: "all", style: "thin", color: "#D9E2F3" };

statsSheet.getRange("A12").values = [["标签覆盖明细"]];
statsSheet.getRange("A12:G12").format.font = { name: "Arial", size: 12, bold: true, color: "#163A5F" };
const coverageHeaders = ["标签ID", "一级标签名", "二级标签名", "主要来源数", "次要来源数", "展开映射行数", "覆盖状态"];
statsSheet.getRange("A14").write([coverageHeaders, ...coverageRows]);
const coverageLastRow = 14 + coverageRows.length;
const coverageTable = statsSheet.tables.add(`A14:G${coverageLastRow}`, true, "TagCoverage");
coverageTable.style = "TableStyleMedium2";
statsSheet.getRange(`A14:G${coverageLastRow}`).format.font = { name: "Arial", size: 10 };
statsSheet.getRange("A14:G14").format = { fill: "#1F4E78", font: { name: "Arial", size: 10, bold: true, color: "#FFFFFF" }, horizontalAlignment: "center", wrapText: true };
statsSheet.getRange(`A15:G${coverageLastRow}`).format.borders = { insideHorizontal: { style: "thin", color: "#E5E7EB" } };
statsSheet.freezePanes.freezeRows(14);
statsSheet.getRange(`A1:G${coverageLastRow}`).format.verticalAlignment = "center";
statsSheet.getRange(`A1:G${coverageLastRow}`).format.autofitRows();
statsSheet.getRange("A1:A100").format.columnWidth = 20;
statsSheet.getRange("B1:B100").format.columnWidth = 18;
statsSheet.getRange("C1:C100").format.columnWidth = 18;
statsSheet.getRange("D1:D100").format.columnWidth = 48;
statsSheet.getRange("E1:E100").format.columnWidth = 16;
statsSheet.getRange("F1:F100").format.columnWidth = 18;
statsSheet.getRange("G1:G100").format.columnWidth = 14;
statsSheet.getRange(`G15:G${coverageLastRow}`).conditionalFormats.add("containsText", {
  text: "未覆盖", format: { fill: "#FDECEC", font: { color: "#B42318", bold: true } }
});

workbook.recalculate();

const mappingCheck = await workbook.inspect({
  kind: "table",
  range: "拆分映射!A1:Q12",
  include: "values,formulas",
  tableMaxRows: 12,
  tableMaxCols: 17,
  maxChars: 9000
});
console.log(mappingCheck.ndjson);

const statsCheck = await workbook.inspect({
  kind: "table",
  range: "覆盖统计!A1:G20",
  include: "values,formulas",
  tableMaxRows: 20,
  tableMaxCols: 7,
  maxChars: 7000
});
console.log(statsCheck.ndjson);

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan"
});
console.log(errors.ndjson);

await fs.mkdir(previewDir, { recursive: true });
for (const [sheetName, range, fileName] of [
  ["拆分映射", "A1:Q14", "mapping.png"],
  ["来源视图字典", "A1:K18", "dictionary.png"],
  ["覆盖统计", "A1:G24", "coverage.png"]
]) {
  const preview = await workbook.render({ sheetName, range, scale: 1.2, format: "png" });
  await fs.writeFile(`${previewDir}/${fileName}`, new Uint8Array(await preview.arrayBuffer()));
}

await fs.mkdir("D:/AI/AI_S2/POC/标签视图映射", { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);

console.log(JSON.stringify({
  outputPath,
  tagCount: tags.length,
  referencedSourceTypeCount: uniqueReferencedSources.size,
  dictionaryRowCount: dictionaryRows.length,
  mappingRowCount: mappingRows.length,
  uncoveredTagCount: coverageRows.filter((row) => row[6] !== "已覆盖").length
}, null, 2));
