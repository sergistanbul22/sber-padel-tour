#!/usr/bin/env python3
# obs_smoke.py — ежедневный «пульс» 7 облачных функций SBER Padel Tour.
# Шлём реальный HTTPS-вызов БЕЗ токена и ждём честный 401 UNAUTHENTICATED —
# это доказывает: функция задеплоена, доступна, исполняет код.
# Таймаут / 404 / 5xx / невалидный ответ = проблема.
import json
import sys
import urllib.request
import urllib.error

REGION = 'europe-west1'
PROJECT = 'sberpt-a546e'
FUNCTIONS = [
    'finalizeTournament',
    'finalizeGame',
    'recalculateTournament',
    'recalculateAllResults',
    'deleteTournament',
    'deleteGame',
    'archiveRatings',
    'manageAvatar',
]
# Живые ответы бывают двух видов (порядок guard'ов разный):
#  - 401/403 UNAUTHENTICATED — сначала проверка авторизации
#  - 400 INVALID_ARGUMENT     — сначала валидация аргументов («Не передан ...»)
# Оба значат «функция задеплоена и исполняет код». Всё остальное — проблема.
# Холодный старт после деплоя иногда даёт разовый 500 — пробуем до 3 раз.
TIMEOUT = 20
ATTEMPTS = 3

def probe_once(name):
    url = f'https://{REGION}-{PROJECT}.cloudfunctions.net/{name}'
    req = urllib.request.Request(
        url, data=b'{"data":{}}',
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode('utf-8', 'replace')[:300]
            return False, f'HTTP {resp.status} без токена (guard сломан?): {body}'
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'replace')[:300]
        if e.code in (401, 403):
            return True, 'OK (отказ в авторизации)'
        if e.code == 400 and 'INVALID_ARGUMENT' in body:
            return True, 'OK (валидация аргументов)'
        return False, f'HTTP {e.code}: {body}'
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'

def probe(name):
    last = ''
    for attempt in range(1, ATTEMPTS + 1):
        ok, detail = probe_once(name)
        if ok:
            return True, f'{detail}' + (f' (попытка {attempt})' if attempt > 1 else '')
        last = detail
        if attempt < ATTEMPTS:
            import time
            time.sleep(5)
    return False, last + f' ({ATTEMPTS} попытки подряд)'

def main():
    problems = []
    for fn in FUNCTIONS:
        ok, detail = probe(fn)
        mark = '✅' if ok else '❌'
        print(f'{mark} {fn:24s} {detail}')
        if not ok:
            problems.append((fn, detail))
    print()
    if problems:
        print(f'ИТОГО: {len(problems)} из {len(FUNCTIONS)} функций НЕОТВЕЧАЮТ')
        sys.exit(1)
    print(f'ИТОГО: все {len(FUNCTIONS)} функций живы')

if __name__ == '__main__':
    main()
