const csrf = document.querySelector('meta[name="c2a-csrf"]').content;

const byId = (id) => document.getElementById(id);
const form = byId("conversion-form");
const fileInput = byId("source-file");
const dropZone = byId("drop-zone");
const dropTitle = byId("drop-title");
const analyzeButton = byId("analyze");
const convertButton = byId("convert");
const consent = byId("consent");
const analysisCard = byId("analysis-card");
const options = byId("conversion-options");
const emptyState = byId("empty-state");
const jobView = byId("job-view");
const toastElement = byId("toast");
let analyzedInput = "";
let analyzedEligible = false;
let activeJob = "";
let eventStream = null;

const statusLabels = {
  queued: "排队中",
  running: "运行中",
  verified: "已验证",
  build_only: "仅构建",
  blocked: "外部阻断",
  failed: "失败",
};

const capabilityLabels = {
  search: "搜索",
  popular: "热门",
  latest: "最新",
  details: "详情",
  chapters: "章节",
  pages: "页面",
  filters: "筛选",
  settings: "设置",
  deep_links: "深链",
  json_api: "JSON API",
  dynamic_base_urls: "动态域名",
  image_headers: "图片请求头",
};

const assessmentLabels = {
  ready: "适合自动转换",
  caution: "可以转换，需关注风险",
  blocked: "未通过付费前检查",
};

const assessmentFindingLabels = {
  "APK behavior is recovered from JADX output and requires live validation":
    "APK 行为来自 JADX 反编译，必须进行实站验证",
  "authenticated and Android-only features are outside public-reading scope":
    "登录功能和 Android 专用行为不在公开阅读范围内",
  "dynamic filters require an additional live endpoint": "动态筛选依赖额外的实站接口",
  "runtime domain selection must remain inside a recovered allowlist":
    "运行时域名必须限制在已恢复的白名单内",
  "page images require source-specific request behavior": "漫画图片需要源站专用请求行为",
  "filter capability was detected but no stable filter contract was recovered":
    "检测到筛选功能，但未恢复出稳定的筛选契约",
  "settings were detected but no focused settings evidence was recovered":
    "检测到源设置，但没有恢复出聚焦的设置证据",
  "no search, popular, or latest listing entry point was recovered":
    "没有恢复出搜索、热门或最新列表入口",
  "analysis produced no source evidence files": "分析没有产生可用的源码证据",
};

function assessmentFinding(message) {
  if (assessmentFindingLabels[message]) return assessmentFindingLabels[message];
  const optional = message.match(/^(\d+) optional features will be excluded$/);
  if (optional) return `${optional[1]} 项可选功能会被排除`;
  const omitted = message.match(/^(\d+) source files exceed the bounded generation context$/);
  if (omitted) return `${omitted[1]} 个源码文件超出生成上下文上限`;
  const missing = message.match(/^missing core reading behavior: (.+)$/);
  if (missing) {
    const labels = missing[1].split(", ").map((item) => capabilityLabels[item] || item);
    return `缺少核心阅读行为：${labels.join("、")}`;
  }
  const crypto = message.match(/^supported cryptography still increases implementation risk: (.+)$/);
  if (crypto) return `受支持的加密仍会增加实现风险：${crypto[1]}`;
  return message;
}

function toast(message, error = false) {
  toastElement.textContent = message;
  toastElement.classList.toggle("error", error);
  toastElement.classList.remove("hidden");
  window.clearTimeout(toastElement._timer);
  toastElement._timer = window.setTimeout(() => toastElement.classList.add("hidden"), 4200);
}

async function api(url, options = {}) {
  const method = options.method || "GET";
  const headers = new Headers(options.headers || {});
  if (!["GET", "HEAD"].includes(method.toUpperCase())) headers.set("X-C2A-CSRF", csrf);
  const response = await fetch(url, { ...options, headers });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = typeof payload === "object" ? payload.detail : payload;
    throw new Error(detail || `请求失败：HTTP ${response.status}`);
  }
  return payload;
}

function setBusy(button, busy, busyText, normalText) {
  button.disabled = busy;
  button.textContent = busy ? busyText : normalText;
}

function updateFileLabel() {
  const file = fileInput.files[0];
  dropTitle.textContent = file ? file.name : "选择或拖入 Tachi APK";
}

fileInput.addEventListener("change", updateFileLabel);
["dragenter", "dragover"].forEach((name) => dropZone.addEventListener(name, (event) => {
  event.preventDefault();
  dropZone.classList.add("dragging");
}));
["dragleave", "drop"].forEach((name) => dropZone.addEventListener(name, (event) => {
  event.preventDefault();
  dropZone.classList.remove("dragging");
}));
dropZone.addEventListener("drop", (event) => {
  if (event.dataTransfer.files.length) {
    fileInput.files = event.dataTransfer.files;
    updateFileLabel();
  }
});

