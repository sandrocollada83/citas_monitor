#!/usr/bin/env python3
"""Monitor conservador de disponibilidad de citas consulares.

Solo alerta cuando aparece una opción visible de fecha u hora tras pulsar Continuar.
No intenta reservar citas ni superar controles de acceso.
"""
import os
import re
import subprocess
import unicodedata

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

TARGET_URL = os.environ["TARGET_URL"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE = "state.txt"


def normalized(text):
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def send_telegram(message):
    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": message,
              "disable_web_page_preview": True},
        timeout=15,
    )
    response.raise_for_status()
    print("[Telegram] Mensaje enviado.")


def get_previous_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as file:
            return file.read().strip()
    except FileNotFoundError:
        return "unknown"


def save_state(value):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        file.write(value + "\n")


def persist_state():
    """Persiste state.txt entre ejecuciones de GitHub Actions."""
    subprocess.run(["git", "config", "user.name", "cita-monitor[bot]"], check=True)
    subprocess.run(["git", "config", "user.email", "bot@cita-monitor.local"], check=True)
    subprocess.run(["git", "add", STATE_FILE], check=True)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if diff.returncode == 1:
        subprocess.run(["git", "commit", "-m", "state: update"], check=True)
        subprocess.run(["git", "push"], check=True)


def inspect_page(page):
    page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(3000)

    # El botón de la primera pantalla NO prueba que haya citas.
    continue_button = page.get_by_role("button", name=re.compile(r"continue|continuar", re.I))
    if continue_button.count() == 0:
        continue_button = page.get_by_role("link", name=re.compile(r"continue|continuar", re.I))
    if continue_button.count() == 0:
        continue_button = page.get_by_text(re.compile(r"continue\s*/\s*continuar", re.I))

    if continue_button.count() > 0:
        continue_button.first.click(timeout=10000)
        page.wait_for_timeout(4000)
    else:
        print("[Monitor] No se encontró Continuar; comprobando si ya estamos en el paso siguiente.")

    body = normalized(page.locator("body").inner_text(timeout=15000))
    if any(phrase in body for phrase in (
        "no hay horas disponibles", "no hay citas disponibles",
        "no existen citas disponibles", "sin disponibilidad",
        "no available appointments", "no available times",
    )):
        return "disabled"

    # Solo una fecha/hora seleccionable es indicio de disponibilidad.
    # Evitar falsos positivos por botones genéricos como Continuar.
    visible_slots = page.locator(
        'button[data-time], button[data-date], '
        '[role="gridcell"] button:not([disabled]), '
        'button[aria-label*="hora" i]:not([disabled]), '
        'button[aria-label*="time" i]:not([disabled]), '
        'input[type="radio"][name*="time" i]:not([disabled])'
    )
    for i in range(min(visible_slots.count(), 100)):
        candidate = visible_slots.nth(i)
        if candidate.is_visible() and candidate.is_enabled():
            print("[Monitor] Posible horario seleccionable detectado.")
            return "active"

    # Una pantalla distinta no es prueba suficiente para afirmar disponibilidad.
    print("[Monitor] Sin confirmación de horario. No se enviará alerta.")
    return "unknown"


def main():
    if os.environ.get("TEST_TELEGRAM") == "1":
        send_telegram("✅ Prueba del monitor consular: Telegram funciona correctamente.")
        return

    previous = get_previous_state()
    current = "unknown"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                current = inspect_page(page)
            finally:
                browser.close()
    except (PlaywrightTimeout, Exception) as error:
        print(f"[Monitor] Error o página no verificable: {error}")
        current = "unknown"

    print(f"[Monitor] Anterior={previous}; actual={current}")
    if current == "active" and previous != "active":
        send_telegram(
            "🚨 Posible cita consular disponible. Verifica inmediatamente la página:\n"
            + TARGET_URL
        )
    if current != "unknown":
        save_state(current)
        persist_state()


if __name__ == "__main__":
    main()
