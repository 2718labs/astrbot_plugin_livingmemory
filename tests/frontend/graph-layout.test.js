import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "../../pages/dashboard/graph-2d.js"), "utf-8");
const coreSource = readFileSync(join(here, "../../pages/dashboard/graph-layout-core.js"), "utf-8");
const sharedSource = readFileSync(join(here, "../../pages/dashboard/graph-shared.js"), "utf-8");
const rendererSource = readFileSync(join(here, "../../pages/dashboard/graph-renderer.js"), "utf-8");
const interactionSource = readFileSync(join(here, "../../pages/dashboard/graph-interaction.js"), "utf-8");

function makeCtx() {
  const gradient = { addColorStop() {} };
  const ctx = {
    measureText: () => ({ width: 10 }),
    createLinearGradient: () => gradient,
    createRadialGradient: () => gradient,
  };
  return new Proxy(ctx, {
    get(target, prop) {
      if (prop in target) return target[prop];
      return () => {};
    },
    set(target, prop, value) {
      target[prop] = value;
      return true;
    },
  });
}

function makeCanvas() {
  const parent = {
    getBoundingClientRect: () => ({ width: 800, height: 600 }),
    clientWidth: 800,
    clientHeight: 600,
  };
  return {
    getContext: () => makeCtx(),
    parentElement: parent,
    addEventListener() {},
    style: {},
    width: 800,
    height: 600,
  };
}

function makeContainer() {
  return { innerHTML: "", appendChild() {} };
}

function loadGraph() {
  const rafQueue = [];
  global.window = {
    devicePixelRatio: 1,
    matchMedia: () => ({ matches: true }),
    ResizeObserver: class {
      observe() {}
      disconnect() {}
    },
    MutationObserver: class {
      observe() {}
      disconnect() {}
    },
    addEventListener() {},
  };
  global.document = {
    documentElement: { getAttribute: () => "light" },
    createElement: () => makeCanvas(),
    addEventListener() {},
  };
  global.getComputedStyle = () => ({ getPropertyValue: () => "" });
  global.requestAnimationFrame = (cb) => {
    rafQueue.push(cb);
    return rafQueue.length;
  };
  global.cancelAnimationFrame = () => {};
  class Observer {
    observe() {}
    disconnect() {}
  }
  global.ResizeObserver = Observer;
  global.MutationObserver = Observer;
  (0, eval)(sharedSource);
  global.GraphShared = global.window.GraphShared;
  (0, eval)(coreSource);
  (0, eval)(rendererSource);
  (0, eval)(interactionSource);
  global.GraphRenderer = global.window.GraphRenderer;
  global.GraphInteraction = global.window.GraphInteraction;
  (0, eval)(source);
  return rafQueue;
}

function flushRaf(rafQueue) {
  let guard = 0;
  while (rafQueue.length && guard < 200000) {
    const cb = rafQueue.shift();
    cb(0);
    guard++;
  }
}

/* 渐进式布局现在是 async：需要交替排空微任务与 rAF 队列。 */
async function settle(rafQueue) {
  let guard = 0;
  while (guard < 200000) {
    guard++;
    await Promise.resolve();
    if (!rafQueue.length) break;
    flushRaf(rafQueue);
  }
}

function makePayload(nodeCount, edgeCount) {
  const nodes = [];
  for (let i = 1; i <= nodeCount; i++) {
    nodes.push({ id: i, type: "topic", label: "N" + i, weight: 1 });
  }
  const edges = [];
  for (let e = 0; e < edgeCount && e < nodeCount - 1; e++) {
    edges.push({
      id: e + 1,
      source: e + 1,
      target: e + 2,
      relation_type: "relates",
      memory_id: 1,
      weight: 1,
      confidence: 0.8,
    });
  }
  return { enabled: true, mode: "query", snapshot: { nodes, edges } };
}

