#!/usr/bin/env node
/**
 * diagram_export.mjs
 * 把 archify 交付的 HTML 中的内联 <svg> 抽出来，用 Chrome 无头渲染成纯 PNG。
 * 用法: node diagram_export.mjs
 * 依赖: 本机 Chrome (chrome.exe) 已安装。
 * 产出: 每张图一个 <名称>.png (白色/透明背景，内容-only，不带 viewer chrome)。
 */
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { dirname, join, basename } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

const CHROME =
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

// 顺序: [源 html, 输出 png 名, 期望宽高比]
const jobs = [
  ["架构图.html", "架构图.png", 1200 / 520],
  ["误报判定流程.html", "误报判定流程.png", null],
  ["clangd引用查询.html", "clangd引用查询.png", 940 / 780],
  ["判定生命周期.html", "判定生命周期.png", 980 / 660],
  ["误报识别管道.html", "误报识别管道.png", 1000 / 560],
  // 7 条规则判定流程图
  ["误报判定_69D_变量未赋值就使用.html", "误报判定_69D.png", null],
  ["误报判定_57S_无作用的语句.html", "误报判定_57S.png", null],
  ["误报判定_1X_声明类型不一致.html", "误报判定_1X.png", null],
  ["误报判定_47S_数组越界.html", "误报判定_47S.png", null],
  ["误报判定_36S_函数没有返回语句.html", "误报判定_36S.png", null],
  ["误报判定_132S_逻辑表达式使用赋值.html", "误报判定_132S.png", null],
  ["误报判定_404S_字符串数组越界初始化.html", "误报判定_404S.png", null],
];

function extractSvg(html) {
  const m = html.match(/<svg[\s\S]*?<\/svg>/i);
  if (!m) throw new Error("未找到 <svg> 块");
  return m[0];
}

function extractStyles(html) {
  // 取全部 <style>...</style>，去重合并成一段（archify 的 class 样式都在这里）
  const re = /<style[^>]*>([\s\S]*?)<\/style>/gi;
  const seen = new Set();
  const out = [];
  let mm;
  while ((mm = re.exec(html)) !== null) {
    if (!seen.has(mm[1])) { seen.add(mm[1]); out.push(mm[1]); }
  }
  return out.join("\n");
}

function lightOverride(styles) {
  // 抽出所有 [data-theme="light"]{...} 的主题变量，合并成 :root 覆盖，压过暗色默认
  const re = /\[\s*data-theme\s*=\s*["']light["']\s*\]\s*\{([^}]*)\}/g;
  const bodies = [];
  let mm;
  while ((mm = re.exec(styles)) !== null) bodies.push(mm[1]);
  return bodies.join("\n");
}

function svgViewBox(svg) {
  const m = svg.match(/viewBox\s*=\s*"([\d.\s]+)"/);
  if (!m) return null;
  const p = m[1].trim().split(/\s+/).map(Number);
  return p;
}

for (const [htmlName, pngName, explicitRatio] of jobs) {
  const htmlPath = join(__dirname, htmlName);
  if (!existsSync(htmlPath)) {
    console.error("缺少源:", htmlPath);
    continue;
  }
  let svg = extractSvg(readFileSync(htmlPath, "utf8"));
  const styles = extractStyles(readFileSync(htmlPath, "utf8"));
  // viewBox 决定渲染尺寸
  const vb = svgViewBox(svg);
  let w = 1400, h = 1400;
  if (vb) {
    w = Math.round((vb[2] - vb[0]) || 1400);
    h = Math.round((vb[3] - vb[1]) || 1400);
  } else if (explicitRatio) {
    w = 1400; h = Math.round(1400 / explicitRatio);
  }
  // 关键：archify 样式走 CSS 变量，变量定义在 [data-theme="light"] 作用域下。
  // 包一层带 data-theme=light 的 div 并注入完整样式，孤立 SVG 才能正确着色。
  const lo = lightOverride(styles);
  const page = `<!doctype html><html data-theme="light"><head><meta charset="utf-8">
<style>body{margin:0;}${styles}
:root{${lo}}</style></head>
<body data-theme="light"><div data-theme="light">${svg}</div></body></html>`;
  const pagePath = join(__dirname, pngName.replace(/\.png$/, ".render.html"));
  writeFileSync(pagePath, page, "utf8");

  const svgPath = join(__dirname, pngName.replace(/\.png$/, ".svg"));
  writeFileSync(svgPath, svg, "utf8");

  const pngPath = join(__dirname, pngName);
  const cmd = [
    "--headless=new",
    "--disable-gpu",
    "--hide-scrollbars",
    `--screenshot=${pngPath}`,
    `--window-size=${w},${h}`,
    `--default-background-color=FFFFFFFF`,
    `file:///${pagePath.replace(/\\/g, "/")}`,
  ];
  try {
    execFileSync(CHROME, cmd, { stdio: "ignore", timeout: 60000 });
    const ok = existsSync(pngPath);
    console.log(ok ? "OK  " + pngName : "FAIL(no file) " + pngName);
  } catch (e) {
    console.error("ERROR " + pngName, e.message.slice(0, 200));
  }
}