import fs from 'node:fs/promises';
import { SpreadsheetFile, Workbook } from '@oai/artifact-tool';

const outputDir = 'D:/AI/AI_S2/POC/标签视图映射';
const outputPath = `${outputDir}/规则证据标签关系映射表_v2.xlsx`;
const previewPath = `${outputDir}/规则证据标签关系映射表_v2预览.png`;
const medinsId = 'H44030500096';
const mdtrtId = '119797913';

const tags = [
  { l1: '患者', l2: '性别', primary: ['DS-S-001'], secondary: ['DS-S-007', 'DS-M-OP-001', 'DS-M-IP-001'] },
  { l1: '患者', l2: '生育状态', primary: ['DS-S-001'], secondary: ['DS-M-IP-002'] },
  { l1: '病理', l2: '病理标志', primary: ['DS-M-IP-019'], secondary: [] },
  { l1: '诊断', l2: '标准诊断', primary: ['DS-S-003'], secondary: ['DS-S-007', 'DS-M-IP-001'] },
  { l1: '评估', l2: '疾病分期', primary: ['DS-S-003'], secondary: ['DS-S-007'] },
  { l1: '治疗史', l2: '既往治疗', primary: ['DS-M-OP-001', 'DS-M-IP-002', 'DS-M-IP-003'], secondary: [] },
  { l1: '病史', l2: '病程时间', primary: ['DS-M-OP-001', 'DS-M-IP-002'], secondary: [] },
  { l1: '治疗', l2: '治疗阶段', primary: ['DS-M-IP-006'], secondary: [] },
  { l1: '病情', l2: '活动进展', primary: ['DS-M-OP-001', 'DS-M-IP-002', 'DS-M-IP-003'], secondary: ['DS-S-003', 'DS-M-IP-018', 'DS-M-IP-021'] },
  { l1: '检查', l2: '影像结果', primary: ['DS-S-005', 'DS-M-OP-003', 'DS-M-IP-018'], secondary: [] },
  { l1: '用药', l2: '药品医嘱', primary: ['DS-M-OP-001', 'DS-M-IP-006', 'DS-M-IP-021'], secondary: [] },
  { l1: '治疗史', l2: '治疗反应', primary: ['DS-M-OP-001', 'DS-M-IP-002', 'DS-M-IP-003', 'DS-M-IP-004', 'DS-M-IP-021'], secondary: [] },
];

const sourceDefinitions = {
  'DS-S-001': { name: '患者信息', view: '患者信息', type: '具体信息', recordName: '不适用', recordType: '不适用', status: '结构化视图直接对应' },
  'DS-S-003': { name: '诊断信息', view: '诊断信息', type: '具体信息', recordName: '不适用', recordType: '不适用', status: '结构化视图直接对应' },
  'DS-S-005': { name: '检查报告', view: 'DS-S-005当前六视图无对应', type: '具体信息', recordName: '不适用', recordType: '不适用', status: '当前六视图无对应' },
  'DS-S-007': { name: '病案首页', view: 'DS-S-007当前六视图无对应', type: '具体信息', recordName: '不适用', recordType: '不适用', status: '当前六视图无对应' },
  'DS-M-OP-001': { name: '门诊病历', view: '病历信息', type: '病历', recordName: '未匹配', recordType: '未匹配', status: '当前脱敏病历未找到对应 recordName' },
  'DS-M-IP-001': { name: '住院病案首页', view: '病历信息', type: '病历', recordName: '患者基本信息登记确认表', recordType: '入院登记处', status: '候选近似匹配，需业务确认是否等同住院病案首页' },
  'DS-M-IP-002': { name: '入院记录', view: '病历信息', type: '病历', recordName: '再次入院记录', recordType: '入院记录', status: '已匹配：recordType 与来源类型一致' },
  'DS-M-IP-003': { name: '首次病程记录', view: '病历信息', type: '病历', recordName: '首次病程记录', recordType: '病程记录', status: '已精确匹配 recordName' },
  'DS-M-IP-004': { name: '日常病程记录', view: '病历信息', type: '病历', recordName: '日常病程记录', recordType: '病程记录', status: '已精确匹配 recordName' },
  'DS-M-IP-006': { name: '阶段小结', view: '病历信息', type: '病历', recordName: '未匹配', recordType: '未匹配', status: '当前脱敏病历未找到对应 recordName' },
  'DS-M-IP-018': { name: '影像检查报告', view: '病历信息', type: '病历', recordName: '未匹配', recordType: '未匹配', status: '当前脱敏病历未找到对应 recordName' },
  'DS-M-IP-019': { name: '病理报告', view: '病历信息', type: '病历', recordName: '未匹配', recordType: '未匹配', status: '当前脱敏病历未找到对应 recordName' },
  'DS-M-IP-021': { name: '出院记录或出院小结', view: '病历信息', type: '病历', recordName: '出院记录', recordType: '出院记录', status: '已匹配来源名称中的“出院记录”' },
  'DS-M-OP-003': { name: '医学影像检查报告', view: '病历信息', type: '病历', recordName: '未匹配', recordType: '未匹配', status: '当前脱敏病历未找到对应 recordName' },
};

