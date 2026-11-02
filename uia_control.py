"""Grounded Windows UI Automation helpers and agent tool wrappers."""
from __future__ import annotations

import importlib.util
import json

PYWINAUTO_AVAILABLE = importlib.util.find_spec("pywinauto") is not None

INTERACTIVE_TYPES = frozenset({
    "Button", "SplitButton", "CheckBox", "RadioButton",
    "Edit", "ComboBox", "Document",
    "Menu", "MenuBar", "MenuItem",
    "TabItem", "ListItem", "TreeItem", "Hyperlink",
    "Slider", "Spinner", "DataItem",
})
MAX_ELEMENTS = 300


def _desktop():
    if not PYWINAUTO_AVAILABLE:
        raise RuntimeError(
            "pywinauto is not installed. Run: pip install pywinauto")
    from pywinauto import Desktop
    return Desktop(backend="uia")


def _window_title(window):
    try:
        return str(window.window_text() or "").strip()
    except Exception:
        return ""


def get_app_window(title):
    """Return the best open window whose title contains ``title``."""
    query = str(title or "").strip()
    if not query:
        raise ValueError("window title is required")
    key = query.casefold()
    matches = []
    for window in _desktop().windows():
        window_title = _window_title(window)
        if not window_title:
            continue
        folded = window_title.casefold()
        if key not in folded:
            continue
        try:
            visible = bool(window.is_visible())
        except Exception:
            visible = False
        matches.append((
            not visible, folded != key, len(window_title), window))
    if not matches:
        raise LookupError(f"no open window matches '{query}'")
    matches.sort(key=lambda item: item[:3])
    return matches[0][3]


def _element_name(element):
    try:
        return str(element.window_text() or "").strip()
    except Exception:
        try:
            return str(element.element_info.name or "").strip()
        except Exception:
            return ""


def _control_type(element):
    try:
        return str(element.element_info.control_type or "")
    except Exception:
        return ""


def _automation_id(element):
    try:
        return str(element.element_info.automation_id or "")
    except Exception:
        return ""


def dump_tree(window, limit=MAX_ELEMENTS):
    """Return named interactive elements as simplified dictionaries."""
    if window is None:
        raise ValueError("window is required")
    limit = max(1, int(limit))
    items = []
    seen = set()
    for element in window.descendants():
        control_type = _control_type(element)
        if control_type not in INTERACTIVE_TYPES:
            continue
        name = _element_name(element)
        automation_id = _automation_id(element)
        if not name and not automation_id:
            continue
        key = (name, control_type, automation_id)
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "name": name,
            "type": control_type,
            "automation_id": automation_id,
        })
        if len(items) >= limit:
            break
    return items


def find_element(window, name):
    """Find an interactive element by automation_id or visible name."""
    if window is None:
        raise ValueError("window is required")
    query = str(name or "").strip()
    if not query:
        raise ValueError("element name is required")
    key = query.casefold()
    exact, partial = [], []
    for element in window.descendants():
        if _control_type(element) not in INTERACTIVE_TYPES:
            continue
        element_name = _element_name(element)
        automation_id = _automation_id(element)
        if automation_id and automation_id.casefold() == key:
            exact.append(element)
        elif element_name and element_name.casefold() == key:
            exact.append(element)
        elif element_name and key in element_name.casefold():
            partial.append(element)
    if exact:
        return exact[0]
    if partial:
        return partial[0]
    raise LookupError(f"no element matches '{query}'")


def click_element(window, name):
    element = find_element(window, name)
    element.click()
    return f"Clicked '{name}'."


def type_into_element(window, name, text):
    element = find_element(window, name)
    text = "" if text is None else str(text)
    try:
        element.set_focus()
    except Exception:
        pass
    element.type_keys(
        text, with_spaces=True, with_newlines=True, with_tabs=True)
    return f"Typed {len(text)} characters into '{name}'."


def uia_dump_tree(input_str):
    try:
        window = get_app_window(input_str)
        return json.dumps({
            "window": _window_title(window) or str(input_str).strip(),
            "elements": dump_tree(window),
        }, ensure_ascii=False)
    except Exception as exc:
        return f"ERROR: {exc}"


def uia_click(input_str):
    try:
        parts = str(input_str or "").split("|", 1)
        if len(parts) != 2:
            raise ValueError("uia_click needs 'window|element'")
        title, element = parts[0].strip(), parts[1].strip()
        if not title or not element:
            raise ValueError("uia_click needs 'window|element'")
        return click_element(get_app_window(title), element)
    except Exception as exc:
        return f"ERROR: {exc}"


def uia_type(input_str):
    try:
        parts = str(input_str or "").split("|", 2)
        if len(parts) != 3:
            raise ValueError("uia_type needs 'window|element|text'")
        title, element, text = parts[0].strip(), parts[1].strip(), parts[2]
        if not title or not element:
            raise ValueError("uia_type needs 'window|element|text'")
        return type_into_element(get_app_window(title), element, text)
    except Exception as exc:
        return f"ERROR: {exc}"
