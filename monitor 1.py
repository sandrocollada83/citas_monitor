#!/usr/bin/env python3
"""Monitor conservador de citas consulares.

Busca Continuar en la página y en sus iframes. Solo avisa si encuentra
un control visible y habilitado que representa una fecha/hora seleccionable.
No reserva citas ni elude controles de acceso.
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

NO_AVAILABILITY = (
    "no hay horas disponibles", "no hay citas disponibles",
    "no existen citas disponibles", "sin disponibilidad",
    "no available appointments", "no available times",
    "intentalo de nuevo dentro de unos dias",
)
CONTINUE = re.compile(r"continuar|continue", re.I)
SLOT_SELECTORS = (
    'button[data-time]:not([disabled]), button[data-date]:not([disabled]), '
    '[role="gridcell"] button:not([disabled]), '
    'button[aria-label*="hora" i]:not([disabled]), '
    'button[aria-label*="time" i]:not([disabled]), '
    'input[type="radio"][name*="time" i]:not([disabled])'
)


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
    """Persiste state.txt entre ejecuciones si el workflow permite git push."""
    subprocess.run(["git", "config", "user.name", "cita-monitor[bot]"], check=True)
    subprocess.run(["git", "config", "user.email", "bot@cita-monitor.local"], check=True)
    subprocess.run(["git", "add", STATE_FILE], check=True)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if diff.returncode == 1:
        subprocess.run(["git", "commit", "-m", "state: update"], check=True)
        subprocess.run(["git", "push"], check=True)


def page_texts(page):
    """Lee todos los marcos accesibles, incluido el documento principal."""
    texts = []
    for frame in page.frames:
        try:
            texts.append(normalized(frame.locator("body").inner_text(timeout=3000)))
        except Exception as error:
            print(f"[Monitor] No se pudo leer un marco: {type(error).__name__}")
    return texts


def find_continue(page):
    """Busca un botón o enlace visible en todos los marcos."""
    for frame in page.frames:
        choices = (
            frame.get_by_role("button", name=CONTINUE),
            frame.get_by_role("link", name=CONTINUE),
            frame.get_by_text(re.compile(r"continue\s*/\s*continuar", re.I)),
            frame.locator('input[type="submit"][value*="Continu" i]'),
        )
        for locator in choices:
            try:
                for index in range(min(locator.count(), 10)):
                    item = locator.nth(index)
                    if item.is_visible() and item.is_enabled():
                        return item
            except Exception:
                continue
    return None


def inspect_page(page):
    page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(5000)
    print(f"[Monitor] URL final: {page.url}")
    print(f"[Monitor] Marcos detectados: {len(page.frames)}")

    # Puede ser un widget que cargue tarde o esté dentro de un iframe.
    clicked = False
    for attempt in range(3):
        button = find_continue(page)
        if button is not None:
            try:
                button.click(timeout=10000)
                clicked = True
                print("[Monitor] Se pulsó Continuar.")
                break
            except Exception as error:
                print(f"[Monitor] No se pudo pulsar Continuar: {type(error).__name__}")
        page.wait_for_timeout(3000)

    if not clicked:
        print("[Monitor] No se encontró Continuar; se comprobará el contenido visible.")

    page.wait_for_timeout(4000)
    texts = page_texts(page)
    if not texts:
        print("[Monitor] No se pudo leer el contenido de la página.")
        return "unknown"

    if any(phrase in text for text in texts for phrase in NO_AVAILABILITY):
        print("[Monitor] Confirmado: no hay horarios disponibles.")
        return "disabled"

    for frame in page.frames:
        try:
            slots = frame.locator(SLOT_SELECTORS)
            for index in range(min(slots.count(), 100)):
                slot = slots.nth(index)
                if slot.is_visible() and slot.is_enabled():
                    print("[Monitor] Control de fecha/hora seleccionable detectado.")
                    return "active"
        except Exception as error:
            print(f"[Monitor] Error inspeccionando un marco: {type(error).__name__}")

    # Ayuda a diagnosticar sin revelar tokens ni enviar alertas falsas.
    excerpt = " | ".join(text[:220].replace("\n", " ") for text in texts[:3])
    print(f"[Monitor] Texto visible (extracto): {excerpt[:500]}")
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
    except Exception as error:
        print(f"[Monitor] Error o página no verificable: {type(error).__name__}: {error}")
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
