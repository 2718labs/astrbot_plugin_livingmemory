/* ================================================================
   graph-shared.js — 图谱渲染共享常量与工具
   供 graph-renderer.js / graph-interaction.js / graph-2d.js 共用，
   挂载到全局 GraphShared。
   ================================================================ */
(function(global) {
  "use strict";

  var CFG = {
    NODE_RADIUS_MIN: 3.4,
    NODE_RADIUS_MAX: 12,
NODE_RADIUS_BASE: 3.8,
    NODE_DEGREE_GAIN: 1.25,
NODE_LEAF_SCALE: 0.78,
    NODE_LEAF_OPACITY: 0.68,
    PERSON_NODE_SCALE: 0.92,
    NODE_FONT_SIZE: 11,
    NODE_META_SIZE: 9,
    NODE_FONT_ZOOM_WEIGHT: 0.25,
    NODE_FONT_SIZE_MIN: 10,
    NODE_FONT_SIZE_MAX: 15,
    NODE_META_SIZE_MIN: 8,
    NODE_META_SIZE_MAX: 12,
    EDGE_WIDTH_DEFAULT: 0.7,
    EDGE_WIDTH_ACTIVE: 1.1,
    EDGE_WIDTH_HIGHLIGHT: 1.7,
    EDGE_OPACITY_DEFAULT: 0.22,
    EDGE_OPACITY_ACTIVE: 0.5,
    EDGE_OPACITY_HIGHLIGHT: 0.76,
    PARTICLE_COUNT_DEFAULT: 1,
    PARTICLE_COUNT_ACTIVE: 2,
    PARTICLE_COUNT_HIGHLIGHT: 3,
    PARTICLE_SPEED: 0.12,
    PARTICLE_SIZE: 1.45,
    /* Force-directed layout - optimized for natural clustering */
    FORCE_ITERATIONS: 400,
    FORCE_REPULSION: 1680,
    FORCE_LINK_DISTANCE: 108,
    FORCE_LINK_STRENGTH: 0.032,
    FORCE_GRAVITY: 0.0095,
    FORCE_DAMPING: 0.82,
    FORCE_MAX_SPEED: 15,
    /* Center node is larger */
    CENTER_SCALE: 1.15,
    CENTER_MAX_RADIUS: 12,
    /* Animation */
    ANIM_SPEED: 0.075,
    IDLE_DAMPING: 0.05,
    ZOOM_MIN: 0.06,
    ZOOM_MAX: 3.5,
    ZOOM_STEP: 0.001,
    DPR_MAX: 2,
    HOVER_RADIUS: 8,
    LARGE_NODE_THRESHOLD: 1200,
    LARGE_EDGE_THRESHOLD: 4500,
    MASSIVE_NODE_THRESHOLD: 3500,
    MASSIVE_EDGE_THRESHOLD: 12000,
    AMBIENT_NODE_LIMIT: 700,
    AMBIENT_EDGE_LIMIT: 1800,
    PROGRESSIVE_LAYOUT_THRESHOLD: 60,
  };

  var TYPE_COLORS = {
    topic: "#78a94b", person: "#2a9e96", fact: "#c58c2a",
    summary: "#df6d62", other: "#74868a",
  };

  function isDark() {
    return (document.documentElement.getAttribute("data-theme") || "light") === "dark";
  }

  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }
  function lerp(a, b, t) { return a + (b - a) * t; }

  function performanceTier(nodeCount, edgeCount) {
    if (nodeCount >= CFG.MASSIVE_NODE_THRESHOLD || edgeCount >= CFG.MASSIVE_EDGE_THRESHOLD) return 2;
    if (nodeCount >= CFG.LARGE_NODE_THRESHOLD || edgeCount >= CFG.LARGE_EDGE_THRESHOLD) return 1;
    return 0;
  }

  function themeColor(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }

  function hexToRgba(h, alpha) {
    var v = String(h || "#000").replace("#", "").trim();
    v = v.length === 3 ? v.split("").map(function(c) { return c + c; }).join("") : v.padEnd(6, "0").slice(0, 6);
    var r = parseInt(v.slice(0, 2), 16), g = parseInt(v.slice(2, 4), 16), b = parseInt(v.slice(4, 6), 16);
    return "rgba(" + r + "," + g + "," + b + "," + clamp(alpha, 0, 1) + ")";
  }

  function getPos(e, el) {
    var rect = el.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  function easeInOutCubic(t) {
    return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  }

  function pointToSegmentDistance(px, py, x1, y1, x2, y2) {
    var dx = x2 - x1;
    var dy = y2 - y1;
    var len2 = dx * dx + dy * dy;
    if (!len2) return Math.sqrt((px - x1) ** 2 + (py - y1) ** 2);
    var t = clamp(((px - x1) * dx + (py - y1) * dy) / len2, 0, 1);
    var x = x1 + t * dx;
    var y = y1 + t * dy;
    return Math.sqrt((px - x) ** 2 + (py - y) ** 2);
  }

  function codePointLength(value) {
    return Array.from(String(value || "")).length;
  }

  function cleanFactLabel(value) {
    var text = String(value || "")
      .replace(/\*\*/g, "")
      .replace(/\s+/g, " ")
      .trim();
    text = text.replace(
      /^(?:\d{4}(?:[-/.]\d{1,2}){2}|\d{4}年\d{1,2}月\d{1,2}日?)[^，,：:]{0,20}[，,：:]\s*/,
      ""
    );
    var reportingVerb = "(?:告诉|询问|提到|谈到|讨论|表示|回应|答应|确认|提醒|解释|允许|拒绝|认为|回忆|承认|建议|要求|明确|说)";
    text = text.replace(
      new RegExp("^(?:[A-Za-z][A-Za-z0-9_.-]{0,31}|我)(?=" + reportingVerb + ")"),
      ""
    );
    text = text.replace(
      new RegExp("^(" + reportingVerb + ")[A-Za-z][A-Za-z0-9_.-]{0,31}(?=[\\u3400-\\u9fff])"),
      "$1"
    );
    return text
      .replace(/([我你她他])(?:[（(][^）)]{1,8}[）)])/g, "$1")
      .replace(/[“”‘’'\"]/g, "")
      .replace(/^[，,。；;：:\s]+/, "")
      .trim();
  }

  function factDisplayLabel(value) {
    var text = cleanFactLabel(value);
    if (codePointLength(text) <= 8) return text;

    var pieces;
    if (typeof Intl !== "undefined" && typeof Intl.Segmenter === "function") {
      var segmenter = new Intl.Segmenter("zh-CN", { granularity: "word" });
      pieces = Array.from(segmenter.segment(text), function(item) {
        return { text: item.segment, word: Boolean(item.isWordLike) };
      });
    } else {
      pieces = Array.from(text, function(character) {
        return {
          text: character,
          word: !/[，,。；;：:！？!?、（）()\s]/.test(character),
        };
      });
    }

    var label = "";
    var length = 0;
    for (var index = 0; index < pieces.length; index++) {
      var piece = pieces[index];
      if (!piece.word) {
        if (length >= 6) break;
        continue;
      }
      if (length >= 6 && /^(?:并|但|而|随后|然后|让|被)$/.test(piece.text)) {
        break;
      }
      var pieceLength = codePointLength(piece.text);
      if (length + pieceLength > 8) {
        if (length >= 6) break;
        var remaining = 8 - length;
        label += Array.from(piece.text).slice(0, remaining).join("");
        length = 8;
        break;
      }
      label += piece.text;
      length += pieceLength;
      if (length === 8) break;
    }
    var shortened = label || Array.from(text).slice(0, 8).join("");
    return shortened + "…";
  }

  function displayGraphLabel(node) {
    var rawLabel = node && (node.label || node.canonical_value) || "Node";
    return node && node.type === "fact" ? factDisplayLabel(rawLabel) : rawLabel;
  }

  function nodeTypography(scale) {
    var viewportScale = Number(scale);
    if (!isFinite(viewportScale) || viewportScale <= 0) viewportScale = 1;
    var fontScale = 1 + (viewportScale - 1) * CFG.NODE_FONT_ZOOM_WEIGHT;
    return {
      labelSize: clamp(
        CFG.NODE_FONT_SIZE * fontScale,
        CFG.NODE_FONT_SIZE_MIN,
        CFG.NODE_FONT_SIZE_MAX
      ),
      metaSize: clamp(
        CFG.NODE_META_SIZE * fontScale,
        CFG.NODE_META_SIZE_MIN,
        CFG.NODE_META_SIZE_MAX
      ),
    };
  }

  function nodeVisualRadius(node, isCenter) {
    var degree = clamp(Number(node && node.degree || 0), 0, 36);
    var weight = clamp(Number(node && node.weight || 0), 0, 20);
    var memoryCount = clamp(Number(node && node.memory_count || 0), 0, 15);
    var radius = CFG.NODE_RADIUS_BASE +
      Math.sqrt(degree) * CFG.NODE_DEGREE_GAIN +
      Math.sqrt(weight) * 0.28 +
      Math.sqrt(memoryCount) * 0.22;
if (degree <= 1) radius *= CFG.NODE_LEAF_SCALE;
    if (node && node.type === "person") radius *= CFG.PERSON_NODE_SCALE;
    if (isCenter) {
      radius = Math.min(CFG.CENTER_MAX_RADIUS, radius * CFG.CENTER_SCALE);
    }
    return clamp(
      radius,
      CFG.NODE_RADIUS_MIN,
      isCenter ? CFG.CENTER_MAX_RADIUS : CFG.NODE_RADIUS_MAX
    );
  }

  function labelCollisionMetrics(value) {
    var width = 0;
    Array.from(String(value || "")).forEach(function(character) {
      if (/[\u3400-\u9fff\uff01-\uff60]/.test(character)) width += 8.2;
      else if (/\s/.test(character)) width += 3.2;
      else width += 5.4;
    });
    return {
      width: clamp(width, 0, 88),
      height: 12,
    };
  }

  global.GraphShared = {
    CFG: CFG,
    TYPE_COLORS: TYPE_COLORS,
    isDark: isDark,
    clamp: clamp,
    lerp: lerp,
    performanceTier: performanceTier,
    themeColor: themeColor,
    hexToRgba: hexToRgba,
    getPos: getPos,
    easeInOutCubic: easeInOutCubic,
    pointToSegmentDistance: pointToSegmentDistance,
    factDisplayLabel: factDisplayLabel,
    displayGraphLabel: displayGraphLabel,
    nodeTypography: nodeTypography,
    nodeVisualRadius: nodeVisualRadius,
    labelCollisionMetrics: labelCollisionMetrics,
  };
})(typeof self !== "undefined" ? self : window);
