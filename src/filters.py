import re
from typing import Tuple
from src.models import KworkProject, FilterConfig


CATEGORY_HIERARCHY = {
    11: {11, 38, 37, 41, 79, 80, 39, 40, 255, 81},  # Разработка и IT
    15: {15, 25, 24, 28, 27, 90, 250, 306, 270, 68, 272, 286},  # Дизайн
    5: {5, 235, 75, 74, 73, 35, 303},  # Тексты и переводы
    17: {17, 72, 71, 59, 56, 44, 43, 273},  # SEO и трафик
    45: {45, 113, 112, 108, 49, 47, 46},  # Соцсети и маркетинг
    7: {7, 106, 78, 77, 76, 23, 20, 300},  # Аудио, видео, съемка
    83: {83, 114, 84, 64, 63, 55, 262, 265, 65},  # Бизнес и жизнь
}


class ProjectFilter:
    """
    Evaluates projects against configurable filtering rules:
    categories, keywords, stop-words, budget, and proposal count.
    """

    def __init__(self, config: FilterConfig):
        self.config = config
        
        # Build complete set of allowed categories including subcategories
        self.allowed_categories = set()
        for cat in config.categories:
            self.allowed_categories.add(cat)
            if cat in CATEGORY_HIERARCHY:
                self.allowed_categories.update(CATEGORY_HIERARCHY[cat])

        self._compiled_keywords = [
            re.compile(rf"\b{re.escape(k.lower())}\b", re.IGNORECASE) if len(k) <= 4 
            else re.compile(re.escape(k.lower()), re.IGNORECASE)
            for k in config.keywords if k.strip()
        ]
        self._compiled_stop_words = [
            re.compile(rf"\b{re.escape(w.lower())}\b", re.IGNORECASE) if len(w) <= 4
            else re.compile(re.escape(w.lower()), re.IGNORECASE)
            for w in config.stop_words if w.strip()
        ]

    def matches(self, project: KworkProject) -> Tuple[bool, str]:
        """
        Returns (True, 'matched') if project passes all filters,
        or (False, reason) if rejected.
        """
        # 1. Category check (applicable to Kwork projects with category_id)
        if self.allowed_categories and project.source == "kwork" and project.category_id != 0:
            if project.category_id not in self.allowed_categories:
                return False, f"Категория {project.category_id} не входит в список разрешённых"

        searchable_text = f"{project.title} {project.description}".lower()

        # 2. Stop words check
        for pattern in self._compiled_stop_words:
            if pattern.search(searchable_text):
                return False, f"Найдено стоп-слово: '{pattern.pattern}'"

        # 3. Keywords check (if list is non-empty)
        if self._compiled_keywords:
            matched_kw = any(pattern.search(searchable_text) for pattern in self._compiled_keywords)
            if not matched_kw:
                return False, "Не найдено ни одного ключевого слова"

        # 4. Budget checks
        effective_budget = max(project.price_limit, project.possible_price_limit)
        if self.config.min_price > 0 and effective_budget < self.config.min_price:
            return False, f"Бюджет {effective_budget} ₽ ниже минимума {self.config.min_price} ₽"

        if self.config.max_price is not None and project.price_limit > self.config.max_price:
            return False, f"Бюджет {project.price_limit} ₽ выше максимума {self.config.max_price} ₽"

        # 5. Offers count check
        if self.config.max_offers is not None and project.offers_count > self.config.max_offers:
            return False, f"Количество откликов ({project.offers_count}) превышает максимум ({self.config.max_offers})"

        return True, "Подходит"
