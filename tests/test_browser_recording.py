import unittest

from browser_recording import RecordingError, parse_recording


JS_TEST_RECORDING = """import { test, expect } from '@playwright/test';

test('test', async ({ page }) => {
  await page.goto('http://localhost:3000/');
  await page.getByPlaceholder('New task').click();
  await page.getByPlaceholder('New task').fill('   ');
  await page.getByRole('button', { name: 'Add Task' }).click();
  // the empty task must not appear
  await expect(page.locator('#task-list li')).toHaveCount(2);
  await expect(page.getByText(/^buy socks$/i)).toBeVisible();
  await expect(page.getByRole('listitem').nth(2)).not.toBeVisible();
});
"""

JS_LIBRARY_RECORDING = """const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({
    headless: false
  });
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto('http://127.0.0.1:5173/chat?room=1');
  await page.getByLabel('Message').fill('hello');
  await page.getByLabel('Message').press('Enter');
  await expect(page.locator('li').filter({ hasText: 'hello' })).toHaveCount(1);
  // ---------------------
  await context.close();
  await browser.close();
})();
"""

PY_RECORDING = """import re
from playwright.sync_api import Page, expect


def test_example(page: Page) -> None:
    page.goto("http://localhost:3000/")
    page.get_by_role("textbox", name="New task").fill("hello")
    page.get_by_role("button", name="Add", exact=True).click()
    expect(page.get_by_role("listitem").first).to_contain_text(re.compile(r"hel+o", re.IGNORECASE))
    expect(page.get_by_text("hello")).not_to_be_hidden()
"""


def steps_without_source(result):
    return [
        {key: value for key, value in step.items() if key not in ("line", "source")}
        for step in result["steps"]
    ]


