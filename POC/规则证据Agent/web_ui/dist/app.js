const elements = {
  runStatus: document.querySelector("#runStatus"),
  runStatusText: document.querySelector("#runStatusText"),
  thinkingToggle: document.querySelector("#thinkingToggle"),
  thinkingLabel: document.querySelector("#thinkingLabel"),
  refreshButton: document.querySelector("#refreshButton"),
  runButton: document.querySelector("#runButton"),
  totalCount: document.querySelector("#totalCount"),
  doneCount: document.querySelector("#doneCount"),
  pendingCount: document.querySelector("#pendingCount"),
  visibleCount: document.querySelector("#visibleCount"),
  ruleSearch: document.querySelector("#ruleSearch"),
  ruleList: document.querySelector("#ruleList"),
  detailEmpty: document.querySelector("#detailEmpty"),
  detailContent: document.querySelector("#detailContent"),
  detailRuleId: document.querySelector("#detailRuleId"),
  detailState: document.querySelector("#detailState"),
  editRuleButton: document.querySelector("#editRuleButton"),
  detailRuleText: document.querySelector("#detailRuleText"),
  factCount: document.querySelector("#factCount"),
  factList: document.querySelector("#factList"),
  logicExpression: document.querySelector("#logicExpression"),
  timeRequirements: document.querySelector("#timeRequirements"),
  unresolvedList: document.querySelector("#unresolvedList"),
  ruleEditorSection: document.querySelector("#ruleEditorSection"),
  ruleEditor: document.querySelector("#ruleEditor"),
  downloadRuleButton: document.querySelector("#downloadRuleButton"),
  cancelRuleEditButton: document.querySelector("#cancelRuleEditButton"),
  saveRuleButton: document.querySelector("#saveRuleButton"),
  promptEditor: document.querySelector("#promptEditor"),
  promptState: document.querySelector("#promptState"),
  promptLength: document.querySelector("#promptLength"),
  savePromptButton: document.querySelector("#savePromptButton"),
  toast: document.querySelector("#toast"),
};

let rules = [];
let selectedRuleId = null;
let editingRuleId = null;
let promptDirty = false;
let defaultsApplied = false;
let toastTimer = null;
let agentRunning = false;
let staticMode = false;

function stateLabel(value) {
  if (value === "done") return "已拆解";
  if (value === "error") return "读取失败";
  return "待拆解";
}

function showToast(message, isError = false) {
  clearTimeout(toastTimer);
  elements.toast.textContent = message;
  elements.toast.className = `toast visible${isError ? " error" : ""}`;
  toastTimer = setTimeout(() => { elements.toast.className = "toast"; }, 3200);
}

async function request(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `请求失败：${response.status}`);
  return payload;
}

async function loadState({ silent = false } = {}) {
  if (staticMode && silent) return;
  try {
    let state;
    if (staticMode) {
      state = await request(`/data/state.json?v=${Date.now()}`);
    } else {
      try {
        state = await request("/api/state");
      } catch (apiError) {
        state = await request(`/data/state.json?v=${Date.now()}`);
        staticMode = true;
        showToast("已进入云端审阅模式：修改后请下载 JSON");
      }
    }
    rules = state.rules;
    renderCounts(state.counts);
    renderRunState(state.run);
    renderRules();

    if (!defaultsApplied) {
      elements.thinkingToggle.checked = state.defaults.thinking;
      updateThinkingLabel();
      defaultsApplied = true;
    }
    if (!promptDirty) {
      elements.promptEditor.value = state.prompt;
      updatePromptLength();
    }
    const selectedStillExists = rules.some((rule) => rule.rule_id === selectedRuleId);
    if (!selectedStillExists) {
      selectedRuleId = rules.find((rule) => rule.state === "done")?.rule_id || rules[0]?.rule_id || null;
    }
    renderRules();
    renderDetail();
  } catch (error) {
    if (!silent) showToast(error.message, true);
    elements.runStatus.dataset.state = "failed";
    elements.runStatusText.textContent = "状态读取失败";
  }
}