const mappingRows = [];
let mappingIndex = 1;
for (const tag of tags) {
  for (const [level, sourceCodes] of [['PRIMARY', tag.primary], ['SECONDARY', tag.secondary]]) {
    for (const code of sourceCodes) {
      const source = sourceDefinitions[code];
      mappingRows.push([
        `MAP-${String(mappingIndex).padStart(3, '0')}`,
        mdtrtId,
        medinsId,
        tag.l1,
        tag.l2,
        level,
        level === 'PRIMARY' ? `${code} ${source.name}` : '—',
        level === 'SECONDARY' ? `${code} ${source.name}` : '—',
        source.view,
        source.type,
        source.recordName,
        source.recordType,
        source.status,
      ]);
      mappingIndex += 1;
    }
  }
}

const recordInventory = [
  ['出院记录', '出院记录'],
  ['再次入院记录', '入院记录'],
  ['首次病程记录', '病程记录'],
  ['黄某主任医师查房记录', '病程记录'],
  ['王某副主任医师查房记录', '病程记录'],
  ['会诊记录', '病程记录'],
  ['日常病程记录', '病程记录'],
  ['会诊记录', '病程记录'],
  ['入院病情评估记录', '知情告知'],
  ['医患双方不收和不送“红包”协议书', '知情告知'],
  ['疾病诊断证明书', '出院记录'],
  ['住院病人出院结算通知书', '出院记录'],
  ['使用材料、药品知情同意书', '知情告知'],
  ['患者基本信息登记确认表', '入院登记处'],
  ['患者住院须知', '入院登记处'],
];

const fieldRows = [
  ['患者信息', 'medins_id', 'medinsId', '查询条件'],
  ['患者信息', 'mdtrt_id', 'mdtrtId', '查询条件'],
  ['费用明细', 'medins_id（标准表未列出）', 'medinsId', '脱敏病历实际存在，查询条件'],
  ['费用明细', 'mdtrt_id（标准表未列出）', 'mdtrtId', '脱敏病历实际存在，查询条件'],
  ['诊断信息', 'medins_id', 'medinsId', '查询条件'],
  ['诊断信息', 'mdtrt_id', 'mdtrtId', '查询条件'],
  ['病历信息', 'medins_id', 'medinsId', '查询条件'],
  ['病历信息', 'mdtrt_id', 'mdtrtId', '查询条件'],
  ['病历信息', 'record_name', 'recordName', '文书名称匹配'],
  ['病历信息', 'record_type', 'recordType', '文书类别辅助匹配'],
  ['病历信息', 'record_content', 'recordContent', '完整原文返回'],
  ['诊断信息', 'dise_name', 'diseName', '暂不加入查询条件'],
  ['费用明细', 'hilist_name', 'hilistName', '暂不加入查询条件'],
];

const wb = Workbook.create();
const mappingSheet = wb.worksheets.add('拆分映射');
const inventorySheet = wb.worksheets.add('recordName清单');
const fieldsSheet = wb.worksheets.add('字段名兼容');
for (const sheet of [mappingSheet, inventorySheet, fieldsSheet]) sheet.showGridLines = false;

