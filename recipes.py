"""Deterministic browser recipes — fixed click/type/wait scripts, no LLM.

Steps in recipes.json support these actions:
  goto      value=URL
  wait_for  selector=CSS, timeout=seconds (default 15)
  click     selector=CSS
  type      selector=CSS, value=text
  wait      value=seconds
  screenshot value=filename (saved to Downloads)

Don't put passwords in recipes.json — it is plain text on disk.
"""

import os, json, time
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

RECIPES_FILE = os.path.join(os.path.dirname(__file__), "recipes.json")


def load_recipes():
    if not os.path.exists(RECIPES_FILE):
        return {}
    with open(RECIPES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def run_recipe(name):
    recipes = load_recipes()
    if name not in recipes:
        return f"ERROR: recipe '{name}' not found. Available: {list(recipes.keys())}"
    recipe = recipes[name]
    import agent
    driver = agent.get_driver()
    if driver is None:
        return "ERROR: could not start browser"
    results = []
    for step in recipe.get("steps", []):
        action = step.get("action")
        try:
            if action == "goto":
                driver.get(step["value"])
                results.append(f"navigated to {step['value']}")
            elif action == "wait_for":
                WebDriverWait(driver, step.get("timeout", 15)).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, step["selector"]))
                )
                results.append(f"found {step['selector']}")
            elif action == "click":
                el = driver.find_element(By.CSS_SELECTOR, step["selector"])
                el.click()
                results.append(f"clicked {step['selector']}")
            elif action == "type":
                el = driver.find_element(By.CSS_SELECTOR, step["selector"])
                el.clear(); el.send_keys(step["value"])
                results.append(f"typed into {step['selector']}")
            elif action == "wait":
                time.sleep(step.get("value", 2))
                results.append(f"waited {step.get('value', 2)}s")
            elif action == "screenshot":
                path = os.path.join(agent.get_downloads_path(), step["value"])
                driver.save_screenshot(path)
                results.append(f"screenshot: {path}")
            else:
                results.append(f"WARNING: unknown action '{action}'")
        except Exception as e:
            results.append(f"ERROR at step '{action}': {e}")
            break
    return f"Recipe '{name}' complete:\n" + "\n".join(results)
