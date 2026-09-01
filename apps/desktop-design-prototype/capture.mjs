import { chromium } from "file:///C:/Users/aizen/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const outputDirectory = path.resolve("../../outputs/desktop-design-prototype");
await mkdir(outputDirectory, { recursive: true });

const edgePath = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const browser = await chromium.launch({ headless: true, executablePath: edgePath });
const cases = [
  { name: "conversation-light", width: 1440, height: 900, query: "space=conversation&theme=light&state=daily" },
  { name: "conversation-dark", width: 1440, height: 900, query: "space=conversation&theme=dark&state=daily" },
  { name: "developer-runtime", width: 1440, height: 900, query: "space=developer&theme=dark&state=daily" },
  { name: "conversation-compact", width: 420, height: 560, query: "space=conversation&theme=light&state=daily" },
  { name: "controls-compact", width: 420, height: 560, query: "space=controls&theme=light&state=daily" },
  { name: "empty-conversation", width: 1440, height: 900, query: "space=conversation&theme=light&state=empty" },
  { name: "no-provider", width: 1440, height: 900, query: "space=conversation&theme=light&state=no-provider" },
  { name: "offline", width: 1440, height: 900, query: "space=conversation&theme=dark&state=offline" },
  { name: "recovering", width: 1440, height: 900, query: "space=conversation&theme=light&state=recovering" },
  { name: "first-run", width: 1440, height: 900, query: "space=conversation&theme=light&state=first-run" },
  { name: "safe-mode", width: 1440, height: 900, query: "space=conversation&theme=dark&state=safe-mode" },
  { name: "history", width: 1440, height: 900, query: "space=history&theme=light&state=daily" },
  { name: "memory-privacy", width: 1440, height: 900, query: "space=memory&theme=light&state=daily" },
  { name: "conversation-controls", width: 1440, height: 900, query: "space=controls&theme=light&state=daily" },
  { name: "model-settings", width: 1440, height: 900, query: "space=settings&theme=light&state=daily" },
  { name: "developer-console", width: 1440, height: 900, query: "space=developer&theme=dark&state=daily", clickSelector: ".developer-nav > button:nth-of-type(8)" },
  { name: "version-conflict", width: 1440, height: 900, query: "space=conversation&theme=light&state=version-conflict" },
  { name: "update-ready", width: 1440, height: 900, query: "space=conversation&theme=dark&state=update-ready" },
];

const reports = [];
for (const testCase of cases) {
  const page = await browser.newPage({ viewport: { width: testCase.width, height: testCase.height }, deviceScaleFactor: 1 });
  const consoleErrors = [];
  page.on("console", (message) => { if (message.type() === "error") consoleErrors.push(message.text()); });
  page.on("pageerror", (error) => consoleErrors.push(error.message));
  await page.goto(`http://127.0.0.1:1421/?${testCase.query}`, { waitUntil: "networkidle" });
  if (testCase.clickSelector) await page.locator(testCase.clickSelector).click();
  await page.screenshot({ path: path.join(outputDirectory, `${testCase.name}.png`), fullPage: true });
  const diagnostics = await page.evaluate(() => {
    const root = document.querySelector("#root");
    const images = [...document.images].map((image) => ({ src: image.getAttribute("src"), loaded: image.complete && image.naturalWidth > 0 }));
    const overflowing = [...document.querySelectorAll("body *")]
      .filter((node) => {
        const element = node;
        const style = getComputedStyle(element);
        return element.scrollWidth > element.clientWidth + 2 && style.overflowX === "visible";
      })
      .slice(0, 12)
      .map((node) => ({ tag: node.tagName, className: node.className, clientWidth: node.clientWidth, scrollWidth: node.scrollWidth }));
    return {
      rootTextLength: root?.textContent?.trim().length ?? 0,
      documentWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
      images,
      overflowing,
    };
  });
  reports.push({ name: testCase.name, viewport: `${testCase.width}x${testCase.height}`, consoleErrors, ...diagnostics });
  await page.close();
}

const smokePage = await browser.newPage({ viewport: { width: 1280, height: 800 } });
const smoke = {};
await smokePage.goto("http://127.0.0.1:1421/?space=conversation&theme=light&state=daily", { waitUntil: "networkidle" });
smoke.controlsRemovedFromRail = await smokePage.locator('.rail button[aria-label="对话控制"]').count() === 0;
smoke.developerHiddenByDefault = await smokePage.locator(".rail-button--developer").count() === 0;
if (!(await smokePage.locator(".context-drawer").isVisible())) {
  await smokePage.locator('.conversation-topbar__actions button[aria-label="查看这次聊天"]').click();
}
const userContextText = await smokePage.locator(".context-drawer").innerText();
smoke.userContextHasNoInternalPolicy = !/回应义务|展开许可|追问许可|follow_up_permission|WorldOps|内部状态/.test(userContextText);
await smokePage.locator(".composer textarea").fill("prototype interaction test");
await smokePage.locator(".composer__action").click();
await smokePage.waitForTimeout(1500);
smoke.conversationSend = await smokePage.locator("article.message").count() >= 6;
await smokePage.goto("http://127.0.0.1:1421/?space=settings&theme=light&state=daily", { waitUntil: "networkidle" });
await smokePage.locator(".add-row").click();
smoke.providerDialog = await smokePage.locator(".modal--provider").isVisible();
await smokePage.locator(".modal--provider .icon-button").click();
await smokePage.getByRole("button", { name: "高级" }).click();
smoke.developerCanBeEnabled = await smokePage.getByRole("switch", { name: "调试模式" }).isVisible();
await smokePage.goto("http://127.0.0.1:1421/?space=developer&theme=dark&state=daily", { waitUntil: "networkidle" });
await smokePage.locator(".developer-nav > button:nth-of-type(8)").click();
await smokePage.locator(".run-command").click();
await smokePage.waitForTimeout(850);
smoke.controlledConsole = await smokePage.locator(".console-output article").count() >= 2;
await smokePage.goto("http://127.0.0.1:1421/?space=conversation&theme=light&state=first-run", { waitUntil: "networkidle" });
await smokePage.locator(".onboarding-actions .button:last-child").click();
smoke.firstRunProgression = await smokePage.locator(".detected-provider").isVisible();
reports.push({ name: "interaction-smoke", ...smoke });
await smokePage.close();

await browser.close();
await writeFile(path.join(outputDirectory, "verification-report.json"), JSON.stringify(reports, null, 2), "utf8");
console.log(JSON.stringify(reports, null, 2));
