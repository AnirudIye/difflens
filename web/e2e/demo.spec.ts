import { expect, test } from "@playwright/test";

// What this test is for: the demo is the one page a stranger sees, and it is
// the only page that can break without anyone signing in to notice. It runs
// the real queue, the real worker, and the real analyzers, so a green run
// here means an anonymous visitor really can get findings on screen.

test("an anonymous visitor reads findings and runs the review again", async ({
  page,
}) => {
  await page.goto("/demo");

  // No sign in happened, and none is offered as a precondition
  await expect(
    page.getByRole("heading", { name: "A real review, no account needed" }),
  ).toBeVisible();

  // The seeded review finishes on its own; the worker may still be on it
  await expect(page.getByText("Done", { exact: true })).toBeVisible({
    timeout: 90_000,
  });

  // Findings from both halves of the pipeline. The severity chips come from
  // severity_counts on the review, so this also proves the row was written.
  await expect(page.locator(".finding").first()).toBeVisible();
  const findingCount = await page.locator(".finding").count();
  expect(findingCount).toBeGreaterThanOrEqual(8);

  // Both files in the sample are represented
  await expect(page.getByText("checkout/payments.py").first()).toBeVisible();
  await expect(page.getByText("src/checkout.ts").first()).toBeVisible();

  // At least one hybrid finding, which is the whole claim of the product:
  // a linter finding and a model explanation merged onto one line
  await expect(page.getByText("analyzer + ai").first()).toBeVisible();

  // The demo says out loud that its AI half is recorded rather than live
  await expect(
    page.getByText(/replays a recorded review/i).first(),
  ).toBeVisible();

  // Feedback is per account, so an anonymous visitor is offered none
  await expect(page.getByRole("button", { name: "Useful" })).toHaveCount(0);

  // The showpiece: a real job through the real queue
  const rerun = page.getByRole("button", { name: "Run this review again" });
  await expect(rerun).toBeEnabled();
  await rerun.click();

  // Queued or Analyzing, then Done again. Checking for the live state first
  // is what proves the button enqueued work rather than re-rendering.
  await expect(page.getByText(/Queued|Analyzing/)).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByText("Done", { exact: true })).toBeVisible({
    timeout: 90_000,
  });
  await expect(page.locator(".finding").first()).toBeVisible();
});
