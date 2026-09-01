import { mkdir, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const appDirectory = path.resolve(scriptDirectory, "..");
const repositoryRoot = path.resolve(appDirectory, "..", "..");
const distDirectory = path.join(appDirectory, "dist");
const outputDirectory = path.join(repositoryRoot, "outputs", "desktop-design-prototype");
const outputPath = path.join(outputDirectory, "Project-Sora-Desktop-Prototype.html");

const htmlPath = path.join(distDirectory, "index.html");
const imagePath = path.join(appDirectory, "public", "sora-reference.png");

let html = await readFile(htmlPath, "utf8");
const image = await readFile(imagePath);
const imageDataUrl = `data:image/png;base64,${image.toString("base64")}`;

function resolveBuiltAsset(assetUrl) {
  const clean = assetUrl.split(/[?#]/, 1)[0].replace(/^\.?\//, "");
  const resolved = path.resolve(distDirectory, clean);
  const relative = path.relative(distDirectory, resolved);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`Refusing asset outside dist: ${assetUrl}`);
  }
  return resolved;
}

function inlineStaticReferences(source) {
  return source
    .replaceAll("/sora-reference.png", imageDataUrl)
    .replaceAll("./sora-reference.png", imageDataUrl)
    .replace(/^\/\/# sourceMappingURL=.*$/gm, "")
    .replace(/^\/\*# sourceMappingURL=.*\*\/$/gm, "");
}

function escapeInlineEndTag(source, tagName) {
  const pattern = new RegExp(`</${tagName}`, "gi");
  const escaped = source.replace(
    pattern,
    () => `<${String.fromCharCode(92)}/${tagName}`,
  );
  if (pattern.test(escaped)) {
    throw new Error(`Unable to escape inline ${tagName} end tag.`);
  }
  return escaped;
}

const stylesheetPattern = /<link\b[^>]*\bhref=["']([^"']+\.css(?:[?#][^"']*)?)["'][^>]*>/gi;
for (const match of [...html.matchAll(stylesheetPattern)]) {
  const css = inlineStaticReferences(await readFile(resolveBuiltAsset(match[1]), "utf8"));
  const safeCss = escapeInlineEndTag(css, "style");
  html = html.replace(match[0], () => `<style data-sora-offline="styles">${safeCss}</style>`);
}

const scriptPattern = /<script\b[^>]*\bsrc=["']([^"']+\.js(?:[?#][^"']*)?)["'][^>]*><\/script>/gi;
for (const match of [...html.matchAll(scriptPattern)]) {
  const javascript = inlineStaticReferences(await readFile(resolveBuiltAsset(match[1]), "utf8"));
  const safeJavascript = escapeInlineEndTag(javascript, "script");
  html = html.replace(
    match[0],
    () => `<script type="module" data-sora-offline="application">${safeJavascript}</script>`,
  );
}

html = inlineStaticReferences(html)
  .replace(
    "<head>",
    `<head>\n    <!-- Self-contained Project Sora prototype. No local server or network assets required. -->`,
  )
  .replace(
    "</body>",
    `  <noscript>此原型需要启用 JavaScript。</noscript>\n  </body>`,
  );

const documentShell = html
  .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "")
  .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, "");
if (/\b(?:src|href)=["']\/(?!\/)/i.test(documentShell)) {
  throw new Error("Offline export still contains a root-relative asset URL.");
}
if (!html.includes('data-sora-offline="application"') || !html.includes('data-sora-offline="styles"')) {
  throw new Error("Offline export is missing inlined application or styles.");
}

await mkdir(outputDirectory, { recursive: true });
await writeFile(outputPath, html, "utf8");

console.log(outputPath);
