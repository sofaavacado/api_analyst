import sys
import asyncio
import re

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, Query
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

app = FastAPI(title="Trudvsem scraper")

BASE_URL = "https://trudvsem.ru"


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def extract_location_from_lines(lines: list[str]) -> str:
    for line in lines:
        low = line.lower()
        if low.startswith("г. ") or low.startswith("город "):
            return normalize_whitespace(line)
    return ""


def extract_company_from_text(text: str) -> str:
    text = normalize_whitespace(text)

    patterns = [
        r"Работодатель:\s*(.+?)\s+г\.\s*[А-ЯA-ZЁ]",
        r"Работодатель:\s*(.+?)\s+город\s+[А-ЯA-ZЁ]",
        r"Работодатель:\s*(.+?)\s+Заработная плата:",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return normalize_whitespace(match.group(1))

    return ""


def is_valid_job_card(title: str, snippet: str) -> bool:
    t = normalize_whitespace(title).lower()
    s = normalize_whitespace(snippet).lower()

    if not t:
        return False

    bad_titles = [
        "сейчас смотрит:",
        "вакансии на должность",
        "по релевантности",
        "найдено ",
    ]
    if any(t.startswith(x) for x in bad_titles):
        return False

    if "общество с ограниченной ответственностью" in s and "хэдхантер" in s:
        return False

    return True


async def close_popups(page):
    try:
        region_yes = page.locator("button:has-text('Да')").first
        if await region_yes.count() > 0:
            await region_yes.click(timeout=2000)
            await page.wait_for_timeout(700)
    except Exception:
        pass

    try:
        cookies_btn = page.locator("button:has-text('Согласен')").first
        if await cookies_btn.count() > 0:
            await cookies_btn.click(timeout=2000)
            await page.wait_for_timeout(700)
    except Exception:
        pass


async def perform_search(page, query: str, timeout_ms: int):
    await page.goto(
        "https://trudvsem.ru/vacancy/search",
        wait_until="domcontentloaded",
        timeout=timeout_ms,
    )
    await page.wait_for_timeout(3000)

    await close_popups(page)

    search_input = page.locator("input[placeholder*='название']").first
    await search_input.wait_for(timeout=timeout_ms)
    await search_input.click()
    await search_input.fill(query)

    search_button = page.locator("button:has-text('Найти')").first
    await search_button.click()

    await page.wait_for_timeout(5000)
    await close_popups(page)


async def apply_experience_filter_no_exp(page):
    """
    Включает фильтр 'Без опыта / До 1 года' через интерфейс сайта.
    """
    await close_popups(page)

    # 1. Пытаемся открыть фильтр "Требуемый опыт работы"
    opened = False
    filter_open_selectors = [
        "text=Требуемый опыт работы",
        "text=Опыт работы",
    ]

    for sel in filter_open_selectors:
        try:
            locator = page.locator(sel).first
            if await locator.count() > 0:
                await locator.click(timeout=3000)
                await page.wait_for_timeout(1200)
                opened = True
                break
        except Exception:
            continue

    # 2. Если фильтр не виден среди быстрых фильтров, попробуем через кнопку фильтров
    if not opened:
        filter_button_candidates = [
            "button[aria-label*='фильтр']",
            "button:has(svg)",
            "text=Фильтры",
        ]
        for sel in filter_button_candidates:
            try:
                locator = page.locator(sel).first
                if await locator.count() > 0:
                    await locator.click(timeout=3000)
                    await page.wait_for_timeout(1500)
                    break
            except Exception:
                continue

        for sel in filter_open_selectors:
            try:
                locator = page.locator(sel).first
                if await locator.count() > 0:
                    await locator.click(timeout=3000)
                    await page.wait_for_timeout(1200)
                    opened = True
                    break
            except Exception:
                continue

    # 3. Выбираем чекбокс "Без опыта/До 1 года"
    checkbox_candidates = [
        "text=Без опыта/До 1 года",
        "text=Без опыта / До 1 года",
    ]

    for sel in checkbox_candidates:
        try:
            option = page.locator(sel).first
            if await option.count() > 0:
                await option.click(timeout=3000)
                await page.wait_for_timeout(1500)
                break
        except Exception:
            continue

    await close_popups(page)
    await page.wait_for_timeout(3000)


async def get_left_cards_data(page):
    js = """
    () => {
        const all = Array.from(document.querySelectorAll('div'));
        const items = [];

        for (const el of all) {
            const text = (el.innerText || '').trim();
            if (!text) continue;
            if (!text.includes('Обновлено:')) continue;

            const lines = text
                .split('\\n')
                .map(x => x.trim())
                .filter(Boolean);

            if (lines.length < 4) continue;
            if (text.length > 220) continue;

            let href = "";

            const link = el.querySelector('a[href*="/vacancy/card/"]');
            if (link && link.href) {
                href = link.href;
            }

            items.push({
                text,
                lines,
                href
            });
        }

        return items;
    }
    """
    return await page.evaluate(js)


async def scrape_trudvsem(
    query: str,
    max_pages: int = 1,
    timeout_ms: int = 25000,
    no_experience_only: bool = False,
) -> list[dict]:
    results = []
    seen = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="ru-RU",
            viewport={"width": 1440, "height": 1200},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        try:
            await perform_search(page, query, timeout_ms)

            if no_experience_only:
                await apply_experience_filter_no_exp(page)

            await close_popups(page)
            await page.wait_for_timeout(2500)

            for _ in range(6):
                await page.mouse.wheel(0, 900)
                await page.wait_for_timeout(700)

            cards = await get_left_cards_data(page)

            for card in cards:
                lines = card["lines"]
                title = normalize_whitespace(lines[0]) if len(lines) > 0 else ""
                snippet = normalize_whitespace(card["text"])
                company = extract_company_from_text(snippet)
                location = extract_location_from_lines(lines)
                url = normalize_whitespace(card.get("href", ""))

                if not is_valid_job_card(title, snippet):
                    continue

                key = url if url else (title, company, location)
                if key in seen:
                    continue
                seen.add(key)

                results.append({
                    "title": title,
                    "company": company,
                    "salary": "",
                    "location": location,
                    "url": url,
                    "source": "trudvsem",
                    "snippet": snippet[:300]
                })

        except PlaywrightTimeoutError:
            await browser.close()
            return []

        await browser.close()

    return results


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/jobs")
async def get_jobs(
    query: str = Query(..., description="Например: Data Analyst"),
    no_experience_only: bool = Query(
        False,
        description="Включать фильтр 'Без опыта / До 1 года'"
    ),
    pages: int = Query(1, ge=1, le=3, description="Сколько страниц скрапить"),
):
    try:
        jobs = await scrape_trudvsem(
            query=query,
            max_pages=pages,
            no_experience_only=no_experience_only,
        )

        return {
            "query": query,
            "no_experience_only": no_experience_only,
            "count": len(jobs),
            "results": jobs,
        }
    except Exception as e:
        return {
            "error": str(e),
            "query": query,
        }