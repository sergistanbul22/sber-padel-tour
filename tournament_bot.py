#!/usr/bin/env python3
"""
Sber Padel Tour Telegram Bot
- Уведомления о завершённых играх, турнирах, тренировках
- Уведомления о новых регистрациях (users)
- Polling команд Telegram
"""

import os
import sys
import json
import time
import signal
import requests
import firebase_admin
from firebase_admin import credentials, firestore

# ========== НАСТРОЙКИ ==========
TELEGRAM_BOT_TOKEN = "8763865911:AAG2xHWXlYuT54ElXy1PgtQ8HuZQnjXdlIg"
CHAT_ID_SBER_PADEL = "-1002556296907"
CHAT_ID_PRO = "-4794823132"
ALL_CHAT_IDS = [CHAT_ID_SBER_PADEL, CHAT_ID_PRO]
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TRAINING_BOOKING_CHAT_ID = "-1002556296907"  # SBER-PADEL для уведомлений о записях
FIREBASE_KEY_FILE = os.path.join(os.path.dirname(__file__), "sberpt-firebase-key.json")

# Tracking files
SENT_GAMES_FILE = os.path.join(os.path.dirname(__file__), "sent_games.json")
SENT_TOURNAMENTS_FILE = os.path.join(os.path.dirname(__file__), "sent_tournaments.json")
SENT_TRAININGS_FILE = os.path.join(os.path.dirname(__file__), "sent_trainings.json")
SENT_USERS_FILE = os.path.join(os.path.dirname(__file__), "sent_user_events.json")

# ========== FIREBASE INIT ==========
_db = None

def init_firebase():
    global _db
    if _db:
        return _db
    if not os.path.exists(FIREBASE_KEY_FILE):
        print(f"[ERROR] Файл {FIREBASE_KEY_FILE} не найден!")
        sys.exit(1)
    cred = credentials.Certificate(FIREBASE_KEY_FILE)
    firebase_admin.initialize_app(cred, {"projectId": "sberpt-a546e"})
    _db = firestore.client()
    return _db

# ========== PLAYER NAMES CACHE ==========
_player_names_cache = {}
_player_ratings_cache = {}
_players_watch = None

def listen_player_names(db):
    global _players_watch
    def on_players_snapshot(col_snapshot, changes, read_time):
        for change in changes:
            if change.type.name in ('ADDED', 'MODIFIED'):
                d = change.document.to_dict()
                _player_names_cache[change.document.id] = d.get("name", change.document.id)
                _player_ratings_cache[change.document.id] = d.get("rating", 0)
            elif change.type.name == 'REMOVED':
                _player_names_cache.pop(change.document.id, None)
                _player_ratings_cache.pop(change.document.id, None)
        print(f"[OK] Игроков в кэше: {len(_player_names_cache)}")
    _players_watch = db.collection("padel_players").on_snapshot(on_players_snapshot)

def get_player_name(pid):
    if not pid or pid == "???":
        return "???"
    name = _player_names_cache.get(pid)
    if name:
        return name
    # Fallback: try to load from Firestore if not in cache
    try:
        doc = db.collection("padel_players").document(pid).get()
        if doc.exists:
            n = doc.to_dict().get("name", pid)
            _player_names_cache[pid] = n
            return n
    except Exception as e:
        print(f"[WARN] get_player_name fallback failed for {pid}: {e}")
    return pid

def get_player_rating(pid):
    if not pid:
        return 0
    rating = _player_ratings_cache.get(pid)
    if rating is not None:
        return rating
    # Fallback: try to load from Firestore
    try:
        doc = db.collection("padel_players").document(pid).get()
        if doc.exists:
            r = doc.to_dict().get("rating", 0)
            _player_ratings_cache[pid] = r
            return r
    except Exception as e:
        print(f"[WARN] get_player_rating fallback failed for {pid}: {e}")
    return 0

