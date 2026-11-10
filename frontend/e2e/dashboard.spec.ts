import { expect, test, type Page } from "@playwright/test";
import { stubApi } from "./helpers";

const indicators = (page: Page) =>
  page.getByTestId("connection-indicators").first();

test.describe("with a healthy (stubbed) backend", () => {
  test.beforeEach(async ({ page }) => stubApi(page, "ok"));

  test("dashboard loads without invented data", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1, name: "Welcome to Katomaran AI" })).toBeVisible();
    for (const v of await page.getByTestId("metric-value").all()) await expect(v).toHaveText("—");
    await expect(page.getByText("No video source connected.").first()).toBeVisible();
    await expect(page.getByText("Visitor analytics will appear after processing begins.").first()).toBeVisible();
  });

  test("sidebar navigation reaches every page", async ({ page }) => {
    await page.goto("/");
    const nav = page.getByRole("navigation", { name: "Main navigation" });
    for (const [name, path] of [
      ["Live Monitoring", "/live"],
      ["Visitors", "/visitors"],
      ["Event Logs", "/events"],
      ["Analytics", "/analytics"],
      ["Settings", "/settings"],
      ["Overview", "/"],
    ] as const) {
      await nav.getByRole("link", { name }).click();
      await expect(page).toHaveURL(path === "/" ? /\/$/ : new RegExp(`${path}$`));
      await expect(nav.getByRole("link", { name })).toHaveAttribute("aria-current", "page");
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    }
  });

  test("system status reflects backend values", async ({ page }) => {
    await page.goto("/");
    await expect(indicators(page).getByRole("status", { name: "API: Connected" })).toBeVisible();
    await expect(indicators(page).getByRole("status", { name: "Database: Connected" })).toBeVisible();
    const panel = page.getByTestId("system-status");
    await expect(panel).toContainText("katomaran-api v0.1.0 (test)");
    await expect(panel).toContainText("Applied (revision 0001)");
    await expect(panel.getByText("Not running")).toHaveCount(2);
    await expect(panel.getByText("Not implemented yet")).toHaveCount(2);
  });

  test("sidebar collapses and state survives reload", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Collapse sidebar" }).click();
    await expect(page.getByRole("button", { name: "Expand sidebar" })).toBeVisible();
    await page.reload();
    await expect(page.getByRole("button", { name: "Expand sidebar" })).toBeVisible();
  });
});

test("reports an unavailable database separately from the API", async ({ page }) => {
  await stubApi(page, "db-down");
  await page.goto("/");
  await expect(indicators(page).getByRole("status", { name: "API: Connected" })).toBeVisible();
  await expect(indicators(page).getByRole("status", { name: "Database: Unavailable" })).toBeVisible();
});

test("shows a disconnected state and stays usable when the backend is offline", async ({ page }) => {
  await stubApi(page, "offline");
  await page.goto("/");
  await expect(indicators(page).getByRole("status", { name: "API: Disconnected" })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("Backend not reachable")).toBeVisible();
  await page.getByRole("navigation", { name: "Main navigation" }).getByRole("link", { name: "Visitors" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Visitors" })).toBeVisible();
});

test("mobile viewport uses the navigation drawer", async ({ page }) => {
  await stubApi(page, "ok");
  await page.setViewportSize({ width: 390, height: 800 });
  await page.goto("/");
  await expect(page.getByRole("navigation", { name: "Main navigation" })).toBeHidden();
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page.getByRole("navigation", { name: "Mobile navigation" }).getByRole("link", { name: "Analytics" }).click();
  await expect(page).toHaveURL(/\/analytics$/);
});

test("@live UI matches the real backend through the dev proxy", async ({ page, request }) => {
  const ready = await request.get("http://127.0.0.1:8000/api/v1/ready");
  const expectedDb = ready.status() === 200 ? "Connected" : "Unavailable";
  await page.goto("/");
  await expect(indicators(page).getByRole("status", { name: "API: Connected" })).toBeVisible();
  await expect(indicators(page).getByRole("status", { name: `Database: ${expectedDb}` })).toBeVisible({
    timeout: 30_000,
  });
});
