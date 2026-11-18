// Runs the page's script against a saved /api/status with a minimal stand-in for the DOM: render(),
// then tomorrow's chart. Prints "render OK, tomorrow OK" or the error. Used by test_page_render.py.
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const status = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
function __mk(tag) {
  const e = { tagName: tag, children: [], style: {}, dataset: {}, attrs: {}, classList: { add(){}, remove(){}, toggle(){} },
    textContent: "", innerHTML: "", hidden: false, className: "", value: "",
    append(...c) { this.children.push(...c); }, replaceChildren(...c) { this.children = c; },
    setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; },
    addEventListener() {}, showModal() {}, close() {}, closest() { return __mk("dialog"); },
    querySelectorAll() { return []; }, querySelector() { return null; }, getBBox() { return {width: 10, height: 10}; } };
  return e;
}
const byId = {};
global.document = { getElementById: (id) => (byId[id] ||= __mk("div")), createElement: __mk, createElementNS: (ns, t) => __mk(t),
  querySelectorAll: () => [], body: __mk("body"), documentElement: __mk("html") };
global.window = global; global.getComputedStyle = () => ({ getPropertyValue: () => "#fff" });
global.fetch = async () => ({ ok: true, json: async () => status });
global.setInterval = () => 0; global.setTimeout = () => 0; global.clearTimeout = () => {};
global.matchMedia = () => ({ matches: false, addEventListener() {} });
try {
  eval(script + "\n;globalThis.__render = render; globalThis.__tomorrow = () => { day = \"tomorrow\"; renderChart(); };");
  __render(status);
  __tomorrow(); console.log("render OK, tomorrow OK");
} catch (e) { console.log("RENDER ERROR:", e && e.stack ? e.stack.split("\n").slice(0, 4).join("\n") : e); }