function renderCounts(counts) {
  elements.totalCount.textContent = counts.total;
  elements.doneCount.textContent = counts.done;
  elements.pendingCount.textContent = counts.pending;
}

function renderRunState(run) {
  if (staticMode) {
    agentRunning = false;
    elements.runStatus.dataset.state = "idle";
    elements.runStatusText.textContent = "云端审阅模式";
    elements.runButton.disabled = true;
    elements.runButton.textContent = "本地运行 Agent";
    elements.runButton.title = "云端静态页面仅用于审阅；请在本地运行 Agent";
    elements.savePromptButton.disabled = true;
    elements.savePromptButton.title = "云端静态页面不保存提示词";
    elements.promptEditor.readOnly = true;
    elements.promptState.textContent = "只读快照";
    elements.thinkingToggle.disabled = true;
    document.querySelectorAll('input[name="writeMode"]').forEach((input) => { input.disabled = true; });
    elements.saveRuleButton.disabled = true;
    elements.saveRuleButton.title = "云端修改请使用“下载 JSON”带回文件";
    return;
  }

  elements.runStatus.dataset.state = run.status;
  const labels = {
    idle: "等待运行",
    running: "正在拆解规则",
    completed: run.elapsed_seconds == null ? "运行完成" : `运行完成 · ${run.elapsed_seconds.toFixed(2)} 秒`,
    failed: "运行失败",
  };
  elements.runStatusText.textContent = labels[run.status] || run.message;
  const running = run.status === "running";
  agentRunning = running;
  elements.runButton.disabled = running;
  elements.savePromptButton.disabled = running;
  elements.saveRuleButton.disabled = running;
  elements.runButton.removeAttribute("title");
  elements.savePromptButton.removeAttribute("title");
  elements.saveRuleButton.removeAttribute("title");
  elements.promptEditor.readOnly = false;
  elements.runButton.textContent = running ? "Agent 运行中" : "运行拆解 Agent";
  if (run.status === "failed" && run.message) {
    elements.runStatusText.title = run.message;
  } else {
    elements.runStatusText.removeAttribute("title");
  }
}

function filteredRules() {
  const keyword = elements.ruleSearch.value.trim().toLowerCase();
  if (!keyword) return rules;
  return rules.filter((rule) => `${rule.rule_id} ${rule.rule_text}`.toLowerCase().includes(keyword));
}

function renderRules() {
  const visibleRules = filteredRules();
  elements.visibleCount.textContent = `${visibleRules.length} 条`;
  elements.ruleList.replaceChildren();
  for (const rule of visibleRules) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `rule-item${rule.rule_id === selectedRuleId ? " active" : ""}`;
    button.addEventListener("click", () => {
      if (rule.rule_id !== selectedRuleId) {
        editingRuleId = null;
        elements.ruleEditorSection.hidden = true;
      }
      selectedRuleId = rule.rule_id;
      renderRules();
      renderDetail();
    });

    const meta = document.createElement("div");
    meta.className = "rule-meta";
    const id = document.createElement("span");
    id.className = "rule-id";
    id.textContent = rule.rule_id;
    const badge = document.createElement("span");
    badge.className = `badge class-${rule.state === "done" ? "y" : rule.state === "error" ? "n" : "pending"}`;
    badge.textContent = stateLabel(rule.state);
    meta.append(id, badge);

    const preview = document.createElement("p");
    preview.className = "rule-preview";
    preview.textContent = rule.rule_text;
    button.append(meta, preview);
    elements.ruleList.append(button);
  }
}

