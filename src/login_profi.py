import time
import os
import re
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    print("=" * 65)
    print("Запуск браузера Chrome для авторизации на Profi.ru...")
    print("Пожалуйста, выполните вход в свой аккаунт в открывшемся окне.")
    print("После успешного входа скрипт автоматически сохранит cookies в .env.")
    print("=" * 65)
    sys.stdout.flush()

    user_data_dir = Path("data/profi_browser_profile").resolve()
    user_data_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                channel="chrome",
                headless=False,
                viewport={"width": 1280, "height": 850},
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:
            print(f"Ошибка запуска Google Chrome: {e}. Пробую стандартный запуск...")
            sys.stdout.flush()
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=False,
                viewport={"width": 1280, "height": 850},
            )

        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://profi.ru/backoffice/n.php")

        print("\nОжидание завершения авторизации (окно открыто)...")
        sys.stdout.flush()

        logged_in = False
        start_wait = time.time()

        # Wait up to 10 minutes for user to complete login (SMS / password)
        while time.time() - start_wait < 600:
            try:
                cookies = context.cookies("https://profi.ru")
                title = page.title()
                url = page.url

                # Check if user passed login and reached backoffice
                is_backoffice = "backoffice" in url
                not_login_title = "Вход" not in title and title.strip() != ""

                if is_backoffice and not_login_title and len(cookies) >= 2:
                    # Give extra 3 seconds to finalize session tokens
                    time.sleep(3)
                    cookies = context.cookies("https://profi.ru")
                    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

                    print("\n[УСПЕХ] Вход выполнен успешно!")
                    print(f"Захвачено {len(cookies)} cookies для profi.ru.")
                    sys.stdout.flush()

                    # Save to .env
                    env_path = Path(".env")
                    content = env_path.read_text(encoding="utf-8") if env_path.exists() else ""

                    if "PROFI_ENABLED" in content:
                        content = re.sub(r"PROFI_ENABLED\s*=.*", "PROFI_ENABLED=true", content)
                    else:
                        content += "\nPROFI_ENABLED=true\n"

                    if "PROFI_COOKIES" in content:
                        content = re.sub(r'PROFI_COOKIES\s*=.*', f'PROFI_COOKIES="{cookie_str}"', content)
                    else:
                        content += f'PROFI_COOKIES="{cookie_str}"\n'

                    env_path.write_text(content, encoding="utf-8")
                    print("Файл .env успешно обновлен: PROFI_ENABLED=true и PROFI_COOKIES сохранены.")
                    sys.stdout.flush()
                    logged_in = True
                    break
            except Exception:
                pass
            time.sleep(2)

        if not logged_in:
            print("\nВремя ожидания входа истекло (10 мин).")
            sys.stdout.flush()
        else:
            print("Браузер закрывается...")
            sys.stdout.flush()
            time.sleep(2)

        context.close()


if __name__ == "__main__":
    main()
