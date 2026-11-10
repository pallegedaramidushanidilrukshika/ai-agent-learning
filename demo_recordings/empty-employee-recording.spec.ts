import { test, expect } from '@playwright/test';

test('test', async ({ page }) => {
  await page.goto('http://127.0.0.1:54180/');
  await page.getByRole('link', { name: 'Create Employee' }).click();
  await page.getByRole('textbox', { name: 'Name' }).click();
  await page.getByRole('textbox', { name: 'Name' }).fill('Alice');
  await page.getByRole('textbox', { name: 'Position' }).click();
  await page.getByRole('textbox', { name: 'Position' }).click();
  await page.getByRole('textbox', { name: 'Position' }).fill('Engineer');
  await page.getByText('Intern').click();
  await page.getByRole('button', { name: 'Save Employee Record' }).click();
  await expect(page.getByText('Alice')).toBeVisible();
  await page.getByRole('link', { name: 'Create Employee' }).click();
  await page.getByRole('button', { name: 'Save Employee Record' }).click();
  await expect(page.locator('tbody tr')).toHaveCount(1);
});
