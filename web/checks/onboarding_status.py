"""Focused local UI proof: completion check plus a valid underscore-prefixed catalog."""

import argparse
from pathlib import Path
from urllib.parse import urlparse

from onboarding_sequence import OnboardingHandler, serve

original_get = OnboardingHandler.do_GET


def catalog(self):
    if urlparse(self.path).path == "/v1/views/pipedream/apps":
        return self.send(
            {
                "account": self.server.account,
                "configured": True,
                "apps": [
                    {
                        "provider": "_0codekit",
                        "name": "0codekit",
                        "auth_type": "keys",
                        "connectable": True,
                    },
                    {
                        "provider": "__1msg",
                        "name": "1msg",
                        "auth_type": "keys",
                        "connectable": True,
                    },
                    {
                        "provider": "gmail",
                        "name": "Gmail",
                        "auth_type": "oauth",
                        "connectable": True,
                    },
                ],
                "total": 3,
                "next_cursor": None,
            }
        )
    return original_get(self)


OnboardingHandler.do_GET = catalog


def main(shots):
    from playwright.sync_api import expect, sync_playwright

    shots.mkdir(parents=True, exist_ok=True)
    server = serve()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            context.route(
                "**/*",
                lambda route: (
                    route.continue_()
                    if urlparse(route.request.url).hostname == "127.0.0.1"
                    else route.abort()
                ),
            )
            # Control only the 1.5-second illustration timer, without waiting for 54 seconds.
            context.add_init_script("""(() => {
                const original = window.setTimeout; const ticks = [];
                window.setTimeout = (fn, ms, ...args) => ms === 1500
                    ? (ticks.push(() => fn(...args)), -ticks.length)
                    : original(fn, ms, ...args);
                window.advanceClaudeTick = () => {
                    const fn = ticks.shift();
                    if (!fn) throw Error('No preview tick');
                    fn();
                };
            })();""")
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}")
            page.get_by_role("button", name="Get Started", exact=True).click()
            page.locator('[data-guide="claude"]').click()
            page.get_by_role("button", name="I’ve requested it", exact=True).click()
            page.locator('[data-pick-export="claude"]').set_input_files(
                {
                    "name": "example.json",
                    "mimeType": "application/json",
                    "buffer": b"[]",
                }
            )
            page.get_by_role("button", name="Upload export", exact=True).click()
            expect(
                page.get_by_role("heading", name="Start with your conversations")
            ).to_be_visible()
            # The green check marks the upload, before the graph finishes growing.
            check = page.locator('[data-source-choice="claude"] .gp-source-check')
            expect(check).to_have_css("opacity", "1")
            assert (
                check.evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(22, 163, 74)"
            )
            expect(page.locator(".gp-complete-check")).to_have_count(0)
            for step in range(1, 37):
                page.evaluate("advanceClaudeTick()")
                page.wait_for_function(
                    "step => GroveConversationPreview.view().step === step", arg=step
                )
            expect(page.locator('[data-source-choice="claude"] strong small')).to_have_text("")
            expect(check).to_have_css("opacity", "1")
            expect(page.locator(".gp-complete-check")).to_be_visible()
            assert (
                page.locator(".gp-complete-check").evaluate(
                    "el => getComputedStyle(el).backgroundColor"
                )
                == "rgb(22, 163, 74)"
            )
            page.screenshot(path=str(shots / "claude-complete.png"), animations="disabled")
            page.get_by_role("button", name="Next step", exact=True).click()
            expect(page.locator(".gp-complete-check")).to_be_visible()
            # Six fixed rows, Gmail first, with no catalog dump and none of the old copy.
            names = page.locator("[data-pipedream-select] strong").all_inner_texts()
            assert names == [
                "Gmail",
                "Google Calendar",
                "Notion",
                "Slack",
                "Linear",
                "Dropbox",
            ], names
            for text in (
                "Check connections",
                "apps in the catalog",
                "Connect your accounts",
                "All apps",
                "0codekit",
            ):
                expect(page.get_by_text(text)).to_have_count(0)
            # A search ranks apps with a logo first, and shows no app without an account.
            page.get_by_role("searchbox").fill("code")
            expect(page.locator("[data-pipedream-select]").first).to_contain_text("Gmail")
            # A catalog name may start with an underscore, and its row stays usable.
            for name in ("Connect 0codekit", "Connect 1msg"):
                expect(page.get_by_role("button", name=name, exact=True)).to_be_enabled()
            page.get_by_role("searchbox").fill("")
            expect(page.locator("[data-pipedream-select]")).to_have_count(6)
            # A failed status read leaves the Connect control usable.
            expect(page.get_by_role("button", name="Connect Gmail", exact=True)).to_be_enabled()
            page.screenshot(path=str(shots / "pipedream-catalog.png"), animations="disabled")
            assert not errors, errors
            assert not server.uploads
            print(
                "PASS green check on upload, six fixed rows with Gmail first, logo ranking, "
                "underscore catalog rows, Gmail control, no old copy"
            )
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--shots", type=Path, required=True)
    main(parser.parse_args().shots)
