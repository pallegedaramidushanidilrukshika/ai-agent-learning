// Replays a validated step list (from browser_recording.py) in headless Chromium.
// It never runs the uploaded recording itself: only allowlisted locator,
// action and assertion names from the step list are called.
//
// Usage: node replay.mjs <steps.json> <base-url> [failure-screenshot.png] [trace.zip]
// An empty screenshot argument skips the screenshot. The trace (DOM snapshots,
// screenshots and network of every step) opens with `npx playwright show-trace`.
// Prints one JSON result object on the last stdout line.
import { readFileSync } from 'node:fs';

const ACTION_TIMEOUT_MS = 10_000;
const ASSERT_TIMEOUT_MS = 5_000;
const NAVIGATION_TIMEOUT_MS = 15_000;
const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]']);
const LOCATOR_METHODS = new Set([
  'getByRole', 'getByText', 'getByLabel', 'getByPlaceholder', 'getByTestId',
  'getByTitle', 'getByAltText', 'locator', 'filter', 'nth', 'first', 'last',
]);
const ACTIONS = new Set(['click', 'dblclick', 'fill', 'press', 'check', 'uncheck', 'selectOption', 'hover']);
const ASSERTIONS = new Set([
  'toBeVisible', 'toBeHidden', 'toBeChecked', 'toBeEnabled', 'toBeDisabled',
  'toContainText', 'toHaveText', 'toHaveValue', 'toHaveCount',
]);

function finish(result) {
  process.stdout.write(`\n${JSON.stringify(result)}\n`);
}

function toValue(value) {
  if (value && typeof value === 'object' && typeof value.regex === 'string') {
    return new RegExp(value.regex, value.flags || '');
  }
  return value;
}

function buildLocator(page, chain) {
  let target = page;
  for (const part of chain) {
    if (!LOCATOR_METHODS.has(part.method)) throw new Error(`Blocked locator ${part.method}`);
    const args = (part.args || []).map(toValue);
    if (part.options) {
      args.push(Object.fromEntries(Object.entries(part.options).map(([key, value]) => [key, toValue(value)])));
    }
    target = target[part.method](...args);
  }
  return target;
}

function textMatches(actual, expected, contains) {
  const normalized = (actual || '').replace(/\s+/g, ' ').trim();
  if (expected instanceof RegExp) return expected.test(normalized);
  const wanted = String(expected).replace(/\s+/g, ' ').trim();
  return contains ? normalized.includes(wanted) : normalized === wanted;
}

async function observe(locator, step) {
  const expected = toValue(step.expected);
  const count = await locator.count();
  if (step.assertion === 'toHaveCount') return { ok: count === expected, actual: `${count} element(s)` };
  const visible = count > 0 && await locator.first().isVisible();
  if (step.assertion === 'toBeHidden') return { ok: !visible, actual: visible ? 'visible' : 'hidden' };
  if (count === 0) return { ok: false, actual: 'no matching element' };
  const element = locator.first();
  switch (step.assertion) {
    case 'toBeVisible':
      return { ok: visible, actual: visible ? 'visible' : 'hidden' };
    case 'toBeChecked': {
      const checked = await element.isChecked();
      return { ok: checked, actual: checked ? 'checked' : 'not checked' };
    }
    case 'toBeEnabled':
    case 'toBeDisabled': {
      const enabled = await element.isEnabled();
      return { ok: step.assertion === 'toBeEnabled' ? enabled : !enabled, actual: enabled ? 'enabled' : 'disabled' };
    }
    case 'toHaveValue': {
      const value = await element.inputValue();
      return { ok: textMatches(value, expected, false), actual: JSON.stringify(value) };
    }
    default: {
      const text = await element.innerText();
      return {
        ok: textMatches(text, expected, step.assertion === 'toContainText'),
        actual: JSON.stringify(text.slice(0, 200)),
      };
    }
  }
}