# ========== SENT EVENTS TRACKING ==========
def load_sent_ids(filepath):
    if not os.path.exists(filepath):
        return set()
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data.get("ids", []))
    except Exception as e:
        print(f"[WARN] Ошибка загрузки {filepath}: {e}")
        return set()

def save_sent_ids(filepath, id_set):
    try:
        tmp = filepath + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ids": list(id_set)}, f, ensure_ascii=False)
        os.replace(tmp, filepath)
    except Exception as e:
        print(f"[WARN] Ошибка сохранения {filepath}: {e}")

# ========== TELEGRAM ==========
def send_telegram(text, chat_id):
    try:
        resp = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=30
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[ERROR] Telegram → {chat_id}: {e}")
        return False

def send_to_all(text):
    for cid in ALL_CHAT_IDS:
        send_telegram(text, cid)

# ========== FORMAT MESSAGES ==========
def format_game(data, doc_id):
    # Support both old format (team1Player1/2, team2Player1/2, score1/2)
    # and new format (pair1[], pair2[], sets[{score1, score2}])
    
    pair1 = data.get("pair1", [])
    pair2 = data.get("pair2", [])
    sets = data.get("sets", [])
    rating_changes = data.get("ratingChanges", {})
    
    if pair1 and pair2:
        # New format
        # Determine winner by counting won sets
        pair1_wins = 0
        pair2_wins = 0
        set_scores = []
        for s in sets:
            s1 = s.get("score1", 0)
            s2 = s.get("score2", 0)
            set_scores.append(f"{s1}:{s2}")
            if s1 > s2:
                pair1_wins += 1
            elif s2 > s1:
                pair2_wins += 1
        
        # Winner first
        if pair1_wins >= pair2_wins:
            winners, losers = pair1, pair2
        else:
            winners, losers = pair2, pair1
        
        def fmt_player(pid):
            name = get_player_name(pid)
            rating = get_player_rating(pid)
            return f"{name} ({rating:.0f})"
        
        w1, w2 = fmt_player(winners[0]), fmt_player(winners[1])
        l1, l2 = fmt_player(losers[0]), fmt_player(losers[1])
        
        sum_win = sum(get_player_rating(pid) - rating_changes.get(pid, 0) for pid in winners)
        sum_lose = sum(get_player_rating(pid) - rating_changes.get(pid, 0) for pid in losers)
        
        score_line = " ".join(set_scores)
        
        # Rating changes lines
        rating_lines = []
        for pid in winners + losers:
            before = get_player_rating(pid) - rating_changes.get(pid, 0)
            after = get_player_rating(pid)
            change = rating_changes.get(pid, 0)
            arrow = "📈" if change > 0 else "📉"
            rating_lines.append(f"{before:.0f} {arrow} {after:.0f} ({change:+.0f})")
        
        ratings_text = "\n".join(rating_lines)
        
        return (
            f"🎾 <b>Игра завершена!</b>\n\n"
            f"{w1} и {w2} |{sum_win:.0f}| выиграли у {l1} и {l2} |{sum_lose:.0f}|\n"
            f"{score_line}\n\n"
            f"{ratings_text}\n\n"
            f"👉 <a href='https://sber-padel-tour.ru/'>Перейти на сайт</a>"
        )
    else:
        # Old format fallback
        t1p1 = get_player_name(data.get("team1Player1", "?"))
        t1p2 = get_player_name(data.get("team1Player2", "?"))
        t2p1 = get_player_name(data.get("team2Player1", "?"))
        t2p2 = get_player_name(data.get("team2Player2", "?"))
        t1 = f"{t1p1} & {t1p2}"
        t2 = f"{t2p1} & {t2p2}"
        s1 = data.get("score1", 0)
        s2 = data.get("score2", 0)
        
        return (
            f"🎾 <b>Игра завершена!</b>\n\n"
            f"{t1}  <b>{s1}:{s2}</b>  {t2}\n\n"
            f"👉 <a href='https://sber-padel-tour.ru/'>Перейти на сайт</a>"
        )

