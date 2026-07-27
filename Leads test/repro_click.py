from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
    page = browser.new_page(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36', viewport={'width': 1366, 'height': 900})
    page.goto('https://firmen.wko.at/gesundheit/tirol/', timeout=60000, wait_until='domcontentloaded')
    page.wait_for_timeout(8000)
    page.evaluate("""
    () => {
      const roots = document.querySelectorAll('#cmp-root, #cmp-backdrop, .cmp-backdrop, .fc-dialog-container, .fc-dialog-overlay');
      roots.forEach(el => { el.remove(); });
      document.body.style.overflow='auto';
      document.documentElement.style.overflow='auto';
      const style = document.createElement('style');
      style.textContent = '#cmp-root, #cmp-backdrop, .cmp-backdrop, .fc-dialog-container, .fc-dialog-overlay { display: none !important; visibility: hidden !important; pointer-events: none !important; }';
      document.head.appendChild(style);
    }
    """)
    page.wait_for_timeout(600)
    for i in range(3):
        count = len(BeautifulSoup(page.content(), 'lxml').select('a[href*="firmaid="]'))
        print('before click', i, count)
        btn = page.locator('input[value="Mehr laden"]').first
        print('button count', btn.count())
        if btn.count() > 0:
            btn.scroll_into_view_if_needed()
            btn.click(timeout=600, force=True)
            page.wait_for_timeout(600)
            count2 = len(BeautifulSoup(page.content(), 'lxml').select('a[href*="firmaid="]'))
            print('after click', i, count2)
    browser.close()