function buildSheet(sheet, title, note, headers, data, widths, tableName, rowHeight = 42) {
  const endCol = String.fromCharCode(64 + headers.length);
  sheet.getRange(`A1:${endCol}1`).merge();
  sheet.getRange('A1').values = [[title]];
  sheet.getRange(`A2:${endCol}2`).merge();
  sheet.getRange('A2').values = [[note]];
  sheet.getRange(`A4:${endCol}4`).values = [headers];
  sheet.getRange(`A5:${endCol}${4 + data.length}`).values = data;
  sheet.getRange(`A1:${endCol}${4 + data.length}`).format.font = { name: 'Arial', size: 10, color: '#1F2937' };
  sheet.getRange('A1').format = { font: { name: 'Arial', size: 14, bold: true, color: '#1F2937' } };
  sheet.getRange('A2').format = { font: { name: 'Arial', size: 9, italic: true, color: '#5B6573' }, wrapText: true };
  sheet.getRange(`A4:${endCol}4`).format = {
    fill: '#1F4E78', font: { name: 'Arial', size: 10, bold: true, color: '#FFFFFF' },
    horizontalAlignment: 'center', verticalAlignment: 'center', wrapText: true,
    borders: { preset: 'all', style: 'thin', color: '#D9E2F3' },
  };
  sheet.getRange(`A5:${endCol}${4 + data.length}`).format = {
    verticalAlignment: 'center', wrapText: true,
    borders: { preset: 'all', style: 'thin', color: '#D9E2F3' },
  };
  widths.forEach((width, index) => {
    const col = String.fromCharCode(65 + index);
    sheet.getRange(`${col}:${col}`).format.columnWidth = width;
  });
  sheet.getRange(`A4:${endCol}4`).format.rowHeight = 34;
  sheet.getRange(`A5:${endCol}${4 + data.length}`).format.rowHeight = rowHeight;
  sheet.freezePanes.freezeRows(4);
  sheet.tables.add(`A4:${endCol}${4 + data.length}`, true, tableName);
}

buildSheet(
  mappingSheet,
  '规则证据标签—来源—视图拆分映射表',
  '每条来源独立一行。结构化来源直接对应六类视图；病历来源映射到病历信息后，继续与脱敏病历中的实际 recordName/recordType 匹配。',
  ['mapping_id', 'mdtrtId', 'medinsId', '一级标签名', '二级标签名', '来源级别', '主要来源', '次要来源', '对应的视图', '文书类型', '匹配到的recordName', 'recordType', '匹配状态'],
  mappingRows,
  [13,15,19,13,15,13,27,30,27,14,29,20,48],
  'SplitMappingTable',
  44,
);

buildSheet(
  inventorySheet,
  '脱敏病历实际 recordName 清单',
  '清单直接提取自脱敏病历.md；映射表只允许匹配此处实际存在的 recordName，近似匹配需人工确认。',
  ['recordName', 'recordType'],
  recordInventory,
  [48,24],
  'RecordNameInventoryTable',
  32,
);

buildSheet(
  fieldsSheet,
  '标准字段与脱敏病历字段兼容表',
  '兼容标准病历视图中的 snake_case 字段和脱敏 XML 中的 camelCase 字段。diseName、hilistName 暂不加入查询条件。',
  ['数据视图', '标准字段名', '脱敏病历字段名', '当前用途'],
  fieldRows,
  [23,34,26,50],
  'FieldCompatibilityTable',
  34,
);

mappingSheet.getRange(`F5:F${4 + mappingRows.length}`).conditionalFormats.add('containsText', {
  text: 'PRIMARY', format: { fill: '#E2F0D9', font: { color: '#215E21', bold: true } },
});
mappingSheet.getRange(`F5:F${4 + mappingRows.length}`).conditionalFormats.add('containsText', {
  text: 'SECONDARY', format: { fill: '#FFF2CC', font: { color: '#7F6000', bold: true } },
});

wb.recalculate();
const inspect = await wb.inspect({
  kind: 'table', sheetId: '拆分映射', range: `A1:M${4 + mappingRows.length}`,
  include: 'values', tableMaxRows: 45, tableMaxCols: 13, maxChars: 30000,
});
console.log(inspect.ndjson);

await fs.mkdir(outputDir, { recursive: true });
const preview = await wb.render({ sheetName: '拆分映射', range: `A1:M${4 + mappingRows.length}`, scale: 1, format: 'png' });
await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));
const inventoryPreview = await wb.render({ sheetName: 'recordName清单', range: `A1:B${4 + recordInventory.length}`, scale: 1, format: 'png' });
await fs.writeFile(`${outputDir}/recordName清单预览.png`, new Uint8Array(await inventoryPreview.arrayBuffer()));
const fieldsPreview = await wb.render({ sheetName: '字段名兼容', range: `A1:D${4 + fieldRows.length}`, scale: 1, format: 'png' });
await fs.writeFile(`${outputDir}/字段名兼容预览.png`, new Uint8Array(await fieldsPreview.arrayBuffer()));
const output = await SpreadsheetFile.exportXlsx(wb);
await output.save(outputPath);
console.log(`SAVED ${outputPath}`);
