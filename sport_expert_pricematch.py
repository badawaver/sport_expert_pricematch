import os
import time
import asyncio
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

# 从环境变量读取配置
START_URL = os.getenv("START_URL", "https://www.sportsexperts.ca/en-CA/search?keywords=arc%27teryx")
INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "1800"))
TIMEOUT_SEC = int(os.getenv("TIMEOUT_SEC", "15"))
UA = os.getenv("UA", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36")

def log(msg):
    print(f"[LOG] {msg}", flush=True)

def fetch_products():
    log(f"开始抓取: {START_URL}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(user_agent=UA)
        log("浏览器已启动，开始加载页面...")
        page.goto(START_URL, timeout=TIMEOUT_SEC * 1000)
        log("页面加载完成，开始解析 HTML...")

        html = page.content()
        browser.close()

        soup = BeautifulSoup(html, "html.parser")
        items = soup.select("li.product-tile")

        log(f"找到 {len(items)} 个商品")
        results = []

        for idx, item in enumerate(items, start=1):
            title = item.get("data-name") or item.select_one("a.name-link")
            title_text = title.get_text(strip=True) if title and hasattr(title, "get_text") else "(no title)"
            price = item.get("data-price", "").strip()
            sale_price = item.get("data-sale-price", "").strip()
            regular_price = item.get("data-regular-price", "").strip()
            link = item.select_one("a.name-link")
            link_url = "https://www.sportsexperts.ca" + link.get("href") if link else "(no link)"

            log(f"{idx}. {title_text} | 当前价: {sale_price or price} | 原价: {regular_price or price} | 链接: {link_url}")
            results.append({
                "title": title_text,
                "price": sale_price or price,
                "regular_price": regular_price or price,
                "link": link_url
            })
        return results

def main():
    log(f"启动监控脚本 | start={START_URL} | interval={INTERVAL_SEC}s | timeout={TIMEOUT_SEC}s")
    
    # 部署后立即执行一次
    try:
        fetch_products()
    except Exception as e:
        log(f"第一次抓取出错: {e}")

    # 循环执行
    while True:
        log("等待下次抓取...")
        time.sleep(INTERVAL_SEC)
        try:
            fetch_products()
        except Exception as e:
            log(f"抓取出错: {e}")

if __name__ == "__main__":
    main()
