import os
import time
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

START_URL = os.getenv("START_URL", "https://www.sportsexperts.ca/en-CA/search?keywords=arc%27teryx")
INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "1800"))
TIMEOUT_SEC = int(os.getenv("TIMEOUT_SEC", "30"))
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

        # 滚动到底部加载所有商品
        log("开始滚动页面以加载全部商品...")
        prev_height = 0
        while True:
            page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            time.sleep(1.5)
            curr_height = page.evaluate("document.body.scrollHeight")
            if curr_height == prev_height:
                break
            prev_height = curr_height
        log("页面滚动完成，开始解析 HTML...")

        html = page.content()
        browser.close()

        soup = BeautifulSoup(html, "html.parser")
        items = soup.select("div.product__details")  # 改成实际存在的商品容器

        log(f"找到 {len(items)} 个商品")
        for idx, item in enumerate(items, start=1):
            title_el = item.select_one(".product__name")
            title_text = title_el.get_text(strip=True) if title_el else "(no title)"
            price_el = item.select_one("[data-price], [data-sale-price], [data-regular-price]")
            price_text = price_el.get_text(strip=True) if price_el else "N/A"
            orig_price_el = item.select_one("[data-regular-price]")
            orig_price_text = orig_price_el.get_text(strip=True) if orig_price_el else "N/A"
            link_el = item.find_parent("a")
            link_url = "https://www.sportsexperts.ca" + link_el.get("href") if link_el else "(no link)"

            log(f"{idx}. {title_text} | 当前价: {price_text} | 原价: {orig_price_text} | 链接: {link_url}")

def main():
    log(f"启动监控脚本 | start={START_URL} | interval={INTERVAL_SEC}s | timeout={TIMEOUT_SEC}s")
    fetch_products()  # 部署后立即执行
    while True:
        log("等待下次抓取...")
        time.sleep(INTERVAL_SEC)
        fetch_products()

if __name__ == "__main__":
    main()
