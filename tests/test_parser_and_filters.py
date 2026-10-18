import sys
import os
import shutil
import tempfile
import unittest
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models import KworkProject, FilterConfig, AppConfig
from src.filters import ProjectFilter
from src.storage import ProjectStorage
from src.parser import KworkParser
from src.config import load_config


class TestKworkFilters(unittest.TestCase):
    def setUp(self):
        self.config = FilterConfig(
            categories=[11],
            keywords=["python", "бот", "парсер"],
            stop_words=["1с", "диплом"],
            min_price=1000.0,
            max_price=50000.0,
            max_offers=10,
        )
        self.filter = ProjectFilter(self.config)

    def test_matching_project(self):
        project = KworkProject(
            id=101,
            title="Написать парсер на Python",
            description="Требуется собрать данные с сайта в Excel.",
            price_limit=5000.0,
            possible_price_limit=10000.0,
            offers_count=2,
            category_id=11,
            url="https://kwork.ru/projects/101",
        )
        matched, reason = self.filter.matches(project)
        self.assertTrue(matched, f"Ожидалось совпадение, но отклонено: {reason}")

    def test_stop_word_rejection(self):
        project = KworkProject(
            id=102,
            title="Доработка выгрузки 1С на Python",
            description="Связать 1C с сайтом.",
            price_limit=5000.0,
            possible_price_limit=5000.0,
            offers_count=1,
            category_id=11,
        )
        matched, reason = self.filter.matches(project)
        self.assertFalse(matched)
        self.assertIn("стоп-слово", reason)

    def test_category_mismatch(self):
        project = KworkProject(
            id=103,
            title="Нарисовать баннер для бота",
            description="Красивый баннер",
            price_limit=3000.0,
            possible_price_limit=3000.0,
            offers_count=1,
            category_id=15,  # Дизайн, а в фильтре 11
        )
        matched, reason = self.filter.matches(project)
        self.assertFalse(matched)
        self.assertIn("Категория", reason)

    def test_budget_rejection(self):
        project = KworkProject(
            id=104,
            title="Скрипт на Python",
            description="Простой скрипт",
            price_limit=500.0,
            possible_price_limit=500.0,
            offers_count=1,
            category_id=11,
        )
        matched, reason = self.filter.matches(project)
        self.assertFalse(matched)
        self.assertIn("ниже минимума", reason)

    def test_offers_count_rejection(self):
        project = KworkProject(
            id=105,
            title="Скрипт на Python",
            description="Простой скрипт",
            price_limit=3000.0,
            possible_price_limit=3000.0,
            offers_count=15,  # Лимит 10
            category_id=11,
        )
        matched, reason = self.filter.matches(project)
        self.assertFalse(matched)
        self.assertIn("превышает максимум", reason)


class TestProjectStorage(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.storage = ProjectStorage(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_storage_deduplication(self):
        project = KworkProject(
            id=201,
            title="Тестовый проект",
            description="Описание",
            price_limit=2000.0,
            possible_price_limit=2000.0,
            category_id=11,
        )
        self.assertFalse(self.storage.is_seen(201))
        self.storage.mark_seen(project, notified=True)
        self.assertTrue(self.storage.is_seen(201))

        stats = self.storage.get_stats()
        self.assertEqual(stats["total_seen"], 1)
        self.assertEqual(stats["total_notified"], 1)


class TestLiveParser(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_real_projects(self):
        parser = KworkParser()
        projects = await parser.fetch_projects(category_id=11)
        self.assertIsInstance(projects, list)
        self.assertGreater(len(projects), 0, "Парсер должен вернуть хотя бы 1 проект из категории 11")

        first = projects[0]
        self.assertGreater(first.id, 0)
        self.assertTrue(bool(first.title))
        self.assertIsNotNone(first.display_price)
        self.assertTrue(first.url.startswith("https://kwork.ru/projects/"))
        print(f"\n[УСПЕХ] Спарсен реальный проект с Kwork: [{first.id}] {first.title} ({first.display_price})")


if __name__ == "__main__":
    unittest.main()
