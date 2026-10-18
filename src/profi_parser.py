import asyncio
import html
import json
import logging
import re
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from urllib.parse import urljoin
import httpx

from src.models import KworkProject

logger = logging.getLogger(__name__)

BO_SEARCH_GRAPHQL_QUERY = """#prfrtkn:webbo:edf00fb17884d25c7864bf0f695f5dd62bd3b6cf:33ac89346fb21e2a24afd7867841b9726d969e7a

query BoSearchBoardItems($filter: BoSearchFrontFiltersInput!, $useSavedFilter: Boolean, $allVerticals: Boolean, $searchQuery: String, $searchEntities: [BoSearchEntityInput!], $searchId: ID, $nextCursor: String, $pageSize: Int, $boSortUp: Int, $minScore: Float, $coordinates: BoSearchAreaInput, $clusterId: ID, $sort: BoSearchSortEnum) @domain(domains: [BO_BOARD, BO_BOARD_LIST]) {
  boSearchBoardItems(
    filter: $filter
    useSavedFilter: $useSavedFilter
    allVerticals: $allVerticals
    searchQuery: $searchQuery
    searchEntities: $searchEntities
    searchId: $searchId
    nextCursor: $nextCursor
    pageSize: $pageSize
    boSortUp: $boSortUp
    minScore: $minScore
    coordinates: $coordinates
    clusterId: $clusterId
    sort: $sort
  ) {
    nextCursor
    serverTs
    totalCount
    items {
      id
      type
      ... on BoSearchSnippet {
        ...snippetFieldsCommon
        clientInfo {
          name
        }
        schedule
      }
    }
  }
}
fragment snippetFieldsCommon on BoSearchSnippet {
  score
  title
  description
  isReposted
  lastUpdateDate
  isFresh
  isViewed
  geo {
    clientMayCome {
      address
      prefix
      suffix
    }
    orderLocation {
      address
      prefix
      suffix
    }
    remote {
      address
      prefix
      suffix
    }
  }
  price {
    prefix
    suffix
    value
  }
}"""


BO_ORDER_SCREEN_GRAPHQL_QUERY = """#prfrtkn:webbo:cf4e06a5a4f7a8e8e69b6ed71043ebf702bcc500:ca4c9d57bdd2c1258853735c18dbaa18419696d1

      query BoOrderScreen($orderId: ID!, $prepId: ID!) @domain(domains: BO_ORDER) {
  orders(id: [$orderId]) {
    _id
    boOrderScreen(prepId: $prepId) {
      id
      price {
        value
        baseValue
        currencySymbol
      }
      params {
        label
        text
      }
    }
  }
}"""



DEFAULT_PROFI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Upgrade-Insecure-Requests": "1",
}


class ProfiAuthError(Exception):
    """Raised when authentication to profi.ru fails or session cookie has expired."""
    pass


def parse_cookie_string(cookie_str: str) -> Dict[str, str]:
    """Parses standard cookie header string into a key-value dictionary."""
    cookies: Dict[str, str] = {}
    if not cookie_str:
        return cookies
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def extract_price_range(text: Optional[str]) -> Tuple[float, float]:
    """Extracts min and max prices from Russian price strings."""
    if not text:
        return 0.0, 0.0
    # Normalize spaces and non-breaking spaces
    cleaned = text.replace("\xa0", " ").strip()
    # Find all contiguous digit blocks
    digits_matches = re.findall(r"\d[\d\s]*\d|\d+", cleaned)
    numbers = []
    for m in digits_matches:
        val_str = m.replace(" ", "")
        if val_str.isdigit():
            numbers.append(float(val_str))

    if not numbers:
        return 0.0, 0.0
    if len(numbers) >= 2:
        return min(numbers[0], numbers[1]), max(numbers[0], numbers[1])
    return numbers[0], numbers[0]


