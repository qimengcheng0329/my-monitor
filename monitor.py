from __future__ import annotations

import asyncio
import json
import logging
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from playwright.async_api import (
    Browser,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

# --------------------------------------------------------------------------
# 🛠️ 终极配置控制台 (你以后想换商品或通知链接，直接在这里改中文！)
# --------------------------------------------------------------------------

# 1. 想同时监控什么，直接加在这个列表里（用英文逗号和双引号隔开）
KEYWORDS = ["捷安特 propel", "iPhone 16"]

# 2. 粘贴你接收微信通知的真实 Webhook 网址（如 WxPusher、Server酱等）
NOTIFICATION_WEBHOOK = "https://这里填你真实的通知通道API地址"


# --------------------------------------------------------------------------
# 自动化核心逻辑 (已完美注入反爬指纹外壳，彻底假装成普通人类浏览器)
# --------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOG = logging.getLogger("flea_monitor")

# 真实 Windows Chrome 用户代理
REAL_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

DEFAULT_MAX_RETRIES = 5
DEFAULT_BASE_DELAY_SECONDS = 3.0
DEFAULT_NAV_TIMEOUT_MS = 20_000
MIN_ACTION_DELAY = 4.0
MAX_ACTION_DELAY = 8.0


@dataclass(frozen=True)
class Target:
    name: str
    url: str
    item_selector: str = "div[class*='item-card']"
    title_selector: str = "div[class*='title-text']"
    price_selector: str = "span[class*='price-num']"
    webhook_url: str = ""


def generate_targets() -> list[Target]:
    targets = []
    for kw in KEYWORDS:
        encoded_kw = httpx.utils.quote(kw)
        targets.append(Target(
            name=kw,
            url=f"https://goofish.com{encoded_kw}",
            webhook_url=NOTIFICATION_WEBHOOK
        ))
    return targets


async def random_delay(min_s: float = MIN_ACTION_DELAY, max_s: float = MAX_ACTION_DELAY) -> None:
    delay = random.uniform(min_s, max_s)
    LOG.info("防封锁：模拟人类随机停顿 %.2fs...", delay)
    await asyncio.sleep(delay)


async def gentle_scroll(page: Page, steps: int = 5, step_delay: tuple[float, float] = (0.6, 1.5)) -> None:
    """模拟渐进式滑屏，触发懒加载并骗过行为风控"""
    for i in range(steps):
        scroll_fraction = (i + 1) / steps
        await page.evaluate(
            "frac => window.scrollTo({top: document.body.scrollHeight * frac, behavior: 'smooth'})",
            scroll_fraction,
        )
        await asyncio.sleep(random.uniform(*step_delay))


async def navigate_with_backoff(
    page: Page,
    target: Target,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY_SECONDS,
    nav_timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
) -> None:
    """带指数退避的健壮网络重试"""
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            LOG.info("[%s] 正在加载页面 (第 %d/%d 次)...", target.name, attempt, max_retries)
            await page.goto(target.url, timeout=nav_timeout_ms, wait_until="domcontentloaded")
            await page.wait_for_selector(target.item_selector, timeout=nav_timeout_ms)
            LOG.info("[%s] 页面元素成功捕获，访问合规！", target.name)
            return
        except PlaywrightTimeoutError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            backoff = base_delay * (2 ** (attempt - 1))
            jitter = random.uniform(0, 1.5)
            wait_time = backoff + jitter
            LOG.warning(
                "[%s] 第 %d 次触发加载延迟 (%s)，正在自动执行指数退避，%.2fs 后自动重试...",
                target.name, attempt, exc.__class__.__name__, wait_time,
            )
            await asyncio.sleep(wait_time)

    assert last_exc is not None
    raise last_exc


async def dump_failure_context(page: Page, target: Target) -> None:
    """异常快照捕获（出了人脸验证码会自动截图留在当前目录）"""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_name = "".join(c if c.isalnum() else "_" for c in target.name)

    screenshot_path = Path(f"error_{safe_name}_{timestamp}.png")
    html_path = Path("error_source.html")

    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
        LOG.error("已自动保存防封现场快照: %s", screenshot_path)
    except Exception:
        pass

    try:
        html = await page.content()
        html_path.write_text(html, encoding="utf-8")
    except Exception:
        pass


async def extract_items(page: Page, target: Target) -> list[dict[str, str]]:
    items = await page.query_selector_all(target.item_selector)
    results: list[dict[str, str]] = []

    for item in items:
        title_el = await item.query_selector(target.title_selector)
        price_el = await item.query_selector(target.price_selector)

        title = (await title_el.inner_text()).strip() if title_el else None
        price = (await price_el.inner_text()).strip() if price_el else None

        if title or price:
            results.append({"title": title, "price": price})

    return results


async def push_webhook(webhook_url: str, payload: dict[str, Any]) -> None:
    # 新版安全补丁：如果用户没配网址，直接在日志打印，绝不崩溃报错
    if "这里填你真实的" in webhook_url or not webhook_url.startswith("http"):
        LOG.info("【系统提示】检测到暂未配置有效的手机Webhook通知，本次抓取数据已成功在下方控制台打印。")
        return

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(webhook_url, json=payload)
            resp.raise_for_status()
            LOG.info("【通知】手机端通知推送成功！")
        except Exception as e:
            LOG.warning("【通知】Webhook 推送失败，但不影响程序继续运行: %s", e)



async def audit_single_target(browser: Browser, target: Target) -> None:
    context = await browser.new_context(
        user_agent=REAL_USER_AGENT,
        viewport={"width": 1920, "height": 1080},
        locale="zh-CN"
    )
    # 注入高级反爬指纹：强制抹除自动化特征检测，让平台风控完全将程序误认为正常人类
    await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    
    page = await context.new_page()

    try:
        try:
            await navigate_with_backoff(page, target)
        except Exception:
            LOG.error("[%s] 耗尽 5 次重试机会均告失败，正在保存现场证据并跳过该目标", target.name)
            await dump_failure_context(page, target)
            return

        await random_delay()
        await gentle_scroll(page)
        await random_delay()

        items = await extract_items(page, target)
        LOG.info("[%s] 成功监听并捕获到当前最新商品共计 %d 条！", target.name, len(items))
        
        # 本地控制台打印前 3 条看效果
        for idx, item in enumerate(items[:3], 1):
            LOG.info(f"   👉 商品 {idx}: 【{item['title']}】 价格: {item['price']} 元")

        await push_webhook(
            target.webhook_url,
            {
                "target": target.name,
                "status": "success",
                "url": target.url,
                "item_count": len(items),
                "items": items,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
    finally:
        await context.close()


async def run(headless: bool = True) -> None:
    targets = generate_targets()
    if not targets:
        LOG.warning("控制台未检测到监控关键词。")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            for target in targets:
                await audit_single_target(browser, target)
                await random_delay()
        finally:
            try:
                await browser.close()
            except Exception:
                pass


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(run(headless=True))
    except RuntimeError as e:
        # 特效补丁：彻底解决 Linux 环境下由于事件循环提前关闭导致的虚假报错崩溃
        if "Event loop is closed" not in str(e):
            raise

if __name__ == "__main__":
    main()
