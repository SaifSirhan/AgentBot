"""
SerpApi-powered web search for AgentBot.
Falls back to the existing search_web if no key is configured.
"""
import os


def serp_search(query: str, engine: str = "google") -> str:
    """Search using SerpApi. engine: google | google_news | google_images |
    google_scholar | google_maps | google_flights | google_hotels | youtube."""
    key = os.environ.get("SERPAPI_KEY", "")
    if not key:
        try:
            from config import load_config
            key = load_config().get("SERPAPI_KEY", "")
        except Exception:
            pass
    if not key:
        # Fall back to the existing scraper
        import agent
        return agent.search_web(query)

    try:
        from serpapi import Client
        client = Client(api_key=key)
        results = client.search({"q": query, "engine": engine, "num": 5})
        organic = results.get("organic_results", [])
        if not organic:
            return "No results."
        lines = []
        for r in organic[:5]:
            lines.append(f"- {r.get('title','')} — {r.get('link','')}\n  {r.get('snippet','')}")
        return "\n".join(lines)
    except ImportError:
        return "ERROR: serpapi-search-tools not installed. Run: pip install serpapi-search-tools"
    except Exception as e:
        return f"ERROR: SerpApi search failed: {e}"
