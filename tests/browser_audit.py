import json
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page, sync_playwright

BASE_URL = os.getenv("BROWSER_AUDIT_URL", "http://127.0.0.1:8765").rstrip("/")
OWNER_COOKIE = os.getenv("BROWSER_AUDIT_OWNER_COOKIE", "")
OUTPUT_DIR = Path("test-results/browser")


def attach_error_capture(page: Page) -> list[str]:
    errors: list[str] = []
    host = urlparse(BASE_URL).netloc
    page.on(
        "console",
        lambda message: (
            errors.append(f"console:{message.text}") if message.type == "error" else None
        ),
    )
    page.on("pageerror", lambda error: errors.append(f"page:{error}"))
    page.on(
        "requestfailed",
        lambda request: (
            errors.append(f"request:{request.method}:{request.url}:{request.failure}")
            if urlparse(request.url).netloc == host
            else None
        ),
    )
    return errors


def assert_layout(page: Page, name: str) -> dict:
    metrics = page.evaluate(
        """() => ({
          width: window.innerWidth,
          bodyWidth: document.body.scrollWidth,
          documentWidth: document.documentElement.scrollWidth,
          visibleText: (document.body.innerText || '').trim().length,
        })"""
    )
    if metrics["bodyWidth"] > metrics["width"] + 1:
        raise AssertionError(f"{name}: body has horizontal overflow: {metrics}")
    if metrics["documentWidth"] > metrics["width"] + 1:
        raise AssertionError(f"{name}: document has horizontal overflow: {metrics}")
    if metrics["visibleText"] < 40:
        raise AssertionError(f"{name}: page appears blank: {metrics}")
    return metrics


def audit_public(browser, viewport: dict, suffix: str) -> dict:
    context = browser.new_context(
        viewport=viewport,
        locale="he-IL",
        timezone_id="Asia/Jerusalem",
        reduced_motion="reduce",
    )
    page = context.new_page()
    errors = attach_error_capture(page)
    page.goto(f"{BASE_URL}/", wait_until="networkidle")
    page.locator(".service-card").first.wait_for(state="visible")
    metrics = assert_layout(page, f"public-{suffix}")
    page.screenshot(path=OUTPUT_DIR / f"public-{suffix}.png", full_page=True)

    page.locator(".service-card").first.click()
    page.locator("#nextTime").click()
    page.locator("#slots[aria-busy='false']").wait_for()
    assert_layout(page, f"slots-{suffix}")
    page.screenshot(path=OUTPUT_DIR / f"slots-{suffix}.png", full_page=True)
    context.close()
    if errors:
        raise AssertionError(f"public-{suffix}: browser errors: {errors}")
    return metrics


def audit_owner(browser, viewport: dict, suffix: str) -> dict:
    context = browser.new_context(
        viewport=viewport,
        locale="he-IL",
        timezone_id="Asia/Jerusalem",
        reduced_motion="reduce",
    )
    if OWNER_COOKIE:
        context.add_cookies(
            [
                {
                    "name": "booking_session",
                    "value": OWNER_COOKIE,
                    "url": BASE_URL,
                    "httpOnly": True,
                    "sameSite": "Lax",
                }
            ]
        )
    page = context.new_page()
    errors = attach_error_capture(page)
    page.goto(f"{BASE_URL}/owner.html", wait_until="networkidle")
    mode = "dashboard" if OWNER_COOKIE else "login"
    selector = "#admin:not([hidden])" if OWNER_COOKIE else ".login-card:not([hidden])"
    page.locator(selector).wait_for(state="visible")
    metrics = assert_layout(page, f"owner-{mode}-{suffix}")
    page.screenshot(path=OUTPUT_DIR / f"owner-{mode}-{suffix}.png", full_page=True)
    context.close()
    if errors:
        raise AssertionError(f"owner-{mode}-{suffix}: browser errors: {errors}")
    return metrics


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for suffix, viewport in (
                ("desktop", {"width": 1440, "height": 1000}),
                ("mobile", {"width": 390, "height": 844}),
            ):
                results[f"public-{suffix}"] = audit_public(browser, viewport, suffix)
                results[f"owner-{suffix}"] = audit_owner(browser, viewport, suffix)
        finally:
            browser.close()
    print(json.dumps(results, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
