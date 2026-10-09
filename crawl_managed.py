"""
Managed crawling via Crawlbase or Firecrawl.
Falls back to the existing crawl_site if no key is configured.
"""
import os, requests


def crawl_managed(url: str, mode: str = "markdown") -> str:
    """Crawl a URL using Crawlbase or Firecrawl.
    mode: markdown | html | screenshot"""
    cb_key = os.environ.get("CRAWLBASE_KEY", "")
    fc_key = os.environ.get("FIRECRAWL_KEY", "")
    if not cb_key and not fc_key:
        try:
            from config import load_config
            cfg = load_config()
            cb_key = cfg.get("CRAWLBASE_KEY", "")
            fc_key = cfg.get("FIRECRAWL_KEY", "")
        except Exception:
            pass

    if fc_key:
        try:
            r = requests.post(
                "https://api.firecrawl.dev/v1/scrape",
                headers={"Authorization": f"Bearer {fc_key}", "Content-Type": "application/json"},
                json={"url": url, "formats": [mode if mode != "screenshot" else "screenshot"]},
                timeout=30,
            )
            data = r.json()
            if data.get("success"):
                d = data.get("data", {})
                if mode == "screenshot":
                    return d.get("screenshot", "No screenshot returned.")
                return d.get("markdown" if mode == "markdown" else "html", "")[:8000]
            return f"ERROR: Firecrawl: {data.get('error', 'unknown')}"
        except Exception as e:
            return f"ERROR: Firecrawl request failed: {e}"

    if cb_key:
        try:
            api_url = f"https://api.crawlbase.com/?token={cb_key}&url={requests.utils.quote(url)}"
            if mode == "markdown":
                api_url += "&format=markdown"
            r = requests.get(api_url, timeout=30)
            return r.text[:8000]
        except Exception as e:
            return f"ERROR: Crawlbase request failed: {e}"

    # Fall back to the existing crawler
    import agent
    return agent.crawl_site(url)