function renderDetail() {
  const rule = rules.find((item) => item.rule_id === selectedRuleId);
  if (!rule) {
    elements.detailEmpty.hidden = false;
    elements.detailContent.hidden = true;
    return;
  }

  elements.detailEmpty.hidden = true;
  elements.detailContent.hidden = false;
  elements.detailRuleId.textContent = rule.rule_id;
  elements.detailRuleText.textContent = rule.rule_text;
  elements.detailState.className = `badge class-${rule.state === "done" ? "y" : rule.state === "error" ? "n" : "pending"}`;
  elements.detailState.textContent = stateLabel(rule.state);
  elements.factList.replaceChildren();
  elements.unresolvedList.replaceChildren();
  const isEditingSelectedRule = editingRuleId === rule.rule_id;
  elements.ruleEditorSection.hidden = !isEditingSelectedRule;
  elements.editRuleButton.disabled = agentRunning || rule.state !== "done";

  if (rule.state !== "done" || !rule.result) {
    elements.factCount.textContent = "尚无结果";
    const message = document.createElement("p");
    message.className = "rule-preview";
    message.textContent = rule.state === "error" ? `结果文件读取失败：${rule.error}` : "该规则还没有生成拆解结果。";
    elements.factList.append(message);
    elements.logicExpression.textContent = "—";
    elements.timeRequirements.textContent = "—";
    const item = document.createElement("li");
    item.textContent = "—";
    elements.unresolvedList.append(item);
    return;
  }

  const result = rule.result;
  const facts = Array.isArray(result.facts) ? result.facts : [];
  elements.factCount.textContent = `${facts.length} 个事实`;
  for (const fact of facts) {
    const item = document.createElement("div");
    item.className = "fact-item";
    const id = document.createElement("code");
    id.textContent = fact.fact_id || "—";
    const description = document.createElement("p");
    description.textContent = fact.description || "—";
    item.append(id, description);
    elements.factList.append(item);
  }
  elements.logicExpression.textContent = result.logical_expression || "—";
  elements.timeRequirements.textContent = result.time_requirements || "—";
  const unresolved = Array.isArray(result.unresolved) && result.unresolved.length ? result.unresolved : ["无"];
  for (const value of unresolved) {
    const item = document.createElement("li");
    item.textContent = value;
    elements.unresolvedList.append(item);
  }
}

function openRuleEditor() {
  const rule = rules.find((item) => item.rule_id === selectedRuleId);
  if (!rule || rule.state !== "done" || !rule.result) return;
  editingRuleId = rule.rule_id;
  elements.ruleEditor.value = JSON.stringify(rule.result, null, 2);
  elements.ruleEditorSection.hidden = false;
  elements.ruleEditor.focus();
}