test("small graph layout completes synchronously with positions", async () => {
  const rafQueue = loadGraph();
  const g = global.window.Graph2D;
  g.init(makeContainer(), {});

  const payload = makePayload(50, 49);
  g.loadData(payload);

  assert.equal(g._nodes.length, 50);
  assert.equal(Object.keys(g.animator._layout.positions).length, 50);
  assert.equal(g.animator._layout._done, true);
  await settle(rafQueue);
  /* 空闲渲染后社区椭圆缓存应已填充。 */
  assert.ok(g.renderer._communityCacheKey != null);
  assert.ok(Array.isArray(g.renderer._communityCache));
});

test("small overview keeps each connected component in one force system", async () => {
  const rafQueue = loadGraph();
  const g = global.window.Graph2D;
  g.init(makeContainer(), {});

  const connected = makePayload(20, 19);
  connected.mode = "overview";
  g.loadData(connected);
  await settle(rafQueue);

  assert.equal(g.animator._layout._topology.strategy, "components");
  assert.equal(new Set(g._nodes.map((node) => node.community)).size, 1);
  assert.ok(g.animator._layout._simEdges.every((edge) => edge.sameCommunity));
  assert.equal(g.renderer._communityCache.length, 1, "单个真实连通分量仍保留社区轮廓");

  const disconnected = makePayload(10, 0);
  disconnected.mode = "overview";
  disconnected.snapshot.edges = [
    { id: 1, source: 1, target: 2, relation_type: "relates", memory_id: 1 },
    { id: 2, source: 2, target: 3, relation_type: "relates", memory_id: 1 },
    { id: 3, source: 3, target: 4, relation_type: "relates", memory_id: 1 },
    { id: 4, source: 4, target: 5, relation_type: "relates", memory_id: 1 },
    { id: 5, source: 6, target: 7, relation_type: "relates", memory_id: 2 },
    { id: 6, source: 7, target: 8, relation_type: "relates", memory_id: 2 },
    { id: 7, source: 8, target: 9, relation_type: "relates", memory_id: 2 },
    { id: 8, source: 9, target: 10, relation_type: "relates", memory_id: 2 },
  ];
  g.loadData(disconnected);

  assert.equal(new Set(g._nodes.map((node) => node.community)).size, 2);
});

test("query mode keeps the existing hub topology for the same small graph", () => {
  loadGraph();
  const g = global.window.Graph2D;
  g.init(makeContainer(), {});

  const payload = makePayload(20, 19);
  payload.mode = "overview";
  g.loadData(payload);
  assert.equal(g.animator._layout._topology.strategy, "components");

  payload.mode = "query";
  g.loadData(payload);
  assert.equal(g.animator._layout._topology.strategy, "hubs");
  assert.ok(new Set(g._nodes.map((node) => node.community)).size > 1);
});

test("visible labels reserve one-sided rectangles in the force layout", () => {
  loadGraph();
  const g = global.window.Graph2D;
  g.init(makeContainer(), {});

  const nodes = [];
  for (let id = 1; id <= 8; id++) {
    nodes.push({ id, type: "topic", label: id <= 2 ? "长期计划调整" : "外围节点" });
  }
  const pairs = [[1, 2], [1, 3], [1, 4], [1, 5], [2, 6], [2, 7], [2, 8]];
  const edges = pairs.map(([source, target], index) => ({
    id: index + 1,
    source,
    target,
    relation_type: "relates",
    memory_id: 1,
    weight: 1,
    confidence: 0.8,
  }));
  g.loadData({ enabled: true, mode: "overview", snapshot: { nodes, edges } });

  const hubs = g.animator._layout._sim.filter((node) => node.id === 1 || node.id === 2);
  const leaf = g.animator._layout._sim.find((node) => node.id === 3);
  assert.ok(hubs.every((node) => node.labelWidth > 0));
  assert.equal(leaf.labelWidth, 0);

  function occupiedBox(node) {
    const padding = 3;
    return {
      x1: node.x - node.radius - padding,
      x2: node.x + node.radius + padding +
        (node.labelWidth > 0 ? 7 + node.labelWidth : 0),
      y1: node.y - Math.max(node.radius, node.labelHeight / 2) - padding,
      y2: node.y + Math.max(node.radius, node.labelHeight / 2) + padding,
    };
  }
  const boxes = g.animator._layout._sim.map(occupiedBox);
  for (let i = 0; i < boxes.length; i++) {
    for (let j = i + 1; j < boxes.length; j++) {
      const overlapX = Math.min(boxes[i].x2, boxes[j].x2) -
        Math.max(boxes[i].x1, boxes[j].x1);
      const overlapY = Math.min(boxes[i].y2, boxes[j].y2) -
        Math.max(boxes[i].y1, boxes[j].y1);
      assert.ok(overlapX <= 0 || overlapY <= 0, "常驻标签矩形不应覆盖其他节点或标签");
    }
  }
});