def format_tournament(data, doc_id):
    name = data.get("name", "Без названия")
    date = data.get("date", "")
    t_type = "Round Robin" if data.get("type") == "roundrobin" else "Americano"
    
    medals = ["🥇", "🥈", "🥉"]
    results_text = ""
    full_table = ""
    
    if data.get("type") == "roundrobin":
        pair_leaderboard = data.get("pairLeaderboard", []) or data.get("leaderboard", [])
        for i, pair in enumerate(pair_leaderboard[:3]):
            pids = pair.get("playerIds", [])
            pnames = " & ".join(get_player_name(pid) for pid in pids)
            w = pair.get("wins", 0)
            l = pair.get("losses", 0)
            gf = pair.get("gamesFor", 0)
            ga = pair.get("gamesAgainst", 0)
            results_text += f"{medals[i]} <b>{pnames}</b> — Игры {w}-{l} | Очки {gf}-{ga}\n"
        
        # Full table (top 10)
        lines = []
        for idx, pair in enumerate(pair_leaderboard[:10]):
            pids = pair.get("playerIds", [])
            pnames = " & ".join(get_player_name(pid) for pid in pids)
            w = pair.get("wins", 0)
            l = pair.get("losses", 0)
            gf = pair.get("gamesFor", 0)
            ga = pair.get("gamesAgainst", 0)
            diff = pair.get("gameDiff", 0)
            diff_str = f"+{diff}" if diff > 0 else str(diff)
            lines.append(f"{idx+1}. {pnames} — Игры {w}-{l} | Очки {gf}-{ga} | Разница {diff_str}")
        full_table = "\n".join(lines)
    else:
        leaderboard = data.get("leaderboard", [])
        for i, p in enumerate(leaderboard[:3]):
            pname = get_player_name(p.get("playerId", "???"))
            results_text += f"{medals[i]} <b>{pname}</b> — {p.get('points', 0)} очков\n"
        
        # Full table (top 10)
        lines = []
        for idx, p in enumerate(leaderboard[:10]):
            pname = get_player_name(p.get("playerId", "???"))
            lines.append(f"{idx+1}. {pname} — {p.get('points', 0)}")
        full_table = "\n".join(lines)
    
    return (
        f"🏆 <b>Завершился турнир!</b>\n\n"
        f"📌 <b>{name}</b>\n"
        f"📅 {date}\n"
        f"🎯 Тип: {t_type}\n\n"
        f"<b>Итоговые результаты:</b>\n\n"
        f"{results_text}\n"
        f"<b>Полная таблица:</b>\n"
        f"<pre>{full_table}</pre>\n\n"
        f"👉 <a href='https://sber-padel-tour.ru/'>Перейти на сайт</a>"
    )

def format_training(data, doc_id):
    date = data.get("date", "")
    time_start = data.get("timeStart", "")
    time_end = data.get("timeEnd", "")
    location = data.get("location", "")
    group = data.get("group", "")
    training_type = data.get("trainingType", "open")
    total_slots = data.get("totalSlots", 4)
    
    time_str = f"{time_start}–{time_end}" if time_start and time_end else (time_start or "")
    type_label = "Закрытая (по заявкам)" if training_type == "closed" else "Открытая"
    
    info = f"📅 {date}"
    if time_str: info += f"\n⏰ {time_str}"
    if location: info += f"\n📍 {location}"
    if group: info += f"\n👥 Группа: {group}"
    info += f"\n🔒 Тип: {type_label}"
    info += f"\n👤 Мест: {total_slots}"
    
    return (
        f"💪 <b>Новая тренировка!</b>\n\n"
        f"{info}\n\n"
        f"👉 Запишись по ссылке:\n"
        f"<a href='https://sber-padel-tour.ru/?training={doc_id}'>https://sber-padel-tour.ru/?training={doc_id}</a>"
    )

