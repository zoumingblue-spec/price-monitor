"""
亚马逊竞品价格监控脚本
每日运行，自动抓取所有竞品 ASIN 的当前价格，追加写入 CSV
"""
import asyncio
import csv
import os
import re
import sys
from datetime import date
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ──────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
INPUT_CSV   = BASE_DIR / "competitor_asins.csv"
OUTPUT_CSV  = BASE_DIR / "competitor_prices.csv"
TODAY       = date.today().strftime("%Y-%m-%d")
DATE_COL    = f"Price_{TODAY}"
CONCURRENCY = 1          # 串行抓取，避免触发 Amazon 反爬
DELAY_MS    = 2000       # 每次导航后等待（毫秒）
# CI 环境自动切换为 headless，本地默认有界面
HEADLESS    = os.environ.get("CI", "") != ""


# ──────────────────────────────────────────────
# 从 Amazon 产品页面提取价格
# ──────────────────────────────────────────────
PRICE_SELECTORS = [
    # 主价格 (Buy Box)
    "#corePriceDisplay_desktop_feature_div .a-price-whole",
    # 降价 deal
    "#dealprice_inside_buybox .a-price-whole",
    # 旧版 price block
    "#priceblock_ourprice",
    "#priceblock_dealprice",
    # 通用 .a-offscreen（包含完整价格字符串）
    ".a-section.a-spacing-none.aok-align-center .a-price .a-offscreen",
    "#price_inside_buybox",
    ".a-price.aok-align-center.reinventPricePriceToPayMargin .a-offscreen",
]

async def is_bot_page(page) -> bool:
    """检测是否被跳转到验证码/机器人检测页面"""
    title = (await page.title()).lower()
    url   = page.url.lower()
    if "robot" in title or "captcha" in title or "sorry" in title:
        return True
    if "ref=cs_503" in url or "validateCaptcha" in url:
        return True
    return False


async def get_price(page) -> str:
    """返回价格字符串，如 '45.99'；无法获取时返回 'NA'"""
    if await is_bot_page(page):
        return "BOT"

    # 先等待价格区域出现
    try:
        await page.wait_for_selector(".a-price", timeout=5000)
    except Exception:
        pass

    # 先尝试 .a-offscreen 拿完整价格文本（最可靠）
    try:
        els = await page.query_selector_all(".a-price .a-offscreen")
        for el in els:
            txt = (await el.inner_text()).strip()
            txt = txt.replace("$", "").replace(",", "").strip()
            m = re.match(r"^\d+(\.\d+)?$", txt)
            if m:
                return txt
    except Exception:
        pass

    # 逐个选择器尝试
    for sel in PRICE_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el:
                txt = (await el.inner_text()).strip()
                txt = re.sub(r"[^0-9.]", "", txt)
                if txt and "." in txt:
                    return txt
                whole = re.sub(r"[^0-9]", "", txt)
                if whole:
                    frac_el = await page.query_selector(
                        sel.replace("-whole", "-fraction")
                        .replace("price_inside", "price_fraction")
                    )
                    frac = ""
                    if frac_el:
                        frac = re.sub(r"[^0-9]", "", await frac_el.inner_text())
                    return f"{whole}.{frac or '00'}"
        except Exception:
            continue

    return "NA"