test("degree radius and leaf treatment create a restrained visual hierarchy", () => {
  loadGraph();
  const radius = global.window.GraphShared.nodeVisualRadius;
  const leaf = radius({ degree: 1, weight: 1, memory_count: 1 }, false);
  const branch = radius({ degree: 4, weight: 1, memory_count: 1 }, false);
  const hub = radius({ degree: 16, weight: 1, memory_count: 1 }, false);

assert.ok(leaf < branch);
  assert.ok(branch < hub);
  assert.ok(hub <= global.window.GraphShared.CFG.NODE_RADIUS_MAX);
  assert.equal(global.window.GraphShared.CFG.NODE_LEAF_OPACITY, 0.68);
  assert.match(rendererSource, /subduedLeaf \? CFG\.NODE_LEAF_OPACITY : 1/);
});

test("person nodes stay visually smaller than same-degree regular nodes", () => {
  loadGraph();
  const radius = global.window.GraphShared.nodeVisualRadius;
  const person = radius({ degree: 6, weight: 3, memory_count: 5, type: "person" }, false);
  const regular = radius({ degree: 6, weight: 3, memory_count: 5 }, false);
  assert.ok(person < regular);
  assert.ok(Math.abs(person - regular * 0.92) < 1e-9);
  assert.ok(person <= global.window.GraphShared.CFG.NODE_RADIUS_MAX);
});

test("person nodes still dominate low-degree regular nodes", () => {
  loadGraph();
  const radius = global.window.GraphShared.nodeVisualRadius;
  const person = radius({ degree: 6, weight: 3, memory_count: 5, type: "person" }, false);
  const regular = radius({ degree: 4, weight: 3, memory_count: 1 }, false);
  assert.ok(person > regular, "person must stay the visual anchor of the graph");
});

test("label width cache populates when labels render", async () => {
  const rafQueue = loadGraph();
  const g = global.window.Graph2D;
  g.init(makeContainer(), {});

  const nodes = [];
  for (let i = 1; i <= 30; i++) {
    nodes.push({ id: i, type: "topic", label: "Prominent-Node-" + i, weight: 3, degree: 6 });
  }
  const edges = [];
  for (let e = 0; e < 29; e++) {
    edges.push({ id: e + 1, source: e + 1, target: e + 2, relation_type: "relates", memory_id: 1, weight: 1, confidence: 0.8 });
  }
  g.loadData({ enabled: true, mode: "query", snapshot: { nodes, edges } });
  await settle(rafQueue);

  assert.ok(Object.keys(g.renderer._labelWidthCache).length > 0, "标签宽度缓存应有条目");
});

