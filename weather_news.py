"""
Weather + news tools. No API keys needed.

Weather: wttr.in — free, no key, works worldwide.
News:    RSS feeds via feedparser — free, works with any feed.

Requires: pip install requests feedparser
"""

import requests
import feedparser

# Curated news sources - add or swap as you like
NEWS_FEEDS = {
    "world":    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "malaysia": "https://www.thestar.com.my/rss/News/Nation",
    "tech":     "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "ai":       "https://www.artificialintelligence-news.com/feed/",
    "science":  "http://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
}


# ---------------------------
# WEATHER
# ---------------------------
def get_weather(location):
    """Get current weather + a short forecast for a location."""
    if not location or not location.strip():
        location = "Shah Alam"
    location = location.strip()

    try:
        # format=j1 returns JSON; we only need a few fields
        r = requests.get(
            f"https://wttr.in/{location}?format=j1",
            headers={"User-Agent": "curl/7.68"},
            timeout=15
        )
        if r.status_code != 200:
            return f"ERROR: Weather service returned {r.status_code}."

        data = r.json()

        current = data["current_condition"][0]
        area = data.get("nearest_area", [{}])[0]
        area_name = area.get("areaName", [{}])[0].get("value", location)
        country = area.get("country", [{}])[0].get("value", "")

        temp_c = current.get("temp_C")
        feels_c = current.get("FeelsLikeC")
        desc = current.get("weatherDesc", [{}])[0].get("value", "")
        humidity = current.get("humidity")
        wind_kmph = current.get("windspeedKmph")
        wind_dir = current.get("winddir16Point")

        out = (
            f"Weather for {area_name}, {country}:\n"
            f"- {desc}, {temp_c}°C (feels like {feels_c}°C)\n"
            f"- Humidity: {humidity}%\n"
            f"- Wind: {wind_kmph} km/h {wind_dir}\n"
        )

        # Add today's high/low
        try:
            today = data["weather"][0]
            out += (f"- Today's range: {today['mintempC']}°C to {today['maxtempC']}°C\n")
        except (KeyError, IndexError):
            pass

        return out.strip()
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------
# NEWS
# ---------------------------
def get_news(category="world", count=5):
    """
    Fetch top N headlines.
    Strategy:
      1. Try the RSS feed for that category (fast, no LLM).
      2. If RSS fails or returns nothing, fall back to:
         - search the web for "<category> news"
         - hand the real search snippets to the LLM to compile headlines
    """
    cat = (category or "world").strip().lower()
    if cat not in NEWS_FEEDS:
        return (f"ERROR: Unknown category '{cat}'. "
                f"Available: {', '.join(NEWS_FEEDS.keys())}")

    # ---- Attempt 1: RSS ----
    try:
        feed = feedparser.parse(NEWS_FEEDS[cat])
        entries = feed.entries[:count]
        if entries:
            lines = [f"Top {len(entries)} {cat} headlines:"]
            for i, e in enumerate(entries, 1):
                title = e.get("title", "(no title)").strip()
                link = e.get("link", "")
                lines.append(f"{i}. {title}\n   {link}")
            return "\n".join(lines)
    except Exception as e:
        print(f"[RSS failed for {cat}, falling back to LLM: {e}]")

    # ---- Attempt 2: LLM-compiled from live search ----
    print(f"[RSS empty for {cat} — using LLM fallback]")
    return _llm_compiled_news(cat, count)


def _llm_compiled_news(cat, count=5):
    """
    Fallback: search the web and have the LLM compile the results into
    a clean headline list. Used when RSS is dead.
    """
    import requests
    from bs4 import BeautifulSoup

    # Map category -> a good search query
    search_query = {
        "world":    "latest world news today",
        "malaysia": "latest Malaysia news today",
        "tech":     "latest tech news today",
        "ai":       "latest AI news today",
        "science":  "latest science news today",
        "asia":     "latest Asia news today",
    }.get(cat, f"latest {cat} news today")

    # Fetch real search results (titles + snippets + URLs)
    try:
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": search_query},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        for r in soup.select(".result")[:8]:
            link_el = r.select_one(".result__a")
            snippet_el = r.select_one(".result__snippet")
            if not link_el:
                continue
            title = link_el.get_text(strip=True)
            href = link_el.get("href", "")
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""
            if title and href:
                results.append(f"- {title}\n  {snippet}\n  {href}")

        if not results:
            return f"ERROR: No news found for '{cat}' via RSS or search."
    except Exception as e:
        return f"ERROR: Search fallback failed: {e}"

    # Hand the real search results to the LLM to compile
    prompt = (
        f"Below are real search results for '{search_query}'. "
        f"Compile the {count} most relevant and recent NEWS headlines "
        f"from these results only. Do NOT invent stories. "
        f"Format each as: N. <headline> — <source URL>. "
        f"If a result is not news (ads, opinion, old), skip it.\n\n"
        f"SEARCH RESULTS:\n" + "\n".join(results)
    )

    try:
        from agent import ask_llm_direct  # avoid circular at module load
    except Exception:
        # If we can't reach the agent, just return the raw results
        return (f"Top {cat} news (raw search results):\n"
                + "\n".join(results[:count]))

    compiled = ask_llm_direct(prompt)
    return f"Top {cat} headlines (compiled):\n{compiled}"