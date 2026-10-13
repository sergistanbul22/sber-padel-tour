#!/usr/bin/env python3
"""Миграция аватарок из Firestore (base64) в Firebase Storage (URL).

Для каждого игрока с avatar, начинающимся с 'data:image':
  1) бэкап base64 в локальный JSON (откат при необходимости)
  2) full.jpg  — исходное фото как есть (512 px)
  3) thumb.jpg — миниатюра 128 px q85
  4) документ обновляется: avatar=URL(thumb), avatarFull=URL(full)

Запуск:  bot_venv/bin/python scripts/migrate_avatars.py --dry-run
         bot_venv/bin/python scripts/migrate_avatars.py
"""
import argparse
import base64
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from google.cloud import firestore, storage
from google.oauth2 import service_account

WS = Path('/root/.openclaw/workspace')
KEY = WS / 'sberpt-firebase-key.json'
PROJECT = 'sberpt-a546e'
BUCKET = 'sberpt-a546e-avatars'  # не дефолтный Firebase-бакет: чтение публичное (IAM), запись — Admin SDK
BACKUP_DIR = WS / 'backups'
THUMB_SIZE = 128
THUMB_QUALITY = 85


def public_url(blob_name: str) -> str:
    return f'https://storage.googleapis.com/{BUCKET}/{blob_name}'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='только показать, что было бы сделано')
    args = ap.parse_args()

    creds = service_account.Credentials.from_service_account_file(str(KEY))
    db = firestore.Client(project=PROJECT, credentials=creds)
    st = storage.Client(project=PROJECT, credentials=creds)
    bucket = st.bucket(BUCKET)
    if not bucket.exists():
        print(f'❌ Бакет {BUCKET} не существует!', file=sys.stderr)
        return 1

    docs = list(db.collection('padel_players').stream())
    targets = []
    for d in docs:
        data = d.to_dict() or {}
        av = data.get('avatar')
        if isinstance(av, str) and av.startswith('data:image'):
            targets.append((d.id, data))

    print(f'Игроков всего: {len(docs)}, с base64-аватаркой: {len(targets)}')
    if not targets:
        print('Нечего мигрировать.')
        return 0

    if args.dry_run:
        for pid, data in targets:
            kb = len(data['avatar']) / 1024
            print(f'  DRY-RUN {pid:24s} {data.get("name", "?")[:24]:26s} {kb:7.0f} КБ')
        print('\nБэкап и запись НЕ выполнялись. Уберите --dry-run для реального прогона.')
        return 0

    BACKUP_DIR.mkdir(exist_ok=True)
    backup_path = BACKUP_DIR / f'avatars_backup_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json'
    backup = {}
    ok, failed = 0, 0

    for pid, data in targets:
        name = data.get('name', '?')
        try:
            raw = base64.b64decode(data['avatar'].split(',', 1)[1])
            backup[pid] = {'name': name, 'avatar': data['avatar'],
                           'avatarUpdatedAt': data.get('avatarUpdatedAt')}

            from PIL import Image
            img = Image.open(io.BytesIO(raw)).convert('RGB')

            # миниатюра
            thumb = img.copy()
            thumb.thumbnail((THUMB_SIZE, THUMB_SIZE))
            buf = io.BytesIO()
            thumb.save(buf, 'JPEG', quality=THUMB_QUALITY, optimize=True)
            thumb_bytes = buf.getvalue()

            full_name = f'avatars/{pid}/full.jpg'
            thumb_name = f'avatars/{pid}/thumb.jpg'

            fblob = bucket.blob(full_name)
            fblob.upload_from_string(raw, content_type='image/jpeg')
            tblob = bucket.blob(thumb_name)
            tblob.upload_from_string(thumb_bytes, content_type='image/jpeg')

            full_url = public_url(full_name)
            thumb_url = public_url(thumb_name)

            db.collection('padel_players').document(pid).update({
                'avatar': thumb_url,
                'avatarFull': full_url,
                'avatarUpdatedAt': int(datetime.now(timezone.utc).timestamp() * 1000),
            })
            ok += 1
            print(f'  ✅ {pid} {name}: thumb {len(thumb_bytes)//1024} КБ, full {len(raw)//1024} КБ')
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f'  ❌ {pid} {name}: {e}', file=sys.stderr)

        backup_path.write_text(json.dumps(backup, ensure_ascii=False, indent=1), encoding='utf-8')

    print(f'\nИТОГО: перенесено {ok}, ошибок {failed}. Бэкап: {backup_path}')
    return 0 if failed == 0 else 2


if __name__ == '__main__':
    sys.exit(main())