test("fact labels remove narration prefixes and keep 6-8 whole characters before an ellipsis", () => {
  loadGraph();
  const label = global.window.GraphShared.factDisplayLabel;
  const cases = [
    ["2026-08-24深夜，Alice告诉爱丽丝，周末需要重新整理书架", "告诉爱丽丝周末…"],
    ["2026-08-24深夜，Alice询问可否把会议改到周五上午", "询问可否把会议…"],
    ["2026-08-24深夜至25日凌晨，我提到Alice已经寄出蓝色文件夹", "提到已经寄出蓝色…"],
    ["2026-08-24晚，Alice讨论知识图谱字体优化", "讨论知识图谱字体…"],
  ];

  for (const [raw, expected] of cases) {
    const actual = label(raw);
    assert.equal(actual, expected);
    assert.ok(actual.endsWith("…"));
    const summary = Array.from(actual.slice(0, -1));
    assert.ok(summary.length >= 6 && summary.length <= 8);
  }
  assert.equal(global.window.GraphShared.CFG.NODE_FONT_SIZE, 11);
  assert.equal(global.window.GraphShared.CFG.NODE_META_SIZE, 9);
  assert.match(rendererSource, /var typography = nodeTypography\(scale\);/);
  assert.match(rendererSource, /var fontSize = typography\.labelSize;/);
  assert.match(rendererSource, /var metaFs = typography\.metaSize;/);
});

test("node typography applies one-quarter zoom with readable limits", () => {
  loadGraph();
  const typography = global.window.GraphShared.nodeTypography;

  assert.deepEqual(typography(1), { labelSize: 11, metaSize: 9 });
  assert.ok(Math.abs(typography(1.65).labelSize - 12.7875) < 1e-9);
  assert.ok(Math.abs(typography(1.65).metaSize - 10.4625) < 1e-9);
  assert.deepEqual(typography(3.5), { labelSize: 15, metaSize: 12 });
  assert.deepEqual(typography(0.06), { labelSize: 10, metaSize: 8 });
});

test("graph keeps raw fact payload while using the short canvas label", () => {
  loadGraph();
  const g = global.window.Graph2D;
  g.init(makeContainer(), {});
  const raw = "2026-08-24晚，Alice讨论知识图谱字体优化";
  const payload = {
    enabled: true,
    mode: "query",
    snapshot: { nodes: [{ id: 1, type: "fact", label: raw }], edges: [] },
  };

  g.loadData(payload);

  assert.equal(g._nodes[0].label, "讨论知识图谱字体…");
  assert.equal(payload.snapshot.nodes[0].label, raw);
});

test("large graph layout completes via progressive stepping", async () => {
  const rafQueue = loadGraph();
  const g = global.window.Graph2D;
  g.init(makeContainer(), {});

  const payload = makePayload(300, 299);
  g.loadData(payload);

  /* Layout completes via progressive rAF chunks. */
  await settle(rafQueue);
  assert.equal(g._nodes.length, 300);
  assert.equal(g.animator._layout._done, true);
  assert.equal(Object.keys(g.animator._layout.positions).length, 300);
});

test("identical graph structure reuses cached layout", async () => {
  const rafQueue = loadGraph();
  const g = global.window.Graph2D;
  g.init(makeContainer(), {});

  const payload = makePayload(80, 79);
  g.loadData(payload);
  await settle(rafQueue);
  const firstPositions = Object.assign({}, g.animator._layout.positions);

  /* Load the same structure again — should skip recompute and keep positions. */
  g.loadData(payload);
  assert.equal(g.animator._layout._done, true);
  assert.deepEqual(
    Object.keys(g.animator._layout.positions).sort(),
    Object.keys(firstPositions).sort()
  );
  await settle(rafQueue);
});

test("selecting a node recenters without recomputing the layout", async () => {
  const rafQueue = loadGraph();
  const g = global.window.Graph2D;
  g.init(makeContainer(), {});

  const payload = makePayload(60, 59);
  g.loadData(payload);

  /* Record the layout progress marker; recenter must not re-run the simulation. */
  const stepAfterLoad = g.animator._layout._step;
  const doneAfterLoad = g.animator._layout._done;

  g.selectNode(30);
  assert.equal(g.animator._layout._done, doneAfterLoad);
  assert.equal(g.animator._layout._step, stepAfterLoad);
  await settle(rafQueue);
});