async function runAssertion(locator, step) {
  if (!ASSERTIONS.has(step.assertion)) throw new Error(`Blocked assertion ${step.assertion}`);
  const deadline = Date.now() + ASSERT_TIMEOUT_MS;
  let last = { ok: false, actual: 'not checked' };
  // Poll like Playwright's expect: the page may still be updating.
  while (true) {
    try {
      last = await observe(locator, step);
    } catch (error) {
      last = { ok: false, actual: `error: ${error.message.split('\n')[0]}` };
    }
    const passed = step.negated ? !last.ok : last.ok;
    if (passed || Date.now() >= deadline) return { passed, actual: last.actual };
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
}

async function runAction(locator, step) {
  if (!ACTIONS.has(step.action)) throw new Error(`Blocked action ${step.action}`);
  const options = { timeout: ACTION_TIMEOUT_MS, ...(step.options || {}) };
  if (step.value !== undefined) await locator[step.action](step.value, options);
  else await locator[step.action](options);
}

async function main() {
  const [stepsPath, baseUrlText, screenshotPath, tracePath] = process.argv.slice(2);
  const steps = JSON.parse(readFileSync(stepsPath, 'utf8'));
  const baseUrl = new URL(baseUrlText);
  if (!LOCAL_HOSTS.has(baseUrl.hostname)) throw new Error('The replay base URL must be local.');

  let chromium;
  try {
    ({ chromium } = await import('playwright'));
  } catch {
    finish({ status: 'UNAVAILABLE', reason: 'Playwright is not installed in browser_replay/ (run npm install there).' });
    return;
  }
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
  } catch (error) {
    finish({
      status: 'UNAVAILABLE',
      reason: `Chromium could not start (run "npx playwright install chromium" in browser_replay/): ${error.message.split('\n')[0]}`,
    });
    return;
  }

  const results = [];
  let failedIndex = null;
  let failureType = null;
  let context;
  let traceSaved = false;
  try {
    context = await browser.newContext();
    if (tracePath) await context.tracing.start({ screenshots: true, snapshots: true });
    // The replay may only talk to the local app.
    await context.route('**/*', (route) => {
      const url = new URL(route.request().url());
      if (['http:', 'https:', 'ws:', 'wss:'].includes(url.protocol) && !LOCAL_HOSTS.has(url.hostname)) {
        return route.abort('blockedbyclient');
      }
      return route.continue();
    });
    const page = await context.newPage();
    for (const [index, step] of steps.entries()) {
      const record = { index, line: step.line, action: step.action, source: step.source };
      try {
        if (step.action === 'goto') {
          await page.goto(new URL(step.path, baseUrl).href, { timeout: NAVIGATION_TIMEOUT_MS });
          record.status = 'passed';
        } else if (step.action === 'expect') {
          const outcome = await runAssertion(buildLocator(page, step.locator), step);
          record.status = outcome.passed ? 'passed' : 'failed';
          record.message = outcome.passed ? undefined : `Assertion failed; actual: ${outcome.actual}`;
        } else {
          await runAction(buildLocator(page, step.locator), step);
          record.status = 'passed';
        }
      } catch (error) {
        record.status = 'error';
        record.message = error.message.split('\n')[0].slice(0, 300);
      }
      results.push(record);
      if (record.status !== 'passed') {
        failedIndex = index;
        failureType = record.status === 'failed' ? 'assertion' : 'action';
        if (screenshotPath) {
          await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => {});
        }
        break;
      }
    }
  } finally {
    if (context && tracePath) {
      traceSaved = await context.tracing.stop({ path: tracePath }).then(() => true, () => false);
    }
    await browser.close();
  }
  finish({
    status: 'COMPLETED',
    trace_saved: traceSaved,
    steps_total: steps.length,
    results,
    failed_index: failedIndex,
    failure_type: failureType,
  });
}

main().catch((error) => {
  finish({ status: 'ERROR', reason: String(error && error.message ? error.message : error).slice(0, 300) });
});