class ProfiParser:
    """
    Parser for Profi.ru specialist backoffice feed (https://profi.ru/backoffice/n.php).
    Extracts orders from SSR state (window.INITIAL) and DOM card structures.
    """

    BASE_URL = "https://profi.ru/backoffice/n.php"

    def __init__(
        self,
        cookies: str = "",
        url: Optional[str] = None,
        proxy: Optional[str] = None,
        timeout: float = 20.0,
        browser_profile_dir: Optional[str] = "data/profi_browser_profile",
    ):
        self.cookies_str = cookies.strip()
        self.base_url = url or self.BASE_URL
        self.proxy = proxy
        self.timeout = timeout
        self.browser_profile_dir = Path(browser_profile_dir) if browser_profile_dir else None

    def _create_client(self) -> httpx.AsyncClient:
        headers = dict(DEFAULT_PROFI_HEADERS)
        if self.cookies_str:
            headers["Cookie"] = self.cookies_str

        return httpx.AsyncClient(
            headers=headers,
            proxy=self.proxy,
            timeout=httpx.Timeout(self.timeout),
            follow_redirects=True,
        )

    def _clean_text(self, text: Optional[str]) -> str:
        if not text:
            return ""
        cleaned = html.unescape(text)
        cleaned = re.sub(r"(?i)<br\s*/?>", "\n", cleaned)
        cleaned = re.sub(r"<[^>]+>", "", cleaned)
        cleaned = re.sub(r"\r\n|\r", "\n", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def check_authorization(self, html_content: str, status_code: int = 200) -> None:
        """
        Validates whether the user is authorized in profi.ru backoffice.
        Raises ProfiAuthError if session is expired or redirect to login occurred.
        """
        if status_code in (401, 403):
            raise ProfiAuthError(f"Profi.ru вернул статус {status_code} (не авторизован).")

        # Check for login page title or login header in INITIAL
        if "<title>Вход на Профи.ру</title>" in html_content:
            raise ProfiAuthError("Сессия Profi.ru недействительна: страница перенаправлена на форму входа.")

        # Check window.INITIAL if present
        if re.search(r"loggedAs\s*:\s*null", html_content) and re.search(r"loginHeader|canLogin\s*:\s*false", html_content):
            raise ProfiAuthError("Сессия Profi.ru недействительна: обнаружен признак неавторизованного пользователя в window.INITIAL.")

        match = re.search(r"window\.INITIAL\s*=\s*(\{.+?\});\s*(?:window|\n|<|var)", html_content, re.DOTALL)
        if match:
            try:
                # May contain JS objects; clean trailing commas if any
                clean_json = re.sub(r",\s*\}", "}", match.group(1))
                data = json.loads(clean_json)
                if data.get("loggedAs") is None and data.get("canLogin") is False and "Вход" in str(data.get("loginHeader", "")):
                    raise ProfiAuthError("Сессия Profi.ru недействительна: loggedAs=null в window.INITIAL.")
            except Exception:
                pass

    def _extract_initial_state(self, html_content: str) -> Optional[Dict[str, Any]]:
        """Attempts to extract window.INITIAL state JSON."""
        match = re.search(r"window\.INITIAL\s*=\s*(\{.+?\});\s*(?:window|\n|<|var)", html_content, re.DOTALL)
        if not match:
            match = re.search(r"window\.INITIAL\s*=\s*(\{.+?\});", html_content)
        if not match:
            return None
        try:
            clean_json = re.sub(r",\s*\}", "}", match.group(1))
            return json.loads(clean_json)
        except Exception:
            return None

    def _parse_order_from_json(self, item: Dict[str, Any]) -> Optional[KworkProject]:
        """Converts an order dictionary from JSON state into a unified KworkProject model."""
        raw_id = item.get("id") or item.get("orderId") or item.get("order_id")
        if not raw_id:
            return None

        order_id = f"profi_{raw_id}"
        title = self._clean_text(item.get("title") or item.get("subject") or "Заказ на Profi.ru")
        desc = self._clean_text(item.get("description") or item.get("details") or item.get("snippet") or "")
        
        # Build extra context (location, schedule)
        extra_lines = []
        loc = item.get("location") or item.get("address")
        if loc:
            extra_lines.append(f"📍 Место: {self._clean_text(str(loc))}")
        schedule = item.get("schedule") or item.get("time")
        if schedule:
            extra_lines.append(f"📅 Сроки: {self._clean_text(str(schedule))}")
        
        if extra_lines:
            desc = desc + ("\n\n" if desc else "") + "\n".join(extra_lines)

        raw_price = item.get("price") or item.get("priceText") or item.get("budget") or ""
        min_p, max_p = extract_price_range(str(raw_price))

        raw_resp_price = item.get("responsePrice") or item.get("response_price") or item.get("bidPrice")
        resp_price_str = str(raw_resp_price).strip() if raw_resp_price else None

        client_name = self._clean_text(item.get("clientName") or item.get("client_name") or item.get("name") or "")
        order_url = item.get("url") or f"https://profi.ru/backoffice/n.php/order/{raw_id}"
        if order_url.startswith("/"):
            order_url = urljoin(self.base_url, order_url)

        cat_name = self._clean_text(
            item.get("category")
            or item.get("categoryName")
            or item.get("category_name")
            or item.get("subject")
            or title
            or "Разработка и IT"
        )

        return KworkProject(
            id=order_id,
            title=title,
            description=desc,
            price_limit=min_p,
            possible_price_limit=max_p,
            offers_count=int(item.get("responsesCount") or item.get("offers_count") or 0),
            category_id=0,
            category_name=cat_name,
            client_name=client_name,
            client_url="",
            url=order_url,
            source="profi",
            response_price=resp_price_str,
        )

    def parse_html(self, html_content: str) -> List[KworkProject]:
        """
        Parses orders from HTML content.
        Supports both structured state (window.INITIAL) and DOM card patterns.
        """
        projects: List[KworkProject] = []
        seen_ids = set()

        # 1. Try extracting from JSON state first
        state = self._extract_initial_state(html_content)
        if state:
            orders_list = (
                state.get("orders")
                or state.get("orderList")
                or state.get("feed")
                or state.get("applications")
            )
            if isinstance(orders_list, list):
                for item in orders_list:
                    if isinstance(item, dict):
                        p = self._parse_order_from_json(item)
                        if p and p.id not in seen_ids:
                            seen_ids.add(p.id)
                            projects.append(p)

        # 2. Extract DOM order cards (CSS/Regex)
        # Match order cards or snippets (OrderSnippetContainerStyles__Container or snippet articles)
        card_blocks = re.findall(
            r"(<(?:div|article|section)[^>]*class=[\"'][^\"']*(?:OrderSnippet|SnippetBody|order-card)[^\"']*[\"'].*?)(?=(?:<(?:div|article|section)[^>]*class=[\"'][^\"']*(?:OrderSnippet|SnippetBody|order-card)|\Z|</(?:body|html)>))",
            html_content,
            re.DOTALL | re.IGNORECASE,
        )

        for card in card_blocks:
            # Extract link & order ID
            link_match = re.search(r'href=[\"\']([^\"\']*(?:/order[/\.\?]|/n\.php/)[^\"\']*)[\"\']', card)
            id_match = re.search(r'(?:id=[\"\'](?:order[_-]?)?(\d+)[\"\']|/order(?:/|\.php\?o=)(\d+))', card)
            
            raw_id = None
            if id_match:
                raw_id = id_match.group(1) or id_match.group(2)
            elif link_match:
                digit_search = re.search(r"\d+", link_match.group(1))
                if digit_search:
                    raw_id = digit_search.group(0)

            if not raw_id:
                continue

            order_id = f"profi_{raw_id}"
            if order_id in seen_ids:
                continue

            # Title
            title_match = re.search(r"<h[2-4][^>]*>(.*?)</h[2-4]>", card, re.DOTALL)
            title = self._clean_text(title_match.group(1)) if title_match else "Заказ на Profi.ru"

            # Price
            price_match = re.search(r'class=[\'"][^\'"]*(?:PriceValue|price-value|order-price|budget)[^\'"]*[\'"][^>]*>([^<]+)', card, re.IGNORECASE)
            if not price_match:
                price_match = re.search(r'([0-9][0-9\s]*(?:₽|руб\.?))', card, re.IGNORECASE)
            raw_price = price_match.group(1) if price_match else ""
            min_p, max_p = extract_price_range(raw_price)

            # Description / snippet
            desc_match = re.search(r'<p[^>]*class=[\'"][^\'"]*(?:MainInfo|description|detail)[^\'"]*[\'"][^>]*>(.*?)</p>', card, re.DOTALL | re.IGNORECASE)
            if not desc_match:
                desc_match = re.search(r'<p[^>]*>(.*?)</p>', card, re.DOTALL | re.IGNORECASE)
            desc = self._clean_text(desc_match.group(1)) if desc_match else ""

            # Location and Schedule
            loc_match = re.search(r'class=[\'"][^\'"]*(?:TextContainer|LocationText|location-text|address)[^\'"]*[\'"][^>]*>([^<]+)', card, re.IGNORECASE)
            loc = self._clean_text(loc_match.group(1)) if loc_match else ""

            sched_match = re.search(r'class=[\'"][^\'"]*(?:ScheduleText|DateText|schedule-text)[^\'"]*[\'"][^>]*>([^<]+)', card, re.IGNORECASE)
            sched = self._clean_text(sched_match.group(1)) if sched_match else ""

            extra = []
            if loc:
                extra.append(f"📍 Место: {loc}")
            if sched:
                extra.append(f"📅 Сроки: {sched}")
            if extra:
                desc = desc + ("\n\n" if desc else "") + "\n".join(extra)

            # Client name
            client_match = re.search(r'class=[\'"][^\'"]*(?:ClientInfo|ClientName|customer|StatusAndClientInfoStyles__Name)[^\'"]*[\'"][^>]*>([^<]+)', card, re.IGNORECASE)
            client_name = self._clean_text(client_match.group(1)) if client_match else ""

            order_url = link_match.group(1) if link_match else f"/backoffice/n.php/order/{raw_id}"
            if order_url.startswith("/"):
                order_url = urljoin(self.base_url, order_url)

            cat_match = re.search(r'class=[\'"][^\'"]*(?:CategoryName|category-name|order-category|subject)[^\'"]*[\'"][^>]*>([^<]+)', card, re.IGNORECASE)
            cat_name = self._clean_text(cat_match.group(1)) if cat_match else (title if title and title != "Заказ на Profi.ru" else "Разработка и IT")

            project = KworkProject(
                id=order_id,
                title=title,
                description=desc,
                price_limit=min_p,
                possible_price_limit=max_p,
                offers_count=0,
                category_id=0,
                category_name=cat_name,
                client_name=client_name,
                client_url="",
                url=order_url,
                source="profi",
            )
            seen_ids.add(order_id)
            projects.append(project)

        return projects

    def _parse_order_from_graphql_snippet(self, item: Dict[str, Any]) -> Optional[KworkProject]:
        """Converts a GraphQL order snippet into a unified KworkProject model."""
        raw_id = item.get("id")
        if not raw_id or not str(raw_id).isdigit():
            return None

        order_id = f"profi_{raw_id}"
        title = self._clean_text(item.get("title") or "Заказ на Profi.ru")
        desc = self._clean_text(item.get("description") or "")

        # Extract location / geo
        geo = item.get("geo") or {}
        remote = (geo.get("remote") or {}).get("prefix")
        order_loc = (geo.get("orderLocation") or {}).get("address") or (geo.get("orderLocation") or {}).get("prefix")
        client_loc = (geo.get("clientMayCome") or {}).get("address") or (geo.get("clientMayCome") or {}).get("prefix")

        location_parts = []
        for part in [remote, order_loc, client_loc]:
            if part and str(part).strip() and str(part).strip() not in location_parts:
                location_parts.append(str(part).strip())

        extra_lines = []
        if location_parts:
            extra_lines.append(f"📍 Место: {', '.join(location_parts)}")

        schedule = item.get("schedule")
        if schedule:
            extra_lines.append(f"📅 Сроки: {self._clean_text(str(schedule))}")

        if extra_lines:
            desc = desc + ("\n\n" if desc else "") + "\n".join(extra_lines)

        # Price
        price_obj = item.get("price") or {}
        prefix = price_obj.get("prefix") or ""
        val = price_obj.get("value") or ""
        full_price_text = f"{prefix} {val}".strip()
        min_p, max_p = extract_price_range(full_price_text)

        # Client info
        client_info = item.get("clientInfo") or {}
        client_name = self._clean_text(client_info.get("name") or "")

        # Canonical backoffice order URL
        order_url = f"https://profi.ru/backoffice/n.php?o={raw_id}"

        cat_name = self._clean_text(
            item.get("category")
            or item.get("category_name")
            or item.get("subject")
            or title
            or "Разработка и IT"
        )

        return KworkProject(
            id=order_id,
            title=title,
            description=desc,
            price_limit=min_p,
            possible_price_limit=max_p,
            offers_count=0,
            category_id=0,
            category_name=cat_name,
            client_name=client_name,
            client_url="",
            url=order_url,
            source="profi",
            response_price=item.get("response_price"),
            is_fresh=item.get("isFresh"),
            is_viewed=item.get("isViewed"),
        )

    def parse_graphql_data(self, data: Dict[str, Any], only_fresh: bool = False) -> List[KworkProject]:
        """
        Parses orders from GraphQL BoSearchBoardItems response.
        Extracts genuine SNIPPET items from the specialist's board.
        Cross-sell CAROUSEL items ('Могут вам подойти') are excluded.
        If only_fresh=True, returns only orders marked as isFresh or appearing before the DIVIDER.
        """
        projects: List[KworkProject] = []
        seen_ids = set()

        items = data.get("data", {}).get("boSearchBoardItems", {}).get("items", [])
        for item in items:
            itype = item.get("type")
            if itype == "DIVIDER" and only_fresh:
                # In Profi feed, DIVIDER ("Вы посмотрели все новые заказы") marks the boundary
                # between new orders and previously viewed backlog.
                break
            if itype == "SNIPPET":
                if only_fresh and item.get("isFresh") is False:
                    continue
                p = self._parse_order_from_graphql_snippet(item)
                if p and p.id not in seen_ids:
                    seen_ids.add(p.id)
                    projects.append(p)

        return projects

    async def _fetch_via_browser(self, only_fresh: bool = False) -> List[KworkProject]:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(self.browser_profile_dir),
                channel="chrome",
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(self.base_url, wait_until="domcontentloaded", timeout=self.timeout * 1000)

                if "login" in page.url.lower():
                    raise ProfiAuthError(f"Сессия Profi.ru недействительна: перенаправление на страницу входа ({page.url}).")

                # Wait briefly (1.5s) for Profi background scripts and auth tokens to settle
                await asyncio.sleep(1.5)

                evaluate_script = """async (query) => {
                    try {
                        const resp = await fetch('https://profi.ru/graphql', {
                            method: 'POST',
                            headers: {
                                'content-type': 'application/json',
                                'accept': 'application/json',
                                'x-app-id': 'BO',
                                'x-warp-ui-app': 'WEBBO',
                                'x-warp-ui-type': 'WEB',
                                'x-warp-ui-ver': '1.0',
                                'x-new-auth-compatible': '1',
                            },
                            body: JSON.stringify({
                                query: query,
                                variables: {
                                    allVerticals: false,
                                    searchQuery: "",
                                    searchEntities: [],
                                    pageSize: 30,
                                    useSavedFilter: true,
                                    sort: "DEFAULT",
                                    filter: {}
                                }
                            })
                        });
                        const text = await resp.text();
                        let data = null;
                        try {
                            data = JSON.parse(text);
                        } catch (_) {
                            data = null;
                        }
                        return {
                            status: resp.status,
                            text: text,
                            data: data
                        };
                    } catch (err) {
                        return {
                            status: 0,
                            text: String(err),
                            data: null
                        };
                    }
                }"""

                res = await page.evaluate(evaluate_script, BO_SEARCH_GRAPHQL_QUERY)
                status = res.get("status", 0)
                raw_text = res.get("text", "")

                # If first attempt returned 401/403 or "not authenticated", give SPA 2.5s to finish background token refresh and retry once
                if status in (401, 403) or "not authenticated" in raw_text.lower():
                    logger.debug(f"Profi GraphQL первый запрос неуспешен (status {status}: {raw_text[:60]}), ожидание авто-обновления токена...")
                    await asyncio.sleep(2.5)
                    res = await page.evaluate(evaluate_script, BO_SEARCH_GRAPHQL_QUERY)
                    status = res.get("status", 0)
                    raw_text = res.get("text", "")

                if status in (401, 403) or "not authenticated" in raw_text.lower():
                    raise ProfiAuthError(f"Profi.ru GraphQL вернул статус {status} (не авторизован): {raw_text[:100]}")

                data = res.get("data")
                if not data:
                    raise RuntimeError(f"Profi.ru GraphQL вернул некорректный ответ (status {status}): {raw_text[:200]}")

                if "errors" in data and not data.get("data", {}).get("boSearchBoardItems"):
                    errors = data.get("errors", [])
                    msg = errors[0].get("message") if errors else "Неизвестная ошибка GraphQL"
                    if any(term in str(msg).lower() for term in ["auth", "forbidden", "unauthorized", "login"]):
                        raise ProfiAuthError(f"Profi.ru GraphQL ошибка авторизации: {msg}")
                    raise RuntimeError(f"Profi.ru GraphQL ошибка: {msg}")

                orders = self.parse_graphql_data(data, only_fresh=only_fresh)
                order_ids = [str(o.id).replace("profi_", "") for o in orders if str(o.id).replace("profi_", "").isdigit()]

                # Fetch response prices (цены отклика) in parallel for all discovered orders
                if order_ids:
                    batch_screen_script = """async ({ query, orderIds }) => {
                        const cookies = document.cookie;
                        const m = cookies.match(/egback=([^;]+)/);
                        const prepId = m ? m[1] : '';
                        return await Promise.all(orderIds.map(async (id) => {
                            try {
                                const resp = await fetch('https://profi.ru/graphql', {
                                    method: 'POST',
                                    headers: {
                                        'content-type': 'application/json',
                                        'accept': 'application/json',
                                        'x-app-id': 'BO',
                                        'x-warp-ui-app': 'WEBBO',
                                        'x-warp-ui-type': 'WEB',
                                        'x-warp-ui-ver': '1.0',
                                        'x-new-auth-compatible': '1',
                                    },
                                    body: JSON.stringify({
                                        query: query,
                                        variables: {
                                            orderId: id,
                                            prepId: prepId
                                        }
                                    })
                                });
                                const d = await resp.json();
                                const orders = d?.data?.orders || [];
                                if (orders.length > 0) {
                                    const screen = orders[0].boOrderScreen || {};
                                    const price = screen.price;
                                    let text = '';
                                    if (price && price.value) {
                                        text = `${price.value} ${price.currencySymbol || '₽'}`.trim();
                                    } else {
                                        const param = (screen.params || []).find(p => p.label && p.label.toLowerCase().includes('отклик'));
                                        if (param && param.text) {
                                            text = param.text.trim();
                                        }
                                    }
                                    return { id: id, response_price: text };
                                }
                                return { id: id, response_price: null };
                            } catch (_) {
                                return { id: id, response_price: null };
                            }
                        }));
                    }"""
                    try:
                        price_results = await page.evaluate(
                            batch_screen_script,
                            {"query": BO_ORDER_SCREEN_GRAPHQL_QUERY, "orderIds": order_ids}
                        )
                        price_map = {r["id"]: r["response_price"] for r in price_results if r.get("response_price")}
                        for o in orders:
                            clean_id = str(o.id).replace("profi_", "")
                            if clean_id in price_map:
                                o.response_price = price_map[clean_id]
                    except Exception as e:
                        logger.warning(f"Ошибка получения цен откликов Profi.ru: {e}")
            finally:
                await context.close()

        logger.info(f"Спарсено {len(orders)} заказов с Profi.ru через GraphQL.")
        return orders

    async def _fetch_via_http(self) -> List[KworkProject]:
        if not self.cookies_str:
            logger.warning("PROFI_COOKIES не заданы. Пропуск проверки Profi.ru.")
            return []

        async with self._create_client() as client:
            try:
                response = await client.get(self.base_url)
            except httpx.RequestError as e:
                logger.error(f"Сетевая ошибка при запросе к Profi.ru ({self.base_url}): {e}")
                return []

            self.check_authorization(response.text, response.status_code)
            orders = self.parse_html(response.text)
            logger.info(f"Спарсено {len(orders)} заказов с Profi.ru через HTTP.")
            return orders

    async def fetch_orders(self, only_fresh: bool = False) -> List[KworkProject]:
        """
        Fetches orders from Profi.ru backoffice.
        Prioritizes browser session with GraphQL if browser_profile_dir exists.
        Falls back to direct HTTP request if browser profile is not available.
        """
        if self.browser_profile_dir and self.browser_profile_dir.exists():
            return await self._fetch_via_browser(only_fresh=only_fresh)

        return await self._fetch_via_http()