test("rapid double load leaves a consistent progressive layout", async () => {
  const rafQueue = loadGraph();
  const g = global.window.Graph2D;
  g.init(makeContainer(), {});

  /* 两次快速加载：旧渐进链路应被代数守卫丢弃，新链路完成布局。 */
  const first = makePayload(260, 259);
  const second = makePayload(280, 279);
  g.loadData(first);
  g.loadData(second);
  await settle(rafQueue);

  assert.equal(g._nodes.length, 280);
  assert.equal(g.animator._layout._done, true);
  assert.equal(Object.keys(g.animator._layout.positions).length, 280);
});

/* ── Web Worker 布局测试 ─────────────────────────────────────── */

const workerSource = readFileSync(
  join(here, "../../pages/dashboard/graph-layout-worker.js"), "utf-8"
);

/* 在进程内模拟 Worker：把 worker 脚本的逻辑以假 self 跑起来，消息同步往返。 */
function installFakeWorker() {
  global.Worker = class {
    constructor() {
      const worker = this;
      this.onmessage = null;
      const posts = [];
      const fakeSelf = {
        postMessage(msg) { posts.push(msg); },
        importScripts() {
          /* importScripts 把核心加载进 worker 全局作用域（即 globalThis）。 */
          const saved = global.self;
          global.self = global;
          (0, eval)(coreSource);
          global.self = saved;
        },
      };
      const saved = global.self;
      const savedImportScripts = global.importScripts;
      global.self = fakeSelf;
      global.importScripts = fakeSelf.importScripts;
      (0, eval)(workerSource);
      global.self = saved;
      global.importScripts = savedImportScripts;
      this._dispatch = (msg) => {
        /* 调用 worker 的 onmessage 时 self 应仍指向 worker 全局。 */
        const saved = global.self;
        global.self = fakeSelf;
        fakeSelf.onmessage({ data: msg });
        global.self = saved;
      };
      this._flush = () => {
        while (posts.length) {
          const msg = posts.shift();
          if (worker.onmessage) worker.onmessage({ data: msg });
        }
      };
    }
    postMessage(msg) {
      this._dispatch(msg);
      this._flush();
    }
    terminate() {}
  };
}

test("worker layout completes via fake worker round trip", async () => {
  const rafQueue = loadGraph();
  installFakeWorker();
  const g = global.window.Graph2D;
  g.init(makeContainer(), {});

  assert.equal(g.animator._layout.isWorker, true, "Worker 布局应被启用");

  const payload = makePayload(300, 299);
  g.loadData(payload);
  await settle(rafQueue);

  assert.equal(g._nodes.length, 300);
  assert.equal(g.animator._layout._done, true);
  assert.equal(Object.keys(g.animator._layout.positions).length, 300);
});

test("worker forwards overview mode to component topology", async () => {
  const rafQueue = loadGraph();
  installFakeWorker();
  const g = global.window.Graph2D;
  g.init(makeContainer(), {});

  const payload = makePayload(70, 69);
  payload.mode = "overview";
  g.loadData(payload);
  await settle(rafQueue);

  assert.equal(new Set(Object.values(g.animator._layout.communities)).size, 1);
});

test("worker layout falls back to inline when Worker unavailable", async () => {
  const rafQueue = loadGraph();
  const savedWorker = global.Worker;
  global.Worker = undefined;
  const g = global.window.Graph2D;
  g.init(makeContainer(), {});

  assert.notEqual(g.animator._layout.isWorker, true, "Worker 不可用时回退内联布局");

  const payload = makePayload(120, 119);
  g.loadData(payload);
  await settle(rafQueue);

  assert.equal(g.animator._layout._done, true);
  assert.equal(Object.keys(g.animator._layout.positions).length, 120);
  global.Worker = savedWorker;
});
