from playwright.sync_api import sync_playwright

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
    html = page.content()
    for kw in ['dataLayerPagination', '__doPostBack', 'nextPageButton', 'resultListPaging', '__EVENTTARGET', '__EVENTVALIDATION', 'onsubmit']:
        idx = html.find(kw)
        print('KW', kw, idx)
        if idx != -1:
            start = max(0, idx - 600)
            end = min(len(html), idx + 3000)
            print(html[start:end])
            print('---')
    print('FORM OUTERHTML')
    print(page.locator('form').first.evaluate('el => el.outerHTML'))
    browser.close()