def format_new_user(data, doc_id):
    first_name = data.get("firstName", "")
    last_name = data.get("lastName", "")
    name = f"{last_name or ''} {first_name or ''}".strip() or doc_id
    
    return (
        f"✅ Новая регистрация на Sber Padel Tour!\n\n"
        f"👤 {name}\n\n"
        f"👉 <a href='https://sber-padel-tour.ru/'>Перейти на сайт</a>"
    )

# ========== FIRESTORE LISTENERS ==========
def on_games_snapshot(col_snapshot, changes, read_time):
    sent_ids = load_sent_ids(SENT_GAMES_FILE)
    new_ids = set()
    
    for change in changes:
        if change.type.name == "MODIFIED":
            doc_id = change.document.id
            if doc_id in sent_ids:
                continue
            data = change.document.to_dict()
            if data.get("status") == "finished":
                print(f"[EVENT] Игра завершена: {doc_id}")
                msg = format_game(data, doc_id)
                send_to_all(msg)
                new_ids.add(doc_id)
        elif change.type.name == "ADDED":
            doc_id = change.document.id
            data = change.document.to_dict()
            if data.get("status") == "finished" and doc_id not in sent_ids:
                print(f"[EVENT] Игра завершена (add): {doc_id}")
                msg = format_game(data, doc_id)
                send_to_all(msg)
                new_ids.add(doc_id)
    
    if new_ids:
        sent_ids.update(new_ids)
        save_sent_ids(SENT_GAMES_FILE, sent_ids)

def on_tournaments_snapshot(col_snapshot, changes, read_time):
    sent_ids = load_sent_ids(SENT_TOURNAMENTS_FILE)
    new_ids = set()
    
    for change in changes:
        if change.type.name == "MODIFIED":
            doc_id = change.document.id
            if doc_id in sent_ids:
                continue
            data = change.document.to_dict()
            if data.get("status") == "finished":
                print(f"[EVENT] Турнир завершён: {doc_id}")
                msg = format_tournament(data, doc_id)
                send_to_all(msg)
                new_ids.add(doc_id)
    
    if new_ids:
        sent_ids.update(new_ids)
        save_sent_ids(SENT_TOURNAMENTS_FILE, sent_ids)

def on_trainings_snapshot(col_snapshot, changes, read_time):
    sent_ids = load_sent_ids(SENT_TRAININGS_FILE)
    new_ids = set()
    
    for change in changes:
        if change.type.name == "ADDED":
            doc_id = change.document.id
            if doc_id in sent_ids:
                continue
            data = change.document.to_dict()
            print(f"[EVENT] Новая тренировка: {doc_id}")
            msg = format_training(data, doc_id)
            group = data.get("group", "")
            if group == "Sber Padel Pro":
                send_telegram(msg, CHAT_ID_PRO)
            else:
                send_telegram(msg, CHAT_ID_SBER_PADEL)
            new_ids.add(doc_id)
    
    if new_ids:
        sent_ids.update(new_ids)
        save_sent_ids(SENT_TRAININGS_FILE, sent_ids)

def on_users_snapshot(col_snapshot, changes, read_time):
    sent_ids = load_sent_ids(SENT_USERS_FILE)
    new_ids = set()
    
    for change in changes:
        if change.type.name == "ADDED":
            doc_id = change.document.id
            if doc_id in sent_ids:
                continue
            data = change.document.to_dict()
            # Skip super admin
            if data.get("role") == "superadmin":
                sent_ids.add(doc_id)
                continue
            print(f"[EVENT] Новая регистрация: {doc_id}")
            msg = format_new_user(data, doc_id)
            send_telegram(msg, CHAT_ID_SBER_PADEL)
            new_ids.add(doc_id)
    
    if new_ids:
        sent_ids.update(new_ids)
        save_sent_ids(SENT_USERS_FILE, sent_ids)

