"""Deep FB session re-auth after a Marketplace checkpoint.

`run.py --fb-login` refreshes a basic logged-in cookie but is not enough after
FB throws a checkpoint: /marketplace/item/<id>/ URLs keep redirecting to
/login even though the landing page passes. This opens a headed Chromium on a
real item page so you can complete FB's verification challenge, then closes the
context cleanly so the cookie state is saved back to .fb_profile/.

Run from the repo root in a separate terminal (it needs a visible window and
waits on Enter):

    .venv/bin/python scripts/fb_deep_login.py [item_url]

Launch settings (viewport, locale) match scrapers/facebook.py and the
refresh-fb-images path so the session fingerprint stays consistent.
"""
import sys
from playwright.sync_api import sync_playwright

DEFAULT_ITEM_URL = "https://www.facebook.com/marketplace/item/905718708749250/"


def main() -> int:
    item_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ITEM_URL
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=".fb_profile",
            headless=False,
            viewport={"width": 1366, "height": 900},
            locale="en-GB",
        )
        page = ctx.new_page()
        page.goto(item_url, timeout=60000)
        print("Window is open. Complete any FB verification challenge in the browser.")
        print("When the LISTING page is visible (price, description, images), return here and press Enter.")
        input()
        ctx.close()
        print("Context closed cleanly. Cookie state saved to .fb_profile/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