function renderAnalysis(payload) {
  const source = payload.source;
  const assessment = payload.assessment;
  analyzedInput = payload.input_ref;
  analyzedEligible = assessment.eligible;
  byId("source-id").textContent = source.id;
  byId("source-name").textContent = source.name;
  byId("source-format").textContent = source.format === "decompiled_apk" ? "APK / JADX" : "Kotlin module";
  byId("source-url").textContent = source.base_url;
  byId("source-files").textContent = String(source.files);
  byId("source-filters").textContent = String(source.filters);
  byId("source-license").textContent = source.license || "未发现";
  const assessmentSummary = byId("assessment-summary");
  assessmentSummary.className = `assessment-summary ${assessment.status}`;
  byId("assessment-label").textContent = assessmentLabels[assessment.status] || assessment.status;
  byId("assessment-score").textContent = `${assessment.score}/100`;
  const budget = assessment.token_budget;
  const tokenRange = (minimum, maximum) =>
    `${Number(minimum).toLocaleString()}–${Number(maximum).toLocaleString()}`;
  byId("token-budget").classList.toggle("hidden", !budget);
  if (budget) {
    byId("initial-token-budget").textContent = tokenRange(
      budget.initial_prompt_tokens_min,
      budget.initial_prompt_tokens_max,
    );
    byId("total-token-budget").textContent = tokenRange(
      budget.recommended_total_tokens_min,
      budget.recommended_total_tokens_max,
    );
  }
  const findings = byId("assessment-findings");
  findings.replaceChildren();
  [...assessment.blockers, ...assessment.risks].slice(0, 4).forEach((message) => {
    const item = document.createElement("li");
    item.textContent = assessmentFinding(message);
    findings.appendChild(item);
  });
  findings.classList.toggle("hidden", findings.childElementCount === 0);
  const capabilities = byId("capabilities");
  capabilities.replaceChildren();
  source.capabilities.forEach((item) => {
    const badge = document.createElement("span");
    badge.textContent = capabilityLabels[item] || item;
    capabilities.appendChild(badge);
  });
  const messages = [...source.warnings, ...source.unsupported_features];
  const notice = byId("source-notice");
  notice.classList.toggle("hidden", messages.length === 0);
  notice.textContent = messages.join(" · ");
  byId("output").value = payload.suggested_output;
  analysisCard.classList.remove("hidden");
  options.classList.remove("hidden");
  options.scrollIntoView({ behavior: "smooth", block: "nearest" });
  convertButton.disabled = !consent.checked || !analyzedEligible;
  convertButton.textContent = analyzedEligible
    ? "开始生成 Aidoku 源"
    : "当前源未通过付费前检查";
}

analyzeButton.addEventListener("click", async () => {
  const data = new FormData();
  const file = fileInput.files[0];
  if (file) data.append("source_file", file);
  data.append("input_ref", byId("input-ref").value.trim());
  setBusy(analyzeButton, true, "正在分析…", "分析输入，不消耗 AI tokens");
  try {
    renderAnalysis(await api("/api/analyze", { method: "POST", body: data }));
    toast("分析完成，可以开始转换");
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(analyzeButton, false, "正在分析…", "分析输入，不消耗 AI tokens");
  }
});

consent.addEventListener("change", () => {
  convertButton.disabled = !consent.checked || !analyzedInput || !analyzedEligible;
});

byId("ai-check").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  setBusy(button, true, "正在连接…", "测试 AI 连接");
  try {
    const result = await api("/api/ai-check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_url: byId("base-url").value, model: byId("model").value }),
    });
    toast(`已连接 ${result.model} · ${result.structured_output ? "JSON Schema" : "JSON fallback"}`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(button, false, "正在连接…", "测试 AI 连接");
  }
});

function renderDoctor(payload) {
  const grid = byId("doctor-grid");
  grid.replaceChildren();
  payload.items.forEach((item) => {
    const card = document.createElement("div");
    card.className = `status-item ${item.available ? "ready" : "missing"}`;
    const dot = document.createElement("span");
    dot.className = "status-dot";
    const copy = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = item.name;
    const detail = document.createElement("small");
    detail.textContent = item.detail || (item.available ? "已就绪" : "缺失");
    copy.append(name, detail);
    card.append(dot, copy);
    grid.appendChild(card);
  });
}

