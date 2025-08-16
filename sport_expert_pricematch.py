import os
import re
import time
import json
from typing import List, Tuple, Optional
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup

# ============ 环境变量 ============
START_URL  = os.getenv("START_URL", "https://www.sportsexperts.ca/en-CA/search?keywords=arc%27teryx").strip()
TIMEOUT    = int(os.getenv("TIMEOUT", "12"))          # 每个请求的超时（秒）
INTERVAL   = int(os.getenv("INTERVAL_SEC", "1800"))   # 轮询间隔（秒），默认30分钟
MAX_PAGES  = int(os.getenv("MAX_PAGES", "5"))         # 最多翻页数，提速用
UA         = os.getenv("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36")
WEBHOOK    = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

session = requests.Session()
session.headers.update({
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en,zh;q=0.9",
    "Cache-Control": "no-cache",
})

money_pat = re.compile(r"(?:\$|C?\s*\$)\s*([0-9]+(?:[.,][0-9]{2})?)")

def to_cents(s: str) -> int:
    s = s.replace(",", "").strip()
    if "." in s:
        d, c = s.split(".", 1)
        return int(d) * 100 + int(c[:2].ljust(2, "0"))
    return int(float(s) * 100)

# --------- 价格提取（加速&稳健）---------
def extract_prices_from_tag(tag) -> List[int]:
    texts = []
    # 优先在典型价格区域取文本（比全卡片快）
    for sel in (
        '[class*="price"]', '[class*="sale"]', '[class*="regular"]',
        '[data-price]', '[data-sale-price]', '[data-regular-price]',
        '.product__price', '.price__value', '.product-price', '.product__pricing'
    ):
        for el in tag.select(sel):
            txt = el.get_text(" ", strip=True)
            if txt:
                texts.append(txt)

    # 兜底：整卡片文本（可能含“$89.99 $44.99”）
    if not texts:
        full_txt = tag.get_text(" ", strip=True)
        if full_txt:
            texts.append(full_txt)

    amounts = []
    for t in texts:
        for m in money_pat.finditer(t):
            try:
                amounts.append(to_cents(m.group(1)))
            except Exception:
                pass
    return sorted(set(amounts))

def find_product_cards(soup: BeautifulSoup):
    cards = soup.select(
        '[class*="product-card"], [class*="product-tile"], [class*="product-item"], '
        '[data-product-id], [data-sku], li[class*="product"], article[class*="product"]'
    )
    if cards:
        return cards
    # 兜底范围（尽量少，避免拖慢）
    return soup.select('li, article, div')

def get_product_info(card, base_url):
    # 链接 & 名称（先从链接，再从标题元素）
    url = ""
    name = ""
    a = card.find("a", href=True)
    if a:
        url = urljoin(base_url, a["href"])
        name = (a.get("title") or a.get_text(" ", strip=True) or "").strip()

    if not name:
        name_el = card.select_one('h1, h2, h3, [class*="title"], [class*="name"]')
        if name_el:
            name = name_el.get_text(" ", strip=True)

    prices = extract_prices_from_tag(card)
    return {
        "name": name or "(no title)",
        "url": url,
        "prices": prices,
    }

def get_next_page_url(soup: BeautifulSoup, curr_url: str) -> Optional[str]:
    link = soup.find("a", rel=lambda v: v and "next" in v.lower(), href=True)
    if link:
        return urljoin(curr_url, link["href"])
    for a in soup.select('a[href*="page="], a[aria-label*="Next"], a[title*="Next"]'):
        if "next" in a.get_text(" ", strip=True).lower():
            return urljoin(curr_url, a["href"])
    # 猜 ?page=N
    try:
        parsed = urlparse(curr_url)
        q = parse_qs(parsed.query)
        curr_page = int(q.get("page", ["1"])[0])
        q["page"] = [str(curr_page + 1)]
        new_query = urlencode({k: v[0] for k, v in q.items()})
        return urlunparse(parsed._replace(query=new_query))
    except Exception:
        return None

