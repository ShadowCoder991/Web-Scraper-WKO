from playwright.sync_api import sync_playwright
import re

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
    page = browser.new_page(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36', viewport={'width': 1366, 'height': 900})
    page.goto('https://firmen.wko.at/gesundheit/tirol/', timeout=60000, wait_until='domcontentloaded')
    page.wait_for_timeout(8000)
    page.evaluate("""
    () => {
      const roots = document.querySelectorAll('#cmp-root, #cmp-backdrop');
      roots.forEach(el => { el.remove(); });
      document.body.style.overflow='auto';
      document.documentElement.style.overflow='auto';
    }
    """)
    page.wait_for_timeout(1000)
    text = page.content()
    matches = re.findall(r'__doPostBack\(\s*\"([^\"]+)\"\s*,\s*\"([^\"]*)\"\s*\)', text)
    print('matches', matches[:10])
    print('form action', page.locator('form#aspnetForm').first.evaluate('el => el.action'))
    btn = page.locator('input[value="Mehr laden"]').first
    print('button outerHTML', btn.evaluate('el => el.outerHTML'))
    print('button form info', btn.evaluate('''
    el => {
      const form = el.form;
      return form ? {
        action: form.action,
        method: form.method,
        id: form.id,
        hidden: Array.from(form.querySelectorAll('input[type="hidden"]')).map(i => i.name)
      } : null;
    }
    '''))
    browser.close()