async function saveRule() {
  const rule = rules.find((item) => item.rule_id === selectedRuleId);
  if (!rule) return;
  let edited;
  try {
    edited = JSON.parse(elements.ruleEditor.value);
  } catch (error) {
    showToast(`JSON 格式错误：${error.message}`, true);
    return;
  }
  elements.saveRuleButton.disabled = true;
  try {
    await request(`/api/rules/${encodeURIComponent(rule.rule_id)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ result: edited }),
    });
    showToast("规则拆解结果已保存");
    editingRuleId = null;
    elements.ruleEditorSection.hidden = true;
    await loadState({ silent: true });
  } catch (error) {
    showToast(error.message, true);
  } finally {
    elements.saveRuleButton.disabled = false;
  }
}

function downloadRule() {
  const rule = rules.find((item) => item.rule_id === selectedRuleId);
  if (!rule) return;

  let edited;
  try {
    edited = JSON.parse(elements.ruleEditor.value);
  } catch (error) {
    showToast(`JSON 格式错误：${error.message}`, true);
    return;
  }

  const blob = new Blob([`${JSON.stringify(edited, null, 2)}\n`], {
    type: "application/json;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `${rule.rule_id}.json`;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
  showToast(`已下载 ${anchor.download}`);
}

function updateThinkingLabel() {
  elements.thinkingLabel.textContent = elements.thinkingToggle.checked ? "开启" : "关闭";
}

function updatePromptLength() {
  elements.promptLength.textContent = `${elements.promptEditor.value.length} 字`;
}

async function savePrompt() {
  const prompt = elements.promptEditor.value.trim();
  if (!prompt) {
    showToast("系统提示词不能为空", true);
    return false;
  }
  elements.savePromptButton.disabled = true;
  try {
    await request("/api/prompt", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
    });
    promptDirty = false;
    elements.promptState.textContent = "已保存";
    elements.promptState.className = "save-state";
    showToast("提示词已保存");
    return true;
  } catch (error) {
    showToast(error.message, true);
    return false;
  } finally {
    elements.savePromptButton.disabled = false;
  }
}

async function runAgent() {
  if (promptDirty && !(await savePrompt())) return;
  const writeMode = document.querySelector('input[name="writeMode"]:checked').value;
  elements.runButton.disabled = true;
  try {
    await request("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thinking: elements.thinkingToggle.checked, write_mode: writeMode }),
    });
    showToast("Agent 已启动");
    await loadState({ silent: true });
  } catch (error) {
    showToast(error.message, true);
    elements.runButton.disabled = false;
  }
}

elements.thinkingToggle.addEventListener("change", updateThinkingLabel);
elements.ruleSearch.addEventListener("input", renderRules);
elements.refreshButton.addEventListener("click", () => loadState());
elements.runButton.addEventListener("click", runAgent);
elements.editRuleButton.addEventListener("click", openRuleEditor);
elements.downloadRuleButton.addEventListener("click", downloadRule);
elements.cancelRuleEditButton.addEventListener("click", () => {
  editingRuleId = null;
  elements.ruleEditorSection.hidden = true;
});
elements.saveRuleButton.addEventListener("click", saveRule);
elements.savePromptButton.addEventListener("click", savePrompt);
elements.promptEditor.addEventListener("input", () => {
  promptDirty = true;
  elements.promptState.textContent = "未保存";
  elements.promptState.className = "save-state dirty";
  updatePromptLength();
});

loadState();
setInterval(() => loadState({ silent: true }), 2000);

function registerWebMcpTools() {
  const context = document.modelContext;
  if (!context?.registerTool) return;
  const reportError = (error) => console.warn("WebMCP tool registration failed", error);

  Promise.resolve(context.registerTool({
    name: "read_rule_decomposition_workspace",
    title: "读取规则拆解工作台",
    description: "读取当前规则数量、运行状态和已有拆解结果摘要。",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true, untrustedContentHint: false },
    async execute() {
      const state = await request("/api/state");
      return {
        counts: state.counts,
        run: state.run,
        rules: state.rules.map((rule) => ({
          rule_id: rule.rule_id,
          state: rule.state,
          logical_expression: rule.result?.logical_expression || null,
        })),
      };
    },
  })).catch(reportError);

  Promise.resolve(context.registerTool({
    name: "save_rule_system_prompt",
    title: "保存规则拆解提示词",
    description: "更新规则拆解 Agent 下一次运行使用的系统提示词。",
    inputSchema: {
      type: "object",
      properties: { prompt: { type: "string", minLength: 1 } },
      required: ["prompt"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: false, untrustedContentHint: false },
    async execute(input) {
      if (!input || typeof input.prompt !== "string" || !input.prompt.trim()) throw new Error("prompt 不能为空");
      const result = await request("/api/prompt", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: input.prompt }),
      });
      await loadState({ silent: true });
      return result;
    },
  })).catch(reportError);

  Promise.resolve(context.registerTool({
    name: "start_rule_decomposition",
    title: "运行规则拆解 Agent",
    description: "按指定思考模式和写入方式启动本地规则拆解。",
    inputSchema: {
      type: "object",
      properties: {
        thinking: { type: "boolean" },
        write_mode: { type: "string", enum: ["append", "overwrite"] },
      },
      required: ["thinking", "write_mode"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: false, untrustedContentHint: false },
    async execute(input) {
      if (!input || typeof input.thinking !== "boolean" || !["append", "overwrite"].includes(input.write_mode)) {
        throw new Error("thinking 或 write_mode 不合法");
      }
      const result = await request("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input),
      });
      await loadState({ silent: true });
      return result;
    },
  })).catch(reportError);
}

registerWebMcpTools();
