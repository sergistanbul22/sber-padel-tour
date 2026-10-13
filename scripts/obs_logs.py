#!/usr/bin/env python3
# obs_logs.py — сверка ошибок Cloud Functions (v2 = Cloud Run revisions) за сутки.
# Группирует по функции + типу ошибки. Выход 0 = чисто, 1 = есть ошибки.
import json
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from google.oauth2 import service_account
from google.auth.transport.requests import Request

KEY = '/root/.openclaw/workspace/sberpt-firebase-key.json'
FUNCTIONS = {
    'finalizetournament', 'finalizegame', 'recalculatetournament',
    'recalculateallresults', 'deletetournament', 'deletegame', 'archiveratings',
    'manageavatar',
}
WINDOW_HOURS = 25  # запас в 1ч, чтобы ежедневный запуск не пропускал записи

def main():
    creds = service_account.Credentials.from_service_account_file(
        KEY, scopes=['https://www.googleapis.com/auth/cloud-platform'])
    creds.refresh(Request())

    since = (datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)).strftime('%Y-%m-%dT%H:%M:%SZ')
    body = {
        'resourceNames': ['projects/sberpt-a546e'],
        'filter': f'severity>=ERROR AND timestamp>="{since}"',
        'orderBy': 'timestamp desc',
        'pageSize': 200,
    }
    req = urllib.request.Request(
        'https://logging.googleapis.com/v2/entries:list',
        data=json.dumps(body).encode(),
        headers={'Authorization': f'Bearer {creds.token}',
                 'Content-Type': 'application/json'})
    data = json.load(urllib.request.urlopen(req, timeout=30))

    groups = defaultdict(lambda: {'count': 0, 'last': None, 'sample': ''})
    total = 0
    for e in data.get('entries', []):
        labels = e.get('resource', {}).get('labels', {})
        fn = labels.get('configuration_name', '?')
        if fn not in FUNCTIONS:
            continue
        total += 1
        msg = ''
        jp = e.get('jsonPayload')
        if jp and jp.get('message'):
            msg = str(jp['message'])
        elif e.get('textPayload'):
            msg = str(e['textPayload'])
        else:
            msg = '(без текста — инфраструктурная ошибка/500)'
        # нормализуем: первая строка, без динамических хвостов
        key = msg.splitlines()[0][:160] if msg else '(пусто)'
        g = groups[(fn, key)]
        g['count'] += 1
        g['last'] = e.get('timestamp', '?')[:19]
        if not g['sample']:
            g['sample'] = msg[:400]

    print(f'Окно: последние {WINDOW_HOURS} ч; ошибок по 7 функциям: {total}')
    if not groups:
        print('ЧИСТО')
        return 0
    for (fn, key), g in sorted(groups.items(), key=lambda kv: -kv[1]['count']):
        print(f'\n❌ {fn} — {g["count"]}× (последняя {g["last"]} UTC)')
        print(f'   {key}')
        if g['sample'] and len(g['sample'].splitlines()) > 1:
            print('   ---')
            for line in g['sample'].splitlines()[1:8]:
                print('   ' + line)
    return 1

if __name__ == '__main__':
    sys.exit(main())