def quick_get(url: str) -> Optional[str]:
    """带轻量重试的 GET"""
    for i in range(2):  # 最多2次
        try:
            r = session.get(url, timeout=TIMEOUT)
            if r.status_code >= 400:
                return None
            return r.text
        except requests.RequestException:
            if i == 1:
                return None
            time.sleep(0.5)
    return None

def scan_all_pages(start_url: str, max_pages: int) -> list:
    page_url = start_url
    seen = set()
    items = []

    for _ in range(max_pages):
        if not page_url or page_url in seen:
            break
        seen.add(page_url)

        html = quick_get(page_url)
        if not html:
            break

        soup = BeautifulSoup(html, "html.parser")

        # 如果页面上几乎没有 $ 金额，且没有任何 product 卡片，极可能是 JS 渲染，直接退出
        if "$" not in soup.get_text() and not soup.select('[data-product-id], [class*="product"]'):
            break

        cards = find_product_cards(soup)
        if not cards:
            break

        for c in cards:
            info = get_product_info(c, page_url)
            if not info["url"] or not info["prices"]:
                continue
            items.append(info)

        next_url = get_next_page_url(soup, page_url)
        if not next_url or next_url == page_url:
            break
        page_url = next_url

    return items

def choose_current_vs_original(prices_cents: List[int]) -> Optional[Tuple[int, int]]:
    uniq = sorted(set(prices_cents))
    if len(uniq) < 2:
        return None
    current, original = min(uniq), max(uniq)
    return (current, original) if current < original else None

def fmt_cents(cents: int) -> str:
    return f"${cents/100:.2f}"

def to_lines(on_sale: list) -> List[str]:
    lines = [f"Sports Expert共发现 {len(on_sale)} 个商品价格低于原价（最多扫描 {MAX_PAGES} 页）:", ""]
    for i, it in enumerate(on_sale, 1):
        lines.append(f"{i:>2}. {it['name']}")
        lines.append(f"    当前价: {fmt_cents(it['current'])} | 原价: {fmt_cents(it['original'])}")
        lines.append(f"    源网站: {it['url']}")
        lines.append("")
    # 末尾加一个空行，不再输出网站链接
    lines.append("")
    return lines

def post_discord(content: str):
    if not WEBHOOK:
        return
    try:
        resp = requests.post(WEBHOOK, json={"content": content}, timeout=10)
        if resp.status_code >= 300:
            print(f"[webhook] failed {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[webhook] error: {e}")

def run_once():
    items = scan_all_pages(START_URL, MAX_PAGES)
    if not items:
        msg = "未抓到任何商品（可能该页是JS渲染或被限速）。"
        print(msg)
        post_discord(msg if WEBHOOK else "")
        return

    on_sale = []
    for it in items:
        pair = choose_current_vs_original(it["prices"])
        if pair:
            curr, orig = pair
            on_sale.append({
                "name": it["name"],
                "url": it["url"],
                "current": curr,
                "original": orig
            })

    if not on_sale:
        msg = "未发现“当前价 ≠ 原价”的商品。"
        print(msg)
        post_discord(msg if WEBHOOK else "")
        return

    # 排序：按原价-现价差额降序
    on_sale.sort(key=lambda x: (x["original"] - x["current"]), reverse=True)
    lines = to_lines(on_sale)
    text = "\n".join(lines)
    print(text)
    if WEBHOOK:
        # 控制单次消息长度，分段发
        chunks = []
        buf = ""
        for line in lines:
            if len(buf) + len(line) + 1 > 1800:
                chunks.append(buf)
                buf = ""
            buf += (line + "\n")
        if buf:
            chunks.append(buf)
        for c in chunks:
            post_discord(c.strip())

def main_loop():
    print(f"[boot] start={START_URL} | interval={INTERVAL}s | max_pages={MAX_PAGES} | timeout={TIMEOUT}s")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[fatal] {e}")
        # 为了更像真人访问，轻微抖动（这里保持简单）
        sleep_s = INTERVAL
        time.sleep(max(10, sleep_s))

if __name__ == "__main__":
    main_loop()
