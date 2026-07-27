from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
    page = browser.new_page(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36', viewport={'width': 1366, 'height': 900})
    page.goto('https://firmen.wko.at/gesundheit/tirol/', timeout=60000, wait_until='domcontentloaded')
    page.wait_for_timeout(8000)
    page.evaluate("""
    () => {
      const roots = document.querySelectorAll('#cmp-root, #cmp-backdrop, .cmp-backdrop');
      roots.forEach(el => { el.remove(); });
      document.body.style.overflow='auto';
      document.documentElement.style.overflow='auto';
      const style = document.createElement('style');
      style.textContent = '#cmp-root, #cmp-backdrop, .cmp-backdrop { display: none !important; }';
      document.head.appendChild(style);
    }
    """)
    page.wait_for_timeout(1000)

    def count_links():
        return len(BeautifulSoup(page.content(), 'lxml').select('a[href*="firmaid="]'))

    print('before', count_links())
    btn = page.locator('input[value="Mehr laden"]').first
    print('button count', btn.count())
    if btn.count() > 0:
        try:
            btn.click(timeout=20000, force=True)
            print('clicked via click()')
        except Exception as e:
            print('click failed', e)
            try:
                btn.evaluate('el => { el.click(); }')
                print('clicked via evaluate click()')
            except Exception as e2:
                print('evaluate failed', e2)
        page.wait_for_timeout(8000)
        print('after', count_links())
        print('body snippet', page.locator('body').inner_text()[:2000])
    browser.close()
