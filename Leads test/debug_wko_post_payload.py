from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
    page = browser.new_page(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36', viewport={'width': 1366, 'height': 900})

    captured = []
    def on_request(req):
        if 'firmen.wko.at' in req.url:
            captured.append((req.method, req.url, req.post_data()))
    page.on('request', on_request)

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

    btn = page.locator('input[value="Mehr laden"]').first
    print('button count', btn.count())
    if btn.count() > 0:
        page.evaluate("""
        () => {
          const btn = document.querySelector('input[value="Mehr laden"]');
          if (!btn) return false;
          const form = btn.form;
          if (!form) return false;
          const target = form.elements['__EVENTTARGET'];
          if (target) target.value = btn.name;
          const arg = form.elements['__EVENTARGUMENT'];
          if (arg) arg.value = '';
          const hidden = Array.from(form.querySelectorAll('input[type="hidden"]'));
          hidden.forEach(i => {
            if (i.name === 'ctl00$ContentPlaceHolder1$resultListPaging$controlsCountField') i.value = '0';
            if (i.name === 'ctl00$ContentPlaceHolder1$resultListPaging$pagingResultsKeyField') i.value = '';
          });
          form.requestSubmit();
          return true;
        }
        """)
        page.wait_for_timeout(8000)
        print('captured requests', len(captured))
        for item in captured[-8:]:
            print('---')
            print(item[0], item[1])
            print(item[2])
    browser.close()