# ========== TRAINING APPLICATIONS (closed trainings) ==========
class TrainingApplicationListener:
    """Отслеживает новые заявки на закрытые тренировки Sber Padel Pro."""

    def __init__(self):
        self._initialized = False
        self._seen = {}  # trainingId -> set(playerId)

    def on_snapshot(self, doc_snapshot, changes, read_time):
        if not self._initialized:
            for doc in doc_snapshot:
                data = doc.to_dict()
                apps = data.get("applications", []) or []
                self._seen[doc.id] = set(a.get("playerId") for a in apps if a.get("playerId"))
            self._initialized = True
            return

        for change in changes:
            if change.type.name not in ("ADDED", "MODIFIED"):
                continue
            doc = change.document
            did = doc.id
            data = doc.to_dict()

            # Только закрытые тренировки группы sber padel pro
            if data.get("trainingType") != "closed":
                continue
            group = (data.get("group") or "").strip().lower()
            if group != "sber padel pro":
                continue

            apps = data.get("applications", []) or []
            current = set(a.get("playerId") for a in apps if a.get("playerId"))
            previous = self._seen.get(did, set())
            new_ids = current - previous

            if new_ids:
                self._send(did, data, apps, new_ids)

            self._seen[did] = current

    def _send(self, training_id, data, applications, new_ids):
        date = data.get("date", "")
        time_start = data.get("timeStart", "")
        time_end = data.get("timeEnd", "")
        location = data.get("location", "Без локации")
        total_slots = data.get("totalSlots", 4)
        slots = data.get("slots", []) or []

        confirmed = [s for s in slots if s.get("playerId")]
        remaining = total_slots - len(confirmed)

        applicant_name = ""
        for app in applications:
            if app.get("playerId") in new_ids:
                applicant_name = app.get("playerName") or get_player_name(app.get("playerId"))
                break

        time_str = f"{time_start}–{time_end}" if time_end else time_start

        apps_lines = []
        for i, app in enumerate(applications, 1):
            name = app.get("playerName") or get_player_name(app.get("playerId"))
            apps_lines.append(f"{i}. {name}")

        conf_lines = []
        for i, s in enumerate(confirmed, 1):
            name = s.get("playerName") or get_player_name(s.get("playerId"))
            conf_lines.append(f"{i}. {name}")

        text = (
            f"🔔 <b>Новая заявка на тренировку</b> — {applicant_name}\n\n"
            f"📅 {date}\n"
            f"⏰ {time_str}\n"
            f"📍 {location}\n\n"
            f"<b>Заявки:</b>\n"
            f"{'\n'.join(apps_lines) or '—'}\n\n"
            f"<b>Подтверждены:</b>\n"
            f"{'\n'.join(conf_lines) or '—'}\n\n"
            f"Осталось {remaining} мест"
        )

        try:
            resp = requests.post(
                f"{TELEGRAM_API}/sendMessage",
                json={
                    "chat_id": CHAT_ID_PRO,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
            resp.raise_for_status()
            print(f"[EVENT] Заявка на тренировку {training_id} → {CHAT_ID_PRO}")
        except Exception as e:
            print(f"[ERROR] Не удалось отправить заявку {training_id}: {e}")


# ========== TRAINING BOOKINGS (open trainings) ==========
class TrainingBookingListener:
    """Отслеживает новые записи на открытые тренировки."""

    def __init__(self):
        self._initialized = False
        self._seen = {}  # trainingId -> set(playerId)

    def on_snapshot(self, doc_snapshot, changes, read_time):
        if not self._initialized:
            for doc in doc_snapshot:
                data = doc.to_dict()
                slots = data.get("slots", []) or []
                self._seen[doc.id] = set(s.get("playerId") for s in slots if s.get("playerId"))
            self._initialized = True
            return

        for change in changes:
            if change.type.name not in ("ADDED", "MODIFIED"):
                continue
            doc = change.document
            did = doc.id
            data = doc.to_dict()

            # Только открытые тренировки
            if data.get("trainingType") == "closed":
                continue

            slots = data.get("slots", []) or []
            current = set(s.get("playerId") for s in slots if s.get("playerId"))
            previous = self._seen.get(did, set())
            new_ids = current - previous

            if new_ids:
                self._send(did, data, slots)

            self._seen[did] = current

    def _send(self, training_id, data, slots):
        date = data.get("date", "")
        time_start = data.get("timeStart", "")
        time_end = data.get("timeEnd", "")
        location = data.get("location", "Без локации")
        total_slots = data.get("totalSlots", 4)

        time_str = f"{time_start}–{time_end}" if time_end else time_start

        booked_players = []
        for i, s in enumerate(slots, 1):
            if s.get("playerId"):
                name = s.get("playerName") or get_player_name(s.get("playerId"))
                booked_players.append(f"{i}. {name}")

        remaining = total_slots - len(booked_players)

        last_player_name = ""
        if booked_players:
            last_player_name = booked_players[-1].split(". ", 1)[1] if ". " in booked_players[-1] else ""

        text = (
            f"🔔 <b>Новая запись на тренировку</b> — {last_player_name}\n\n"
            f"📅 {date}\n"
            f"⏰ {time_str}\n"
            f"📍 {location}\n\n"
            f"<b>Записаны:</b>\n"
            f"{'\n'.join(booked_players) or '—'}\n\n"
            f"Осталось {remaining} мест"
        )

        try:
            resp = requests.post(
                f"{TELEGRAM_API}/sendMessage",
                json={
                    "chat_id": TRAINING_BOOKING_CHAT_ID,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
            resp.raise_for_status()
            print(f"[EVENT] Запись на тренировку {training_id} → {TRAINING_BOOKING_CHAT_ID}")
        except Exception as e:
            print(f"[ERROR] Не удалось отправить уведомление о записи {training_id}: {e}")


# ========== RATINGS COMMAND ==========
def get_ratings(db):
    """Получает рейтинги игроков из Firestore."""
    try:
        players = []
        for doc in db.collection("padel_players").stream():
            d = doc.to_dict()
            players.append({
                "name": d.get("name", "Без имени"),
                "rating": d.get("rating", 100),
                "tournaments": d.get("tournamentsPlayed", 0)
            })
        players.sort(key=lambda x: x["rating"], reverse=True)
        return players
    except Exception as e:
        print(f"[WARN] Ошибка получения рейтингов: {e}")
        return []

def format_ratings(players):
    """Форматирует список рейтингов в текст."""
    if not players:
        return "📊 Рейтинг пока недоступен"
    
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = ["📊 <b>Рейтинг игроков</b>\n"]
    
    for i, p in enumerate(players[:20], 1):
        medal = medals.get(i, "")
        lines.append(f"{i}. {p['name']} — <b>{p['rating']:.1f}</b> {medal}")
    
    return "\n".join(lines)

# ========== POLLING COMMANDS ==========
def poll_commands():
    offset = 0
    while _running:
        try:
            resp = requests.get(
                f"{TELEGRAM_API}/getUpdates",
                params={"offset": offset, "limit": 10, "timeout": 30},
                timeout=35
            )
            resp.raise_for_status()
            data = resp.json()
            
            if not data.get("ok"):
                time.sleep(5)
                continue
            
            for update in data.get("result", []):
                offset = max(offset, update["update_id"] + 1)
                msg = update.get("message", {})
                text = msg.get("text", "")
                chat_id = msg.get("chat", {}).get("id", "")
                
                if text == "/trainings":
                    send_trainings_list(chat_id)
                elif text == "/rating":
                    db = init_firebase()
                    players = get_ratings(db)
                    msg = format_ratings(players)
                    send_telegram(msg, chat_id)
                    print(f"[CMD] /rating от {chat_id}")
                elif text == "/start":
                    send_telegram(
                        "👋 Привет! Бот Sber Padel Tour.\n\n"
                        "/trainings — список тренировок\n"
                        "/rating — рейтинг игроков",
                        chat_id
                    )
                    
        except requests.exceptions.ReadTimeout:
            continue
        except Exception as e:
            print(f"[WARN] Ошибка polling: {e}")
            time.sleep(5)

def send_trainings_list(chat_id):
    try:
        db = init_firebase()
        docs = db.collection("padel_trainings").order_by("date").limit(10).stream()
        lines = ["🎾 <b>Ближайшие тренировки:</b>\n"]
        for doc in docs:
            d = doc.to_dict()
            date = d.get("date", "")
            time = d.get("time", "")
            coach = d.get("coach", "")
            level = d.get("level", "")
            t = f"📅 {date}"
            if time: t += f" ⏰ {time}"
            if coach: t += f" | {coach}"
            if level: t += f" | {level}"
            lines.append(t)
        if len(lines) == 1:
            lines.append("Нет предстоящих тренировок")
        send_telegram("\n".join(lines), chat_id)
    except Exception as e:
        print(f"[ERROR] Trainings list: {e}")
        send_telegram("Ошибка загрузки тренировок", chat_id)

# ========== MAIN ==========
_running = True
_watches = []

def signal_handler(signum, frame):
    global _running
    print("\n[BOT] Получен сигнал остановки...")
    _running = False

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    db = init_firebase()
    listen_player_names(db)
    
    # Pre-load existing docs to avoid spam on restart
    print("[INIT] Загрузка существующих документов...")
    
    games_sent = load_sent_ids(SENT_GAMES_FILE)
    for doc in db.collection("padel_games").stream():
        d = doc.to_dict()
        if d.get("status") == "finished":
            games_sent.add(doc.id)
    save_sent_ids(SENT_GAMES_FILE, games_sent)
    print(f"[INIT] games: загружено, в истории {len(games_sent)}")
    
    tournaments_sent = load_sent_ids(SENT_TOURNAMENTS_FILE)
    for doc in db.collection("padel_tournaments").stream():
        d = doc.to_dict()
        if d.get("status") == "finished":
            tournaments_sent.add(doc.id)
    save_sent_ids(SENT_TOURNAMENTS_FILE, tournaments_sent)
    print(f"[INIT] tournaments: загружено, в истории {len(tournaments_sent)}")
    
    trainings_sent = load_sent_ids(SENT_TRAININGS_FILE)
    for doc in db.collection("padel_trainings").stream():
        trainings_sent.add(doc.id)
    save_sent_ids(SENT_TRAININGS_FILE, trainings_sent)
    print(f"[INIT] trainings: загружено, в истории {len(trainings_sent)}")
    
    users_sent = load_sent_ids(SENT_USERS_FILE)
    for doc in db.collection("users").stream():
        users_sent.add(doc.id)
    save_sent_ids(SENT_USERS_FILE, users_sent)
    print(f"[INIT] users: загружено {len(users_sent)} существующих")
    
    # Start listeners
    _watches.append(db.collection("padel_games").on_snapshot(on_games_snapshot))
    _watches.append(db.collection("padel_tournaments").on_snapshot(on_tournaments_snapshot))
    _watches.append(db.collection("padel_trainings").on_snapshot(on_trainings_snapshot))
    _watches.append(db.collection("users").on_snapshot(on_users_snapshot))
    
    # Training application & booking listeners
    app_listener = TrainingApplicationListener()
    booking_listener = TrainingBookingListener()
    _watches.append(db.collection("padel_trainings").on_snapshot(app_listener.on_snapshot))
    _watches.append(db.collection("padel_trainings").on_snapshot(booking_listener.on_snapshot))
    
    print("[OK] Все слушатели запущены")
    print("[OK] Polling команд запущен")
    
    # Run polling in main thread
    poll_commands()
    
    # Cleanup
    for w in _watches:
        w.unsubscribe()
    print("[BOT] Остановлен.")
