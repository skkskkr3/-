"""Print non-sensitive connection diagnostics for the local MVP."""

import sqlite3
import re
from collections import Counter
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

from app import open_saved_browser_context, close_saved_browser_context, jwxt_profile_path, profile_path
import jwxt_adapter


with sync_playwright() as playwright:
    profile = profile_path(1)
    browser, context = open_saved_browser_context(playwright, profile)
    page = context.new_page()
    requests = []
    page.on("response", lambda item: requests.append((item.status, urlparse(item.url).hostname, urlparse(item.url).path)))
    response = page.goto("https://mooc1-api.chaoxing.com/mooc-ans/mooc2/work/all-task",
                         wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    html = page.content()
    address = urlparse(page.url)
    print("final_host_path", address.hostname, address.path)
    print("http_status", response.status if response else None)
    print("login_redirect", "passport" in page.url.lower() or "login" in page.url.lower())
    print("work_links", page.locator("a[href*='work'], a[href*='Work']").count())
    print("assignment_text", html.count("作业"))
    print("script_count", page.locator("script").count())
    print("iframe_count", page.locator("iframe").count())
    print("state_markers", {marker: marker in html for marker in ("待完成", "已完成", "暂无作业", "全部作业")})
    print("response_paths", sorted(set(requests))[:40])
    soup = BeautifulSoup(html, "html.parser")
    classes = sorted({name for tag in soup.find_all(True) for name in tag.get("class", [])})
    interactive = sorted({(tag.name, tuple(tag.get("class", [])), tuple(sorted(tag.attrs)))
                          for tag in soup.find_all(True) if tag.has_attr("onclick") or tag.has_attr("data")})
    print("class_names", classes)
    print("interactive_shapes", interactive[:30])
    task = soup.select_one(".task-list li[data], .task-list li[onclick]")
    if task:
        print("task_shape", [(node.name, tuple(node.get("class", [])), len(node.get_text(" ", strip=True)))
                             for node in task.find_all(True)])
        print("task_attributes", sorted(task.attrs))
        print("task_attribute_shapes", {key: re.sub(r"\d+", "N", str(value))[:300]
                                         for key, value in task.attrs.items() if key != "class"})
        status = task.select_one(".stuStatus")
        time_node = task.select_one(".time")
        print("task_status", status.get_text(" ", strip=True) if status else "missing")
        print("task_time_shape", re.sub(r"\d", "N", time_node.get_text(" ", strip=True)) if time_node else "missing")
        detail = context.request.get(task.get("data"), timeout=30000)
        detail_text = BeautifulSoup(detail.text(), "html.parser").get_text("\n", strip=True) if detail.ok else ""
        detail_address = urlparse(detail.url)
        print("detail_host_path_status", detail_address.hostname, detail_address.path, detail.status)
        print("detail_markers", {marker: marker in detail.text() for marker in ("发布时间", "截止时间", "作答时间", "提交时间", "未提交")})
        time_keys = sorted(set(re.findall(r"([A-Za-z_][A-Za-z0-9_]{2,30})\s*[:=]\s*['\"]20\d{2}[-/]", detail.text())))
        print("detail_time_keys", time_keys[:20])
        answer_times = re.findall(r"作答时间\s*[：:]?\s*([^\n]{1,80})", detail_text)
        print("detail_answer_time_shapes", [re.sub(r"\d", "N", value) for value in answer_times[:3]])
        for label in ("发布时间", "截止时间"):
            values = re.findall(label + r"\s*[：:]?\s*([0-9/\-:\s]{5,25})", detail_text)
            print("detail_" + label, [re.sub(r"\d", "N", value.strip()) for value in values[:3]])
    statuses = []
    for _ in range(20):
        statuses.extend(page.locator(".task-list .stuStatus").all_text_contents())
        next_button = page.locator(".xl-nextPage:not(.xl-disabled)")
        first = page.locator(".task-list li[data]").first.get_attribute("data") if page.locator(".task-list li[data]").count() else ""
        if next_button.count() == 0:
            break
        next_button.first.click()
        try:
            page.wait_for_function("previous => document.querySelector('.task-list li[data]')?.getAttribute('data') !== previous",
                                   arg=first, timeout=5000)
        except Exception:
            break
    print("all_task_statuses", Counter(value.strip() or "blank" for value in statuses))
    close_saved_browser_context(browser, context, profile)

    profile = jwxt_profile_path(1)
    browser, context = open_saved_browser_context(playwright, profile)
    response = context.request.get("https://jwxt.hue.edu.cn/jsxsd/framework/xsMain.jsp", timeout=30000)
    address = urlparse(response.url)
    print("jwxt_final_host_path", address.hostname, address.path)
    print("jwxt_http_status", response.status)
    print("jwxt_identity_found", bool(jwxt_adapter.identity(response.text())))
    print("jwxt_cookie_names", sorted({cookie["name"] for cookie in context.cookies()
                                       if str(cookie.get("domain", "")).lower().endswith("hue.edu.cn")}))
    close_saved_browser_context(browser, context, profile)
