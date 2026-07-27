from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36', viewport={'width': 1366, 'height': 900})
    page.on('request', lambda req: print('REQ', req.method, req.url) if 'firmaid' in req.url or 'nextPage' in req.url or 'load' in req.url.lower() or 'search' in req.url.lower() or 'api' in req.url.lower() else None)
    page.on('response', lambda res: print('RESP', res.status, res.url) if 'firmaid' in res.url or 'nextPage' in res.url or 'load' in res.url.lower() or 'search' in res.url.lower() or 'api' in res.url.lower() else None)
    page.goto('https://firmen.wko.at/gesundheit/tirol/', timeout=60000, wait_until='domcontentloaded')
    page.wait_for_timeout(8000)
    page.evaluate("""
    () => {
      const root = document.getElementById('cmp-root');
      if (root) root.remove();
      const backdrop = document.getElementById('cmp-backdrop');
      if (backdrop) backdrop.remove();
      document.body.style.overflow = 'auto';
      document.documentElement.style.overflow = 'auto';
      return true;
    }
    """)
    page.wait_for_timeout(1000)
    print('before', page.locator('a[href*="firmaid="]').count())
    button = page.locator('input[value="Mehr laden"]').first
    print('button exists', button.count())
    if button.count() > 0:
        print('button html', button.evaluate('el => el.outerHTML'))
        print('form info', button.evaluate("""
        el => {
          const form = el.form;
          return {
            formExists: !!form,
            formAction: form ? form.action : null,
            formMethod: form ? form.method : null,
            formId: form ? form.id : null,
            hiddenInputs: form ? Array.from(form.querySelectorAll('input[type="hidden"]')).map(i => ({name:i.name, value:i.value.slice(0,200)})) : []
          };
        }
        """))
        button.click(timeout=10000)
        page.wait_for_timeout(6000)
    print('after', page.locator('a[href*="firmaid="]').count())
    browser.close()
