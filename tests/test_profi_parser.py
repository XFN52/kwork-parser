import unittest
from unittest.mock import patch, AsyncMock
from src.profi_parser import ProfiParser, ProfiAuthError, parse_cookie_string, extract_price_range
from src.models import KworkProject, TelegramConfig
from src.telegram_bot import TelegramNotifier
from src.storage import ProjectStorage
import tempfile
import shutil
from pathlib import Path


class TestProfiParser(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_profi.db"
        self.storage = ProjectStorage(str(self.db_path))

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_parse_cookie_string(self):
        cookie_header = "p_uid=12345; session_id=abc-def-ghi; some_token=xyz"
        parsed = parse_cookie_string(cookie_header)
        self.assertEqual(parsed["p_uid"], "12345")
        self.assertEqual(parsed["session_id"], "abc-def-ghi")
        self.assertEqual(parsed["some_token"], "xyz")

    def test_extract_price_range(self):
        min_p, max_p = extract_price_range("до 15 000 ₽")
        self.assertEqual(min_p, 15000.0)
        self.assertEqual(max_p, 15000.0)

        min_p, max_p = extract_price_range("от 5 000 до 12 500 руб.")
        self.assertEqual(min_p, 5000.0)
        self.assertEqual(max_p, 12500.0)

        min_p, max_p = extract_price_range("3 000 ₽ за услугу")
        self.assertEqual(min_p, 3000.0)
        self.assertEqual(max_p, 3000.0)

        min_p, max_p = extract_price_range("По договорённости")
        self.assertEqual(min_p, 0.0)
        self.assertEqual(max_p, 0.0)

    def test_check_authorization_unauthorized(self):
        parser = ProfiParser(cookies="test=1")

        # Status 401
        with self.assertRaises(ProfiAuthError):
            parser.check_authorization("<html></html>", status_code=401)

        # Login title
        login_html = "<html><head><title>Вход на Профи.ру</title></head><body>Login</body></html>"
        with self.assertRaises(ProfiAuthError):
            parser.check_authorization(login_html, status_code=200)

        # window.INITIAL with loggedAs: null and canLogin: false
        initial_login_html = """
        <script>
        window.INITIAL = {
            loginHeader: 'Вход и регистрация для профи',
            profileId: '',
            canLogin: false,
            loggedAs: null
        };
        </script>
        """
        with self.assertRaises(ProfiAuthError):
            parser.check_authorization(initial_login_html, status_code=200)

    def test_parse_from_window_initial(self):
        html_content = """
        <html>
        <head>
        <script>
        window.INITIAL = {
            "loggedAs": {"id": 99999, "name": "Иван"},
            "orders": [
                {
                    "id": "7849102",
                    "title": "Разработка Telegram-бота на Python",
                    "description": "Нужно написать бота для записи клиентов на прием.",
                    "price": "от 10 000 до 20 000 ₽",
                    "location": "Москва (дистанционно)",
                    "schedule": "до 15 октября",
                    "clientName": "Алексей",
                    "url": "/backoffice/n.php/order/7849102"
                }
            ]
        };
        </script>
        </head>
        <body></body>
        </html>
        """
        parser = ProfiParser(cookies="p_auth=valid")
        orders = parser.parse_html(html_content)
        self.assertEqual(len(orders), 1)
        order = orders[0]
        self.assertEqual(order.id, "profi_7849102")
        self.assertEqual(order.source, "profi")
        self.assertEqual(order.title, "Разработка Telegram-бота на Python")
        self.assertIn("Нужно написать бота", order.description)
        self.assertIn("📍 Место: Москва (дистанционно)", order.description)
        self.assertIn("📅 Сроки: до 15 октября", order.description)
        self.assertEqual(order.price_limit, 10000.0)
        self.assertEqual(order.possible_price_limit, 20000.0)
        self.assertEqual(order.client_name, "Алексей")
        self.assertEqual(order.url, "https://profi.ru/backoffice/n.php/order/7849102")

    def test_parse_from_dom_cards(self):
        html_content = """
        <div class="Container__Root">
            <div class="OrderSnippetContainerStyles__Container" id="order_8831920">
                <a class="SnippetBodyStyles__Container" href="/backoffice/order.php?o=8831920" id="8831920">
                    <h3 class="SubjectAndPriceStyles__Title">Настройка сервера Ubuntu и VPN</h3>
                    <span class="SubjectAndPriceStyles__PriceValue">15 000 ₽</span>
                </a>
                <p class="SnippetBodyStyles__MainInfo">Требуется настроить Wireguard и Nginx на чистом VPS.</p>
                <div class="LocationAndScheduleStyles__Container">
                    <span class="LocationAndScheduleStyles__TextContainer">Удаленно</span>
                    <span class="LocationAndScheduleStyles__ScheduleText">Срочно</span>
                </div>
                <div class="StatusAndClientInfoStyles__Name">Мария К.</div>
            </div>
        </div>
        """
        parser = ProfiParser(cookies="p_auth=valid")
        orders = parser.parse_html(html_content)
        self.assertEqual(len(orders), 1)
        order = orders[0]
        self.assertEqual(order.id, "profi_8831920")
        self.assertEqual(order.source, "profi")
        self.assertEqual(order.title, "Настройка сервера Ubuntu и VPN")
        self.assertEqual(order.price_limit, 15000.0)
        self.assertEqual(order.possible_price_limit, 15000.0)
        self.assertIn("Требуется настроить Wireguard", order.description)
        self.assertIn("📍 Место: Удаленно", order.description)
        self.assertIn("📅 Сроки: Срочно", order.description)
        self.assertEqual(order.client_name, "Мария К.")
        self.assertEqual(order.url, "https://profi.ru/backoffice/order.php?o=8831920")

    def test_storage_profi_deduplication(self):
        # Create a Kwork project and a Profi order with same numeric ID
        kwork_proj = KworkProject(
            id=12345,
            title="Kwork task",
            description="Kwork desc",
            price_limit=5000.0,
            possible_price_limit=5000.0,
            source="kwork",
            url="https://kwork.ru/projects/12345",
        )
        profi_proj = KworkProject(
            id="profi_12345",
            title="Profi task",
            description="Profi desc",
            price_limit=15000.0,
            possible_price_limit=15000.0,
            source="profi",
            url="https://profi.ru/backoffice/n.php/order/12345",
        )

        self.assertFalse(self.storage.is_seen(kwork_proj.id))
        self.assertFalse(self.storage.is_seen(profi_proj.id))

        self.storage.mark_seen(kwork_proj, notified=True)
        self.assertTrue(self.storage.is_seen(kwork_proj.id))
        self.assertFalse(self.storage.is_seen(profi_proj.id))

        self.storage.mark_seen(profi_proj, notified=True)
        self.assertTrue(self.storage.is_seen(profi_proj.id))

        # Retrieve both
        loaded_kwork = self.storage.get_project(12345)
        loaded_profi = self.storage.get_project("profi_12345")

        self.assertIsNotNone(loaded_kwork)
        self.assertIsNotNone(loaded_profi)
        self.assertEqual(loaded_kwork.source, "kwork")
        self.assertEqual(loaded_profi.source, "profi")
        self.assertEqual(loaded_kwork.price_limit, 5000.0)
        self.assertEqual(loaded_profi.price_limit, 15000.0)

    def test_parse_graphql_data(self):
        gql_response = {
            "data": {
                "boSearchBoardItems": {
                    "totalCount": 3,
                    "items": [
                        {
                            "id": "DIVIDER",
                            "type": "DIVIDER",
                            "title": "Новые заказы"
                        },
                        {
                            "id": "94510222",
                            "type": "SNIPPET",
                            "title": "Программисты",
                            "description": "Настройка вентилятора в процессоре",
                            "geo": {
                                "remote": {"prefix": "Дистанционно · Санкт-Петербург"},
                                "orderLocation": {"address": "Невский пр."},
                            },
                            "price": {"prefix": "до", "suffix": "", "value": "1500 ₽"},
                            "clientInfo": {"name": "Алла"},
                            "schedule": "Срочно",
                        },
                        {
                            "id": "CAROUSEL",
                            "type": "CAROUSEL",
                            "snippets": [
                                {
                                    "id": "94470064",
                                    "title": "Авитолог консультация",
                                    "description": "Консультация по рекламе",
                                    "price": {"prefix": "от", "suffix": "", "value": "2000 ₽"},
                                    "geo": {"remote": {"prefix": "Дистанционно · Москва"}},
                                }
                            ]
                        }
                    ]
                }
            }
        }
        parser = ProfiParser(browser_profile_dir=None)
        projects = parser.parse_graphql_data(gql_response)
        # CAROUSEL ("Могут вам подойти" cross-sell ads) must be completely excluded
        self.assertEqual(len(projects), 1)

        p1 = projects[0]
        self.assertEqual(p1.id, "profi_94510222")
        self.assertEqual(p1.source, "profi")
        self.assertEqual(p1.title, "Программисты")
        self.assertIn("Настройка вентилятора", p1.description)
        self.assertIn("📍 Место: Дистанционно · Санкт-Петербург, Невский пр.", p1.description)
        self.assertIn("📅 Сроки: Срочно", p1.description)
        self.assertEqual(p1.price_limit, 1500.0)
        self.assertEqual(p1.category_name, "Программисты")
        self.assertEqual(p1.client_name, "Алла")
        self.assertEqual(p1.url, "https://profi.ru/backoffice/n.php?o=94510222")
        self.assertEqual(p1.is_fresh, None)
        self.assertEqual(p1.is_viewed, None)

    def test_parse_graphql_data_divider_and_fresh(self):
        gql_response = {
            "data": {
                "boSearchBoardItems": {
                    "items": [
                        {
                            "id": "94510648",
                            "type": "SNIPPET",
                            "title": "Quiz-сайт",
                            "isFresh": True,
                            "isViewed": False,
                        },
                        {
                            "id": "DIVIDER",
                            "type": "DIVIDER",
                            "title": "Вы посмотрели все новые заказы",
                        },
                        {
                            "id": "CAROUSEL",
                            "type": "CAROUSEL",
                            "snippets": [
                                {"id": "94470064", "title": "Авитолог", "isFresh": True}
                            ]
                        },
                        {
                            "id": "94510222",
                            "type": "SNIPPET",
                            "title": "Программисты",
                            "isFresh": False,
                            "isViewed": True,
                        }
                    ]
                }
            }
        }
        parser = ProfiParser(browser_profile_dir=None)

        # 1. only_fresh=True: should ONLY return Quiz-сайт (before DIVIDER, isFresh=True)
        fresh_projects = parser.parse_graphql_data(gql_response, only_fresh=True)
        self.assertEqual(len(fresh_projects), 1)
        self.assertEqual(fresh_projects[0].id, "profi_94510648")
        self.assertEqual(fresh_projects[0].title, "Quiz-сайт")
        self.assertTrue(fresh_projects[0].is_fresh)
        self.assertFalse(fresh_projects[0].is_viewed)

        # 2. only_fresh=False: should return both board snippets (Quiz-сайт and Программисты), but NOT CAROUSEL
        all_projects = parser.parse_graphql_data(gql_response, only_fresh=False)
        self.assertEqual(len(all_projects), 2)
        ids = [p.id for p in all_projects]
        self.assertIn("profi_94510648", ids)
        self.assertIn("profi_94510222", ids)
        self.assertNotIn("profi_94470064", ids)

    def test_fetch_orders_no_cookies_no_browser(self):
        import asyncio
        parser = ProfiParser(cookies="", browser_profile_dir=None)
        orders = asyncio.run(parser.fetch_orders())
        self.assertEqual(orders, [])

    @patch("src.profi_parser.httpx.AsyncClient.get")
    def test_fetch_orders_http(self, mock_get):
        import asyncio
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.text = """
        <html><body>
        <div class="OrderSnippetContainerStyles__Container" id="order_112233">
            <a href="/backoffice/order.php?o=112233"><h3>Тестовый заказ</h3></a>
            <span class="price-value">5 000 ₽</span>
        </div>
        </body></html>
        """
        mock_get.return_value = mock_resp

        parser = ProfiParser(cookies="p_auth=123", browser_profile_dir=None)
        orders = asyncio.run(parser.fetch_orders())
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].id, "profi_112233")

    def test_telegram_profi_formatting_and_keyboard(self):
        from src.telegram_bot import TelegramNotifier
        from src.models import TelegramConfig

        profi_proj = KworkProject(
            id="profi_94510222",
            title="Программисты",
            description="Настройка вентилятора в процессоре\n\n📍 Место: Дистанционно · Санкт-Петербург\n📅 Сроки: Срочно",
            price_limit=1500.0,
            possible_price_limit=1500.0,
            offers_count=0,
            category_id=0,
            category_name="Profi.ru",
            client_name="Алла",
            client_url="",
            url="https://profi.ru/backoffice/n.php?o=94510222",
            source="profi",
        )

        notifier = TelegramNotifier(config=TelegramConfig(bot_token="test_token", chat_ids=[12345]))
        msg = notifier._format_project_message(profi_proj)
        self.assertIn("https://profi.ru/backoffice/n.php?o=94510222", msg)
        self.assertIn("Программисты", msg)
        self.assertIn("1 500 ₽", msg)
        self.assertIn("Profi.ru", msg)
        self.assertIn("Алла", msg)
        self.assertIn("Биржа: Profi.ru", msg)
        self.assertIn("━━━━━━━━━━━━━━━━━━", msg)
        self.assertNotIn('<a href="">', msg)

        # Check Kwork project header
        kwork_proj = KworkProject(
            id=123,
            title="Kwork task",
            description="Kwork desc",
            price_limit=5000.0,
            possible_price_limit=5000.0,
            offers_count=0,
            url="https://kwork.ru/projects/123",
            source="kwork",
        )
        kwork_msg = notifier._format_project_message(kwork_proj)
        self.assertIn("Биржа: Kwork", kwork_msg)
        self.assertIn("━━━━━━━━━━━━━━━━━━", kwork_msg)

        kb = notifier._get_project_inline_keyboard(profi_proj)
        first_btn = kb["inline_keyboard"][0][0]
        self.assertEqual(first_btn["text"], "🔗 Открыть проект на Profi.ru")
        self.assertEqual(first_btn["url"], "https://profi.ru/backoffice/n.php?o=94510222")

    def test_proposal_generation_for_profi_project(self):
        import asyncio
        from src.telegram_bot import TelegramNotifier
        from src.models import TelegramConfig

        profi_proj = KworkProject(
            id="profi_94510222",
            title="Программисты",
            description="Настройка вентилятора в процессоре",
            price_limit=1500.0,
            possible_price_limit=1500.0,
            offers_count=0,
            category_id=0,
            category_name="Profi.ru",
            client_name="Алла",
            client_url="",
            url="https://profi.ru/backoffice/n.php?o=94510222",
            source="profi",
        )
        self.storage.mark_seen(profi_proj, notified=True)

        mock_ai = AsyncMock()
        mock_ai.is_available = unittest.mock.MagicMock(return_value=True)
        mock_ai.generate_proposal.return_value = {
            "suggested_price": 1500,
            "suggested_days": 1,
            "proposal_text": "Здравствуйте, Алла! Готов настроить вентиляторы в процессоре.",
            "mode_name": "Оптимальный",
        }

        notifier = TelegramNotifier(
            config=TelegramConfig(bot_token="test_token", chat_ids=[12345]),
            storage=self.storage,
            ai_assistant=mock_ai,
        )

        notifier.send_message = AsyncMock(return_value=999)
        notifier.edit_message_text = AsyncMock(return_value=True)

        asyncio.run(
            notifier.handle_proposal_generation(
                chat_id=12345,
                project_id="profi_94510222",
                mode="optimal",
            )
        )

        mock_ai.generate_proposal.assert_called_once()
        cached = self.storage.get_cached_proposal("profi_94510222", "optimal")
        self.assertIsNotNone(cached)
        self.assertEqual(cached["suggested_price"], 1500)
        self.assertIn("Здравствуйте, Алла!", cached["proposal_text"])

    def test_reply_keyboard_buttons_and_commands(self):
        import asyncio
        from src.telegram_bot import TelegramNotifier
        from src.models import TelegramConfig

        notifier = TelegramNotifier(config=TelegramConfig(bot_token="test_token", chat_ids=[12345]))
        kb = notifier.get_main_reply_keyboard()
        buttons = [btn["text"] for row in kb["keyboard"] for btn in row]
        self.assertIn("📋 Заказы Kwork", buttons)
        self.assertIn("📋 Заказы Profi.ru", buttons)
        self.assertIn("🔎 Проверить Kwork", buttons)
        self.assertIn("🔎 Проверить Profi.ru", buttons)

        mock_check = AsyncMock()
        notifier.on_manual_check = mock_check
        notifier.send_message = AsyncMock(return_value=1)

        # Test "🔎 Проверить Profi.ru"
        asyncio.run(notifier.handle_command(12345, "🔎 Проверить Profi.ru"))
        # Allow background create_task to execute
        mock_check.assert_called()

        # Test "🔎 Проверить Kwork"
        mock_check.reset_mock()
        asyncio.run(notifier.handle_command(12345, "🔎 Проверить Kwork"))
        mock_check.assert_called()

    def test_response_price_parsing_and_telegram_display(self):
        # 1. Test model serialization & deserialization with response_price
        p = KworkProject(
            id="profi_94510222",
            title="Программисты",
            description="Настройка вентилятора",
            price_limit=1500,
            possible_price_limit=1500,
            source="profi",
            response_price="39 ₽",
        )
        json_data = p.model_dump_json()
        restored = KworkProject.model_validate_json(json_data)
        self.assertEqual(restored.response_price, "39 ₽")

        # 2. Test JSON state parsing with response_price
        parser = ProfiParser()
        order = parser._parse_order_from_json({
            "id": "94510222",
            "title": "Программисты",
            "price": "1500 ₽",
            "responsePrice": "39 ₽",
        })
        self.assertIsNotNone(order)
        self.assertEqual(order.response_price, "39 ₽")

        # 3. Test Telegram project card formatting includes response_price
        notifier = TelegramNotifier(config=TelegramConfig(bot_token="test_token", chat_ids=[12345]))
        msg = notifier._format_project_message(p)
        self.assertIn("🏷 <b>Цена отклика:</b> 39 ₽", msg)
        self.assertIn("🔴 <b>Биржа: Profi.ru</b>", msg)

        # 4. Test Kwork project without response_price does not have the line
        kwork_p = KworkProject(
            id=123,
            title="Kwork Job",
            description="Desc",
            price_limit=5000,
            possible_price_limit=5000,
            source="kwork",
        )
        kwork_msg = notifier._format_project_message(kwork_p)
        self.assertNotIn("Цена отклика", kwork_msg)


if __name__ == "__main__":
    unittest.main()





