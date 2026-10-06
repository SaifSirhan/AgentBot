"""
WhatsApp Web sender via Selenium.

First use: script opens web.whatsapp.com and you scan the QR code with
your phone. The session is saved in the Chrome profile - you only scan once.

After that, the agent can send messages to any of your contacts by name.

Requires: selenium, webdriver-manager, and the SAME Chrome profile used
by agent.py's other Selenium tools (AgentBrowserProfile).
"""

import os
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

WA_URL = "https://web.whatsapp.com/"
LOGIN_TIMEOUT = 60   # how long to wait for QR scan on first run


def _get_browser():
    """Reuse the same browser agent.py already manages."""
    import agent
    return agent.get_driver()


def _wait_for_whatsapp_ready(driver, timeout=20):
    """
    Wait until either the chat list appears (logged in) or the QR page shows.
    Returns (ready: bool, error_msg: str | None)
    """
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    # Give it a moment for the page to actually render
    time.sleep(3)

    # Check for QR / login page first
    try:
        src_lower = driver.page_source.lower()
    except Exception:
        src_lower = ""

    if "scan this qr code" in src_lower or "connect your phone" in src_lower:
        return False, ("ERROR: WhatsApp Web not logged in. "
                       "Open Chrome manually, go to web.whatsapp.com, "
                       "scan the QR code with your phone, then retry.")

    # Wait for either the chat list or the search box
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR,
                 'div[aria-label="Chat list"], '
                 'div[aria-label="Search input textbox"], '
                 'div[contenteditable="true"][data-tab="3"]')
            )
        )
        return True, None
    except Exception:
        return False, ("ERROR: WhatsApp chat list never loaded. "
                       "Possible causes: not logged in, layout changed, "
                       "or network slow. Try opening web.whatsapp.com manually first.")


def _find_search_box(driver):
    """Return the search input element or None."""
    selectors = [
        'div[aria-label="Search input textbox"]',
        'div[aria-label="Search or start new chat"]',
        'div[contenteditable="true"][data-tab="3"]',
        'div[contenteditable="true"][title="Search input textbox"]',
    ]
    for sel in selectors:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            if el:
                return el
        except Exception:
            continue
    return None


def _find_message_box(driver):
    """Return the message input element or None."""
    selectors = [
        'div[contenteditable="true"][data-tab="10"]',
        'div[contenteditable="true"][data-tab="6"]',
        'footer div[contenteditable="true"]',
        'div[aria-label="Type a message"]',
    ]
    for sel in selectors:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            if el:
                return el
        except Exception:
            continue
    return None


def send_message(contact, message):
    """
    Send a WhatsApp message to a contact by name.
    contact: name as it appears in your WhatsApp chats (partial is fine)
    message: text to send
    """
    if not contact or not message:
        return "ERROR: send_message needs both contact and message."

    driver = _get_browser()
    if driver is None:
        return "ERROR: Could not start browser."

    try:
        # Open WhatsApp Web if not already there
        if not driver.current_url.startswith("https://web.whatsapp.com"):
            driver.get(WA_URL)
            time.sleep(5)

        # Wait for either chat list or QR page
        ready, err = _wait_for_whatsapp_ready(driver)
        if not ready:
            return err

        # Find and use the search box
        search_box = _find_search_box(driver)
        if not search_box:
            return "ERROR: Could not find WhatsApp search box (layout may have changed)."

        search_box.click()
        time.sleep(0.5)
        search_box.send_keys(Keys.CONTROL + "a")
        search_box.send_keys(Keys.DELETE)
        time.sleep(0.5)

        search_box.send_keys(contact)
        time.sleep(2)
        search_box.send_keys(Keys.RETURN)
        time.sleep(1.5)

        # Find the message input
        msg_box = _find_message_box(driver)
        if not msg_box:
            return ("ERROR: Could not find WhatsApp message input box. "
                    "Contact search may have failed or no chat opened.")

        msg_box.click()
        time.sleep(0.5)
        msg_box.send_keys(message)
        time.sleep(0.5)
        msg_box.send_keys(Keys.RETURN)

        return f"Sent WhatsApp to '{contact}': {message[:80]}"

    except Exception as e:
        return f"ERROR: {e}"


def is_logged_in():
    """Quick check whether WhatsApp Web is already logged in."""
    driver = _get_browser()
    if driver is None:
        return False, "Browser not available."
    try:
        if not driver.current_url.startswith("https://web.whatsapp.com"):
            driver.get(WA_URL)
            time.sleep(5)
        ready, err = _wait_for_whatsapp_ready(driver, timeout=10)
        if ready:
            return True, "Logged in."
        return False, err or "Not logged in."
    except Exception as e:
        return False, str(e)