byId("refresh-doctor").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  setBusy(button, true, "检查中…", "重新检查");
  try {
    const result = await api("/api/doctor");
    renderDoctor(result);
    toast(result.ready ? "环境全部就绪" : "仍有组件缺失，请重新运行安装脚本", !result.ready);
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(button, false, "检查中…", "重新检查");
  }
});

function progressFor(job) {
  if (["verified", "build_only", "blocked", "failed"].includes(job.status)) return 100;
  const text = `${job.message} ${job.logs.join(" ")}`;
  if (text.includes("validation passed")) return 92;
  if (text.includes("AI repair") || text.includes("Repair")) return 72;
  if (text.includes("validation failed") || text.includes("validating")) return 58;
  if (text.includes("returned")) return 46;
  if (text.includes("initial AI") || text.includes("generation")) return 24;
  return job.status === "running" ? 12 : 5;
}

function artifactLabel(name) {
  return { package: "下载 package.aix", report_md: "查看 Markdown 报告", report_json: "下载 JSON 报告" }[name] || name;
}

function renderJob(job) {
  activeJob = job.id;
  emptyState.classList.add("hidden");
  jobView.classList.remove("hidden");
  const state = byId("job-state");
  state.textContent = statusLabels[job.status] || job.status;
  state.className = `job-state ${job.status}`;
  byId("current-message").textContent = job.message;
  byId("progress-bar").style.width = `${progressFor(job)}%`;
  byId("metric-rounds").textContent = String(job.ai_rounds);
  byId("metric-tokens").textContent = Number(job.total_tokens).toLocaleString();
  byId("metric-status").textContent = statusLabels[job.status] || job.status;
  const log = byId("job-log");
  log.replaceChildren();
  job.logs.forEach((message) => {
    const item = document.createElement("li");
    item.textContent = message;
    log.appendChild(item);
  });
  const error = byId("job-error");
  error.classList.toggle("hidden", !job.error);
  error.textContent = job.error || "";
  const artifacts = byId("artifacts");
  const links = byId("artifact-links");
  links.replaceChildren();
  Object.entries(job.artifacts).forEach(([name, url]) => {
    const link = document.createElement("a");
    link.href = url;
    link.textContent = artifactLabel(name);
    const arrow = document.createElement("span");
    arrow.textContent = "↓";
    link.appendChild(arrow);
    links.appendChild(link);
  });
  artifacts.classList.toggle("hidden", Object.keys(job.artifacts).length === 0);
  byId("resume").classList.toggle("hidden", !["failed", "blocked"].includes(job.status));
}

function watchJob(jobId) {
  if (eventStream) eventStream.close();
  eventStream = new EventSource(`/api/jobs/${jobId}/events`);
  eventStream.onmessage = (event) => {
    const job = JSON.parse(event.data);
    renderJob(job);
    if (["verified", "build_only", "blocked", "failed"].includes(job.status)) {
      eventStream.close();
      toast(`转换结束：${statusLabels[job.status]}`, job.status === "failed");
      setBusy(convertButton, false, "正在启动…", "开始生成 Aidoku 源");
    }
  };
  eventStream.onerror = () => {
    if (eventStream) eventStream.close();
  };
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!analyzedInput || !consent.checked) return;
  setBusy(convertButton, true, "正在启动…", "开始生成 Aidoku 源");
  const payload = {
    input_ref: analyzedInput,
    output: byId("output").value,
    base_url: byId("base-url").value,
    model: byId("model").value,
    query: byId("query").value || null,
    proxy: byId("proxy").value || null,
    generation_reasoning: byId("generation-reasoning").value,
    repair_reasoning: byId("repair-reasoning").value,
    live: byId("live").checked,
    force: byId("force").checked,
    resume: false,
    consent: true,
  };
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    renderJob(job);
    watchJob(job.id);
    byId("progress-title").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    setBusy(convertButton, false, "正在启动…", "开始生成 Aidoku 源");
    toast(error.message, true);
  }
});

byId("resume").addEventListener("click", async (event) => {
  if (!activeJob) return;
  const button = event.currentTarget;
  setBusy(button, true, "正在恢复…", "从检查点继续");
  try {
    const job = await api(`/api/jobs/${activeJob}/resume`, { method: "POST" });
    renderJob(job);
    watchJob(job.id);
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(button, false, "正在恢复…", "从检查点继续");
  }
});

api("/api/jobs").then((jobs) => {
  if (jobs.length) {
    renderJob(jobs[0]);
    if (["queued", "running"].includes(jobs[0].status)) watchJob(jobs[0].id);
  }
}).catch(() => {});
