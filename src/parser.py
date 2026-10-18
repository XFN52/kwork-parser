import io
import json
import re
import html
import logging
import zipfile
import xml.etree.ElementTree as ET
from typing import List, Optional, Dict, Any
import httpx

from src.models import KworkProject, KworkAttachment, KworkClientProfile

logger = logging.getLogger(__name__)

KWORK_CATEGORIES: Dict[int, str] = {
    11: "Разработка и IT",
    38: "Доработка и настройка сайта",
    37: "Создание сайта",
    41: "Скрипты, боты и mini apps",
    79: "Верстка",
    80: "Десктоп программирование",
    39: "Мобильные приложения",
    40: "Игры",
    255: "Сервера и хостинг",
    81: "Юзабилити, тесты и помощь",
    15: "Дизайн",
    5: "Тексты и переводы",
    17: "SEO и трафик",
    45: "Соцсети и маркетинг",
    7: "Аудио, видео, съемка",
    83: "Бизнес и жизнь",
}

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


class KworkParser:
    """
    Asynchronous scraper for Kwork exchange projects (https://kwork.ru/projects).
    Extracts structured order data directly from server-side rendered state.
    """

    BASE_URL = "https://kwork.ru/projects"

    def __init__(self, proxy: Optional[str] = None, timeout: float = 15.0):
        self.proxy = proxy
        self.timeout = timeout

    def _create_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            proxy=self.proxy,
            timeout=httpx.Timeout(self.timeout),
            follow_redirects=True,
        )

    def _clean_text(self, text: Optional[str]) -> str:
        if not text:
            return ""
        # Unescape HTML entities
        cleaned = html.unescape(text)
        # Replace <br> tags with newlines and strip HTML tags
        cleaned = re.sub(r"(?i)<br\s*/?>", "\n", cleaned)
        cleaned = re.sub(r"<[^>]+>", "", cleaned)
        # Normalize whitespace
        cleaned = re.sub(r"\r\n|\r", "\n", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def _extract_state_data(self, html_content: str) -> Optional[Dict[str, Any]]:
        """Extracts window.stateData JSON object from page HTML."""
        match = re.search(r"window\.stateData\s*=\s*(\{.*?\});\s*(?:window|\n|<|var)", html_content, re.DOTALL)
        if not match:
            # Alternative match until semicolon
            match = re.search(r"window\.stateData\s*=\s*(\{.+?\});", html_content)
        
        if not match:
            logger.warning("Не удалось найти window.stateData в теле ответа Kwork.")
            return None

        json_str = match.group(1)
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.error(f"Ошибка декодирования JSON stateData: {e}")
            return None

    def _parse_project_item(self, item: Dict[str, Any]) -> Optional[KworkProject]:
        """Converts raw project dict from stateData into KworkProject model."""
        try:
            proj_id = int(item.get("id"))
            title = self._clean_text(item.get("name", ""))
            description = self._clean_text(item.get("description", ""))
            price_limit = float(item.get("priceLimit") or 0.0)
            possible_price_limit = float(item.get("possiblePriceLimit") or price_limit)
            offers_count = int(item.get("kwork_count") or 0)
            category_id = int(item.get("category_id") or 0)
            time_left = str(item.get("timeLeft") or "")
            date_create = str(item.get("date_create") or "")

            user_data = item.get("user") or {}
            client_name = user_data.get("username", "")
            client_url = item.get("wantUserGetProfileUrl", "")
            if not client_url and client_name:
                client_url = f"https://kwork.ru/user/{client_name}"

            hired_percent = None
            if "data" in user_data and isinstance(user_data["data"], dict):
                hired_percent = user_data["data"].get("wants_hired_percent")

            category_name = KWORK_CATEGORIES.get(category_id)

            # Parse attached files
            attachments: List[KworkAttachment] = []
            for f in item.get("files", []):
                if isinstance(f, dict):
                    f_id = int(f.get("FID") or 0)
                    f_name = self._clean_text(f.get("fname", ""))
                    f_url = f.get("url", "")
                    f_size = int(f.get("size") or 0)
                    f_mime = str(f.get("mime_type_id") or "")
                    if f_name or f_url:
                        attachments.append(KworkAttachment(
                            id=f_id,
                            name=f_name,
                            url=f_url,
                            size=f_size,
                            mime_type=f_mime,
                        ))

            return KworkProject(
                id=proj_id,
                title=title,
                description=description,
                price_limit=price_limit,
                possible_price_limit=possible_price_limit,
                offers_count=offers_count,
                category_id=category_id,
                category_name=category_name,
                time_left=time_left,
                date_create=date_create,
                client_name=client_name,
                client_url=client_url,
                client_hired_percent=hired_percent,
                url=f"https://kwork.ru/projects/{proj_id}",
                files=attachments,
            )
        except Exception as e:
            logger.error(f"Ошибка разбора элемента проекта {item.get('id')}: {e}")
            return None

    async def fetch_projects(
        self,
        category_id: Optional[int] = None,
        keyword: Optional[str] = None,
        page: int = 1,
        view: Optional[int] = 0,
    ) -> List[KworkProject]:
        """
        Fetches projects from Kwork for given filters.
        """
        params: Dict[str, Any] = {}
        if view is not None:
            params["view"] = view
        if category_id:
            params["c"] = category_id
        if keyword:
            params["keyword"] = keyword
        if page > 1:
            params["page"] = page

        try:
            async with self._create_client() as client:
                response = await client.get(self.BASE_URL, params=params)
                if response.status_code != 200:
                    logger.warning(f"Kwork ответил кодом {response.status_code} для URL {response.url}")
                    return []

                state_data = self._extract_state_data(response.text)
                if not state_data:
                    return []

                raw_wants = state_data.get("wants", [])
                projects: List[KworkProject] = []
                for item in raw_wants:
                    proj = self._parse_project_item(item)
                    if proj:
                        projects.append(proj)

                logger.debug(f"Получено {len(projects)} проектов с Kwork (cat={category_id}, page={page})")
                return projects

        except httpx.RequestError as e:
            logger.error(f"Сетевая ошибка при запросе к Kwork: {e}")
            return []
        except Exception as e:
            logger.error(f"Непредвиденная ошибка парсера: {e}", exc_info=True)
            return []

    async def fetch_project_by_id(self, project_id: int) -> Optional[KworkProject]:
        """
        Fetches full details for a single project by its ID from https://kwork.ru/projects/{id}.
        """
        url = f"https://kwork.ru/projects/{project_id}"
        try:
            async with self._create_client() as client:
                response = await client.get(url)
                if response.status_code != 200:
                    logger.warning(f"Kwork вернул {response.status_code} при запросе проекта #{project_id}")
                    return None

                state_data = self._extract_state_data(response.text)
                if not state_data:
                    return None

                want_data = state_data.get("wantData")
                if not want_data or not isinstance(want_data, dict):
                    return None

                return self._parse_project_item(want_data)

        except Exception as e:
            logger.error(f"Ошибка получения проекта #{project_id}: {e}")
            return None

    async def fetch_client_profile(self, username: str) -> Optional[KworkClientProfile]:
        """
        Fetches client profile details from https://kwork.ru/user/{username}.
        Extracts full name, profession, description ('О себе' / company details),
        payer level, registration date, and listed skills.
        """
        if not username or not username.strip():
            return None

        clean_user = username.strip().lstrip("@")
        url = f"https://kwork.ru/user/{clean_user}"
        try:
            async with self._create_client() as client:
                response = await client.get(url)
                if response.status_code != 200:
                    logger.debug(f"Профиль заказчика {clean_user} вернул HTTP {response.status_code}")
                    return None

                state_data = self._extract_state_data(response.text)
                if not state_data:
                    return None

                raw_full_name = state_data.get("userProfileFullName") or ""
                raw_prof = state_data.get("userProfileProfession") or ""
                raw_desc = state_data.get("userProfileDescription") or ""
                payer_label = state_data.get("payerLevelLabel") or ""
                level_desc = state_data.get("levelDescription") or ""
                add_time = state_data.get("userProfileAddTime") or ""

                skills_raw = state_data.get("userSkills", [])
                skills: List[str] = []
                if isinstance(skills_raw, list):
                    for s in skills_raw:
                        if isinstance(s, dict) and s.get("name"):
                            skills.append(self._clean_text(s["name"]))
                        elif isinstance(s, str) and s.strip():
                            skills.append(self._clean_text(s))

                return KworkClientProfile(
                    username=clean_user,
                    full_name=self._clean_text(raw_full_name),
                    profession=self._clean_text(raw_prof),
                    description=self._clean_text(raw_desc),
                    payer_level=self._clean_text(payer_label),
                    payer_description=self._clean_text(level_desc),
                    registered_at=self._clean_text(add_time),
                    skills=skills,
                )

        except Exception as e:
            logger.warning(f"Ошибка загрузки профиля заказчика '{clean_user}': {e}")
            return None

    async def download_and_extract_file(self, file_url: str, fname: str, max_chars: int = 5000) -> str:
        """
        Downloads an attached file from Kwork and extracts textual content.
        Uses cdn-edge.kwork.ru host for direct binary access.
        """
        if not file_url:
            return ""

        # Switch kwork.ru to cdn-edge.kwork.ru for direct file downloading
        direct_url = file_url.replace("https://kwork.ru/files/", "https://cdn-edge.kwork.ru/files/")
        if not direct_url.startswith("http"):
            direct_url = f"https://cdn-edge.kwork.ru{direct_url if direct_url.startswith('/') else '/' + direct_url}"

        fname_lower = fname.lower()

        try:
            async with self._create_client() as client:
                resp = await client.get(direct_url)
                if resp.status_code != 200:
                    logger.warning(f"Не удалось скачать файл {fname} ({resp.status_code}): {direct_url}")
                    return f"[Файл: {fname} (ошибка загрузки HTTP {resp.status_code})]"

                content = resp.content
                if not content:
                    return f"[Файл: {fname} (пустой)]"

                # 1. DOCX format (built-in zipfile + XML extraction)
                if fname_lower.endswith(".docx"):
                    try:
                        with zipfile.ZipFile(io.BytesIO(content)) as zf:
                            xml_bytes = zf.read("word/document.xml")
                            tree = ET.fromstring(xml_bytes)
                            texts = [elem.text for elem in tree.iter() if elem.text]
                            doc_text = " ".join(texts).strip()
                            if len(doc_text) > max_chars:
                                doc_text = doc_text[:max_chars] + "... [текст файла сокращен]"
                            return f"--- Текст из документа '{fname}' ---\n{doc_text}\n--- Конец документа ---"
                    except Exception as e:
                        logger.warning(f"Ошибка разбора docx {fname}: {e}")
                        return f"[Документ Word: {fname}, размер: {len(content)} байт]"

                # 2. PDF format
                if fname_lower.endswith(".pdf"):
                    try:
                        import pypdf
                        reader = pypdf.PdfReader(io.BytesIO(content))
                        pages_text = []
                        for idx, page in enumerate(reader.pages[:10]):  # First 10 pages
                            txt = page.extract_text()
                            if txt:
                                pages_text.append(txt.strip())
                        pdf_text = "\n\n".join(pages_text).strip()
                        if len(pdf_text) > max_chars:
                            pdf_text = pdf_text[:max_chars] + "... [текст файла сокращен]"
                        return f"--- Текст из PDF '{fname}' ---\n{pdf_text}\n--- Конец PDF ---"
                    except Exception as e:
                        logger.warning(f"Ошибка разбора PDF {fname}: {e}")
                        return f"[Документ PDF: {fname}, размер: {len(content)} байт]"

                # 3. Plain text formats (.txt, .md, .csv, .json, .log, .py, .html, etc.)
                text_exts = (".txt", ".md", ".json", ".csv", ".log", ".py", ".sql", ".html", ".xml", ".yml", ".yaml", ".ini", ".cfg")
                if any(fname_lower.endswith(ext) for ext in text_exts):
                    text = ""
                    for enc in ("utf-8", "cp1251", "latin-1"):
                        try:
                            text = content.decode(enc)
                            break
                        except UnicodeDecodeError:
                            continue
                    if text:
                        text = text.strip()
                        if len(text) > max_chars:
                            text = text[:max_chars] + "... [текст файла сокращен]"
                        return f"--- Содержимое файла '{fname}' ---\n{text}\n--- Конец файла ---"

                # 4. Images (.png, .jpg, .jpeg, .webp, .gif)
                img_exts = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
                if any(fname_lower.endswith(ext) for ext in img_exts):
                    return f"[Изображение / Скриншот: '{fname}', размер: {len(content):,} байт]"

                # 5. Archives (.zip, .rar, etc.)
                if fname_lower.endswith(".zip"):
                    try:
                        with zipfile.ZipFile(io.BytesIO(content)) as zf:
                            names = zf.namelist()[:15]
                            return f"[Архив ZIP: '{fname}', файлов: {len(zf.namelist())}, список: {', '.join(names)}]"
                    except Exception:
                        return f"[Архив ZIP: '{fname}', размер: {len(content):,} байт]"

                return f"[Приложенный файл: '{fname}', размер: {len(content):,} байт]"

        except Exception as e:
            logger.error(f"Сбой при обработке вложения {fname}: {e}")
            return f"[Вложение: {fname} (ошибка обработки)]"

    async def read_project_attachments(self, project: KworkProject, max_total_chars: int = 10000) -> str:
        """
        Downloads and reads all attachments for a project, returning consolidated textual summary.
        """
        if not project.files:
            return ""

        extracted_blocks: List[str] = []
        total_len = 0

        for att in project.files:
            if total_len >= max_total_chars:
                extracted_blocks.append(f"... [Остальные {len(project.files) - len(extracted_blocks)} файлов опущены для экономии контекста]")
                break

            limit_per_file = max(2000, (max_total_chars - total_len))
            res = await self.download_and_extract_file(att.url, att.name, max_chars=limit_per_file)
            if res:
                extracted_blocks.append(res)
                total_len += len(res)

        return "\n\n".join(extracted_blocks)

