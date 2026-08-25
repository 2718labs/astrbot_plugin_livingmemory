/**
 * Utils - 纯工具函数集
 * 提供数据处理、UI 渲染相关的工具方法
 */

/**
 * 标准化重要性值（支持 0-1 和 0-10 两种输入）
 * @param {number} value - 输入值
 * @returns {number} 0-10 范围的重要性值
 */
export function normalizeImportance(value) {
  let n = Number(value);
  if (!Number.isFinite(n)) n = 0.5;
  if (n <= 1) n *= 10;
  return Math.min(10, Math.max(0, n));
}

/**
 * 子事实 importance 自加权（Σ(imp²)/Σ(imp)）：强事实主导但不独占，弱事实也参与。
 * 只用于父记忆条目的显示口径，不改动存储值与排序。
 * @param {Array} facts - fact 对象数组（含 importance 0-1 字段）
 * @param {number} fallback - 无有效 fact 时回退的 0-1 值
 * @returns {number} 0-1 加权值；无有效输入时返回 fallback
 */
export function weightedImportance(facts, fallback) {
  let weightedSum = 0;
  let weightSum = 0;
  for (const fact of Array.isArray(facts) ? facts : []) {
    const value = fact && fact.importance;
    const n = Number(value);
    if (!Number.isFinite(n)) continue;
    const imp = Math.min(1, Math.max(0, n));
    weightedSum += imp * imp;
    weightSum += imp;
  }
  if (weightSum <= 0) {
    const fb = Number(fallback);
    return Number.isFinite(fb) ? Math.min(1, Math.max(0, fb)) : 0.5;
  }
  return weightedSum / weightSum;
}

/**
 * 统一显示时间：兼容秒/毫秒时间戳、ISO 字符串和旧 SQLite 微秒字符串。
 * @param {number|string|Date|null|undefined} value - 时间值
 * @param {string} fallback - 空值回退文本
 * @returns {string} 本地 YYYY-MM-DD HH:mm:ss
 */
export function formatTimestamp(value, fallback = "--") {
  if (value == null || value === "") return fallback;

  const text = String(value).trim();
  const dateOnly = text.match(/^(\d{4}-\d{2}-\d{2})$/);
  if (dateOnly) return dateOnly[1];

  // SQLite/Python 的无时区 datetime 存量值按本地墙钟时间展示，
  // 只去掉微秒，不让浏览器把它误判成 UTC。
  const naive = text.match(
    /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.\d+)?$/
  );
  if (naive) return naive[1] + " " + naive[2];

  let date;
  if (value instanceof Date) {
    date = value;
  } else if (/^-?\d+(?:\.\d+)?$/.test(text)) {
    const numeric = Number(text);
    const milliseconds = Math.abs(numeric) < 1e12 ? numeric * 1000 : numeric;
    date = new Date(milliseconds);
  } else {
    date = new Date(text);
  }
  if (Number.isNaN(date.getTime())) return text || fallback;

  const pad = number => String(number).padStart(2, "0");
  return [
    date.getFullYear(),
    pad(date.getMonth() + 1),
    pad(date.getDate())
  ].join("-") + " " + [
    pad(date.getHours()),
    pad(date.getMinutes()),
    pad(date.getSeconds())
  ].join(":");
}

/**
 * 从记忆详情对象中提取文本内容
 * @param {Object} detail - 记忆详情对象
 * @returns {string} 文本内容
 */
export function getDetailText(detail) {
  return detail.summary || detail.text || detail.content || "";
}

/**
 * HTML 转义，防止 XSS
 * @param {string} text - 原始文本
 * @returns {string} 转义后的 HTML 安全文本
 */
export function esc(text) {
  const div = document.createElement("div");
  div.textContent = String(text);
  return div.innerHTML;
}

/**
 * 渲染状态标签 pill
 * @param {string} status - 状态值（active/archived/deleted）
 * @returns {string} HTML 字符串
 */
export function statusPill(status) {
  const label = statusLabel(status);
  const cls = ["active", "archived", "deleted"].includes(status) ? status : "active";
  return '<span class="status-pill ' + cls + '">' + esc(label) + '</span>';
}

/**
 * 获取状态的显示文本
 * @param {string} status - 状态值
 * @returns {string} 显示文本
 */
export function statusLabel(status) {
  if (status === "active") return window.t("status.active");
  if (status === "archived") return window.t("status.archived");
  if (status === "deleted") return window.t("status.deleted");
  return status || "--";
}

/**
 * 获取记忆类型的显示文本
 * @param {string} type - 类型值
 * @returns {string} 显示文本
 */
export function typeLabel(type) {
  const normalized = String(type || "").toUpperCase();
  if (normalized === "GENERAL") return window.t("type.general");
  if (normalized === "FACT") return window.t("type.fact");
  if (normalized === "FACTUAL") return window.t("type.factual");
  if (normalized === "PREFERENCE") return window.t("type.preference");
  if (normalized === "EVENT") return window.t("type.event");
  if (normalized === "EPISODIC") return window.t("type.episodic");
  if (normalized === "RELATIONAL") return window.t("type.relational");
  if (normalized === "PLANNED") return window.t("type.planned");
  if (normalized === "OPINION") return window.t("type.opinion");
  return type || "GENERAL";
}

/**
 * 渲染图节点类型徽章
 * @param {string} type - 节点类型
 * @returns {string} HTML 字符串
 */
export function nodeBadge(type) {
  const normalized = String(type || "unknown").toLowerCase();
  return '<span class="peek-node-badge ' + esc(normalized) + '">' + esc(type || "Unknown") + '</span>';
}

/**
 * 渲染元数据项（label: value）
 * @param {string} label - 标签
 * @param {string} value - 值
 * @returns {string} HTML 字符串
 */
export function metaItem(label, value) {
  return '<div class="memory-detail-meta-item"><span class="memory-detail-meta-label">' + esc(label) + '</span><span class="memory-detail-meta-value">' + value + '</span></div>';
}

/**
 * 防抖函数
 * @param {Function} fn - 要防抖的函数
 * @param {number} ms - 防抖延迟（毫秒）
 * @returns {Function} 防抖后的函数
 */
export function debounce(fn, ms) {
  let timer;
  return function(...args) {
    clearTimeout(timer);
    timer = setTimeout(() => fn.apply(this, args), ms);
  };
}

/**
 * 获取 Atom 类型的显示文本
 * @param {string} type - Atom 类型
 * @returns {string} 显示文本
 */
export function atomLabel(type) {
  const normalized = String(type || "").toLowerCase();
  const labels = {
    factual: window.t("system.atomFactual"),
    episodic: window.t("system.atomEpisodic"),
    relational: window.t("system.atomRelational"),
    planned: window.t("system.atomPlanned"),
    entity: window.t("atom.entity"),
    event: window.t("atom.event"),
    preference: window.t("atom.preference"),
    topic: window.t("atom.topic"),
  };
  return labels[normalized] || type || "--";
}