class ParseRecordingTests(unittest.TestCase):
    def test_playwright_test_script(self):
        result = parse_recording(JS_TEST_RECORDING, "bug.spec.ts")
        self.assertEqual(result["language"], "javascript")
        self.assertEqual(result["recorded_origin"], "http://localhost:3000")
        self.assertEqual(result["assertions"], 3)
        steps = steps_without_source(result)
        self.assertEqual(steps[0], {"action": "goto", "path": "/"})
        self.assertEqual(steps[2], {
            "action": "fill",
            "locator": [{"method": "getByPlaceholder", "args": ["New task"]}],
            "value": "   ",
        })
        self.assertEqual(steps[3]["locator"], [
            {"method": "getByRole", "args": ["button"], "options": {"name": "Add Task"}},
        ])
        self.assertEqual(steps[4]["assertion"], "toHaveCount")
        self.assertEqual(steps[4]["expected"], 2)
        self.assertEqual(steps[5]["locator"][0]["args"], [{"regex": "^buy socks$", "flags": "i"}])
        self.assertTrue(steps[6]["negated"])
        self.assertEqual(steps[6]["locator"][1], {"method": "nth", "args": [2]})
        self.assertEqual(result["steps"][1]["line"], 5)

    def test_library_script_keeps_path_and_query(self):
        result = parse_recording(JS_LIBRARY_RECORDING, "recording.js")
        self.assertEqual(result["recorded_origin"], "http://127.0.0.1:5173")
        self.assertEqual(result["steps"][0]["path"], "/chat?room=1")
        self.assertEqual(result["steps"][2]["value"], "Enter")
        self.assertEqual(result["steps"][3]["locator"][1], {
            "method": "filter", "args": [], "options": {"hasText": "hello"},
        })

    def test_python_script(self):
        result = parse_recording(PY_RECORDING, "test_bug.py")
        self.assertEqual(result["language"], "python")
        steps = steps_without_source(result)
        self.assertEqual(steps[1]["locator"][0]["options"], {"name": "New task"})
        self.assertEqual(steps[2]["locator"][0]["options"], {"name": "Add", "exact": True})
        self.assertEqual(steps[3]["locator"][1], {"method": "first", "args": []})
        self.assertEqual(steps[3]["assertion"], "toContainText")
        self.assertEqual(steps[3]["expected"], {"regex": "hel+o", "flags": "i"})
        self.assertEqual(steps[4]["assertion"], "toBeHidden")
        self.assertTrue(steps[4]["negated"])

    def test_script_hash_is_recorded(self):
        first = parse_recording(JS_TEST_RECORDING, "a.ts")
        second = parse_recording(JS_TEST_RECORDING + "\n", "a.ts")
        self.assertEqual(len(first["script_sha256"]), 64)
        self.assertNotEqual(first["script_sha256"], second["script_sha256"])

    def assert_rejected(self, body, message, filename="bug.spec.ts"):
        script = (
            "import { test, expect } from '@playwright/test';\n"
            "test('t', async ({ page }) => {\n"
            "  await page.goto('http://localhost:3000/');\n"
            f"{body}\n"
            "  await expect(page.getByText('x')).toBeVisible();\n"
            "});\n"
        )
        with self.assertRaises(RecordingError) as caught:
            parse_recording(script, filename)
        self.assertIn(message, str(caught.exception))
        return caught.exception

    def test_rejects_code_execution(self):
        self.assert_rejected("  require('child_process').execSync('calc');", "steps must start from page")
        self.assert_rejected("  await page.evaluate(() => fetch('http://evil'));", "Line 4")
        self.assert_rejected("  await page.evaluate('fetch(1)');", "expected a locator")
        self.assert_rejected("  const fs = require('fs');", "Line 4")
        self.assert_rejected("  await page.getByText(`${process.env.KEY}`).click();", "template expressions")
        self.assert_rejected("  await page.getByText(x).click();", "only plain text")

    def test_rejects_non_local_and_mixed_origins(self):
        error = self.assert_rejected("  await page.goto('https://example.com/');", "only open the local app")
        self.assertIn("Line 4", str(error))
        self.assert_rejected("  await page.goto('http://localhost:4000/');", "same local app")
        self.assert_rejected("  await page.goto('file:///C:/secrets.txt');", "http(s) URL")
        self.assert_rejected("  await page.goto('http://user:pw@localhost:3000/');", "credentials")

    def test_rejects_unsupported_actions_and_options(self):
        self.assert_rejected("  await page.getByText('a').setInputFiles('C:/x');", "not an allowed action")
        self.assert_rejected("  await page.getByText('a').click({ force: true });", "options are not allowed")
        self.assert_rejected("  await page.getByText('a').press('Enter; rm');", "")
        self.assert_rejected("  await page.getByRole('Button!').click();", "invalid ARIA role")
        self.assert_rejected("  await page.locator('a').evaluateAll(x);", "not an allowed action")
        self.assert_rejected("  await page.getByText('a').click(); extra", "unexpected text")
        self.assert_rejected("  await page.getByText('" + "a" * 600 + "').click();", "limited to 500")

    def test_requires_goto_first_and_an_assertion(self):
        with self.assertRaises(RecordingError) as caught:
            parse_recording("await page.getByText('a').click();\nawait page.goto('http://localhost:3000/');")
        self.assertIn("must start by opening a page", str(caught.exception))
        with self.assertRaises(RecordingError) as caught:
            parse_recording("await page.goto('http://localhost:3000/');\nawait page.getByText('a').click();")
        self.assertIn("no assertion", str(caught.exception))
        with self.assertRaises(RecordingError):
            parse_recording("")

    def test_size_limits(self):
        with self.assertRaises(RecordingError) as caught:
            parse_recording("// x\n" * 20000)
        self.assertIn("larger than", str(caught.exception))
        with self.assertRaises(RecordingError) as caught:
            parse_recording("\n" * 500)
        self.assertIn("more than 400 lines", str(caught.exception))

    def test_python_rejects_other_modules(self):
        with self.assertRaises(RecordingError) as caught:
            parse_recording(
                "import os\nfrom playwright.sync_api import Page, expect\n"
                "def test_x(page: Page) -> None:\n"
                "    page.goto('http://localhost:3000/')\n"
                "    os.system('calc')\n"
                "    expect(page.get_by_text('x')).to_be_visible()\n",
                "test_x.py",
            )
        message = str(caught.exception)
        self.assertIn("Line 1", message)
        self.assertIn("Line 5", message)


if __name__ == "__main__":
    unittest.main()