# ──────────────────────────────────────────────
# 读取输入 CSV
# ──────────────────────────────────────────────
def load_asins(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            asin = row.get("ASIN", "").strip()
            if not asin or asin.lower() == "asin":
                continue
            rows.append({
                "产品线": row.get("产品线", "").strip(),
                "竞争品牌": row.get("竞争品牌", "").strip(),
                "竞品型号": row.get("竞品型号", "").strip(),
                "ASIN": asin,
            })
    return rows


# ──────────────────────────────────────────────
# 读/写输出 CSV（支持追加日期列）
# ──────────────────────────────────────────────
def load_output(path: Path) -> tuple[list[str], list[dict]]:
    """返回 (headers, rows)"""
    if not path.exists():
        return [], []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        rows = list(reader)
    return list(headers), rows


def save_output(path: Path, headers: list[str], rows: list[dict]):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ──────────────────────────────────────────────
# 主抓取逻辑
# ──────────────────────────────────────────────
async def scrape(asin_rows: list[dict]) -> dict[str, str]:
    """返回 {ASIN: price_str}"""
    results: dict[str, str] = {}
    sem = asyncio.Semaphore(CONCURRENCY)
    total = len(asin_rows)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            viewport={"width": 1280, "height": 800},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        # 隐藏 webdriver 特征，避免被 Amazon 识别为爬虫
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3] });
            window.chrome = { runtime: {} };
        """)

        async def fetch_one(idx: int, row: dict):
            asin = row["ASIN"]
            url  = f"https://www.amazon.com/dp/{asin}"
            async with sem:
                page = await context.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                    await page.wait_for_timeout(800)
                    price = await get_price(page)
                except PWTimeout:
                    price = "TIMEOUT"
                except Exception as e:
                    price = "ERR"
                finally:
                    await page.close()

                results[asin] = price
                status = "[OK]" if price not in ("NA", "TIMEOUT", "ERR") else "[NA]"
                brand  = row['竞争品牌']
                model  = row['竞品型号']
                print(f"  [{idx+1:>3}/{total}] {status} {brand:12s} {model:20s} {asin}  ->  {price}")
                sys.stdout.flush()
                await asyncio.sleep(DELAY_MS / 1000)

        tasks = [fetch_one(i, r) for i, r in enumerate(asin_rows)]
        await asyncio.gather(*tasks)
        await browser.close()

    return results


# ──────────────────────────────────────────────
# 合并结果到输出 CSV
# ──────────────────────────────────────────────
def merge(asin_rows: list[dict], prices: dict[str, str]):
    base_headers = ["产品线", "竞争品牌", "竞品型号", "ASIN", "US ASIN Link"]
    old_headers, old_rows = load_output(OUTPUT_CSV)

    # 建立 ASIN → 已有行 的映射
    existing: dict[str, dict] = {r["ASIN"]: r for r in old_rows}

    # 确定最终列顺序：保留旧日期列 + 追加今天
    date_cols = [h for h in old_headers if h.startswith("Price_")]
    if DATE_COL not in date_cols:
        date_cols.append(DATE_COL)
    new_headers = base_headers + date_cols

    new_rows = []
    for row in asin_rows:
        asin = row["ASIN"]
        merged = existing.get(asin, {})
        merged.update({
            "产品线":   row["产品线"],
            "竞争品牌": row["竞争品牌"],
            "竞品型号": row["竞品型号"],
            "ASIN":     asin,
            "US ASIN Link": f"https://www.amazon.com/dp/{asin}",
        })
        merged[DATE_COL] = prices.get(asin, "NA")
        new_rows.append(merged)

    save_output(OUTPUT_CSV, new_headers, new_rows)
    print(f"\nSaved: {OUTPUT_CSV}")
    print(f"Column: {DATE_COL}  |  Rows: {len(new_rows)}")


# ──────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────
async def main():
    print("=== Amazon competitor price monitor ===")
    print(f"Date: {TODAY}  |  Input: {INPUT_CSV.name}\n")

    asin_rows = load_asins(INPUT_CSV)
    print(f"Total: {len(asin_rows)} ASINs, start scraping...\n")
    sys.stdout.flush()

    prices = await scrape(asin_rows)

    # 统计
    ok   = sum(1 for v in prices.values() if v not in ("NA","TIMEOUT","ERR","BOT"))
    na   = sum(1 for v in prices.values() if v == "NA")
    bot  = sum(1 for v in prices.values() if v == "BOT")
    err  = sum(1 for v in prices.values() if v in ("TIMEOUT","ERR"))
    print(f"\nDone -> OK: {ok}  NA: {na}  BOT-blocked: {bot}  ERR: {err}")

    merge(asin_rows, prices)


if __name__ == "__main__":
    asyncio.run(main())
    # 本地运行时自动推送到 GitHub（CI 环境由 Actions 处理）
    if not os.environ.get("CI"):
        import subprocess
        try:
            subprocess.run(["git", "add", "competitor_prices.csv"], cwd=BASE_DIR, check=True)
            subprocess.run(["git", "commit", "-m", f"price update {TODAY}"], cwd=BASE_DIR, check=True)
            subprocess.run(["git", "push"], cwd=BASE_DIR, check=True)
            print("Git push OK")
        except subprocess.CalledProcessError as e:
            print(f"Git push skipped: {e}")
