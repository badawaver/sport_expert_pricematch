import os
import re
import time
from typing import List, Tuple, Optional, Dict
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup, Tag

# ============ 环境变量 ============
START_URL  = os.getenv("START_URL", "https://www.sportsexperts.ca/en-CA/search?keywords=arc%27teryx").strip()
TIMEOUT    = int(os.getenv("TIMEOUT", "12"))          # 每个请求的超时（秒）
INTERVAL   = int(os.getenv("INTERVAL_SEC", "1800"))   # 轮询间隔（秒），默认30分钟
MAX_PAGES  = int(os.getenv("MAX_PAGES", "5"))         # 最多翻页数
UA         = os.getenv("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36")
WEBHOOK    = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

session = requests.Session()
session.headers.update({
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en,zh;q=0.9",
    "Cache-Control": "no-cache",
})

money_pat = re.compile(r"(?:C?\s*\$)\s*([0-9]+(?:[.,][0-9]{2})?)", re.I)
sale_words = re.compile(r"(sale|save|now|compare|regular|reg\.?|was)", re.I)

def to_cents(s: str) -> int:
    s = s.replace(",", "").strip()
    if "." in s:
        d, c = s.split(".", 1)
        return int(d) * 100 + int(c[:2].ljust(2, "0"))
    return int(float(s) * 100)

def fmt_cents(cents: int) -> str:
    return f"${cents/100:.2f}"

# --------- DOM 内语义成对提价：先精准后兜底 ---------
def extract_price_pairs(card: Tag) -> List[Tuple[int,int]]:
    """
    从同一卡片内更“语义化”地寻找 (current, original) 对：
    - 常见类名：sale/sales/regular/compare/was/now
    - 邻近文本关键字
    - 结构化区块（同一父节点包含两个金额）
    只收集 current < original 的对。
    """
    pairs: List[Tuple[int,int]] = []

    # 1) 典型类名映射（尽可能通用）
    sale_selectors = [
        '[class*="sale"]', '[class*="sales"]', '[data-sale]', '.price__sale', '.price--sale', '.sale'
    ]
    reg_selectors = [
        '[class*="regular"]', '[class*="compare"]', '[class*="original"]', '.price__regular', '.price--compare', '.price--was', '.was'
    ]

    def amounts_from(nodes: List[Tag]) -> List[int]:
        out = []
        for n in nodes:
            txt = n.get_text(" ", strip=True)
            for m in money_pat.finditer(txt):
                try:
                    out.append(to_cents(m.group(1)))
                except:  # noqa
                    pass
        return out

    sale_nodes = []
    for sel in sale_selectors:
        sale_nodes += card.select(sel)
    reg_nodes = []
    for sel in reg_selectors:
        reg_nodes += card.select(sel)

    sale_vals = amounts_from(sale_nodes)
    reg_vals  = amounts_from(reg_nodes)
    for s in sale_vals:
        for r in reg_vals:
            if s < r:
                pairs.append((s, r))

    # 2) 父级块里近邻两价格（同时出现关键词）
    #    例如： "Now $129.99  Was $199.99" 或 "Sale $149.99  Regular $189.99"
    blocks = card.select('.price, .product__price, .product-price, [class*="price"]')
    if not blocks:
        blocks = [card]
    for blk in blocks:
        txt = blk.get_text(" ", strip=True)
        if not sale_words.search(txt):
            continue
        amts = [to_cents(m.group(1)) for m in money_pat.finditer(txt)]
        # 常见 “Now/Was” 两个价格
        if len(amts) >= 2:
            # 尝试按“带 now/sale 的在前”去匹配
            # 简化策略：枚举相邻对
            for i in range(len(amts)-1):
                cur, orig = amts[i], amts[i+1]
                if cur < orig and (cur, orig) not in pairs:
                    pairs.append((cur, orig))

    # 去重
    pairs = sorted(set(pairs))
    return pairs

def find_product_cards(soup: BeautifulSoup):
    cards = soup.select(
        '[class*="product-card"], [class*="productTile"], [class*="product-tile"], '
        '[class*="product-item"], [data-product-id], [data-sku], li[class*="product"], '
        'article[class*="product"]'
    )
    if cards:
        return cards
    return soup.select('li, article, div')

def get_product_info(card: Tag, base_url: str) -> Dict:
    # 链接 & 名称
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

    pairs = extract_price_pairs(card)
    return {
        "name": name or "(no title)",
        "url": url,
        "pairs": pairs,  # List[(current, original)]
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

        # 简单反爬/JS渲染判断
        if "$" not in soup.get_text() and not soup.select('[data-product-id], [class*="product"]'):
            break

        cards = find_product_cards(soup)
        if not cards:
            break

        for c in cards:
            info = get_product_info(c, page_url)
            if not info["url"] or not info["pairs"]:
                continue
            items.append(info)

        next_url = get_next_page_url(soup, page_url)
        if not next_url or next_url == page_url:
            break
        page_url = next_url

    return items

def to_lines(on_sale: list) -> List[str]:
    lines = [f"Sports Experts 共发现 {len(on_sale)} 个打折商品（最多扫描 {MAX_PAGES} 页）:", ""]
    for i, it in enumerate(on_sale, 1):
        lines.append(f"{i:>2}. {it['name']}")
        lines.append(f"    当前价: {fmt_cents(it['current'])} | 原价: {fmt_cents(it['original'])} | 折扣: -{it['discount_pct']:.0f}%")
        lines.append(f"    源网站: {it['url']}")
        lines.append("")
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
        msg = "未抓到任何‘现价/原价’成对价格的商品（可能该页是JS渲染或无折扣展示）。"
        print(msg)
        post_discord(msg if WEBHOOK else "")
        return

    on_sale = []
    # 用 URL 去重：同一商品卡片可能重复出现（例如不同颜色）
    seen_urls = set()
    for it in items:
        if it["url"] in seen_urls:
            continue
        seen_urls.add(it["url"])

        # 可能一张卡片里抓到多个 pair（例如多颜色/多尺码并列显示）
        # 取“折扣最大”的一对
        best = None
        best_diff = 0
        for (curr, orig) in it["pairs"]:
            if curr < orig:
                diff = orig - curr
                if diff > best_diff:
                    best_diff = diff
                    best = (curr, orig)
        if best:
            curr, orig = best
            discount_pct = (orig - curr) / orig * 100.0
            on_sale.append({
                "name": it["name"],
                "url": it["url"],
                "current": curr,
                "original": orig,
                "discount_pct": discount_pct
            })

    if not on_sale:
        msg = "未发现“当前价 < 原价”的打折商品。"
        print(msg)
        post_discord(msg if WEBHOOK else "")
        return

    # 按折扣额降序
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
        time.sleep(max(10, INTERVAL))

if __name__ == "__main__":
    main_loop()
