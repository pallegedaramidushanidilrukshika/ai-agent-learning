import { test, expect } from '@playwright/test';

test('test', async ({ page }) => {
  await page.goto('http://localhost:3000/');
  await page.locator('body').click();
  await page.getByRole('textbox', { name: 'add new task' }).click();
  await page.getByRole('textbox', { name: 'add new task' }).click();
  await page.getByRole('textbox', { name: 'add new task' }).click();
  await page.getByRole('textbox', { name: 'add new task' }).fill('      ');
  await page.getByRole('button', { name: 'Add Task' }).click();
  await expect(page.locator('input[name="check"]')).toHaveCount(2);
});
