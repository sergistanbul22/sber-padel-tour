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
from firebase_admin import credentials, firestore, auth

# ========== ПРОВЕРКА НА ДУБЛИ ==========
_skip_single_instance = False

def check_single_instance():
    """Проверяет, что бот запущен только в одном экземпляре"""
    if _skip_single_instance:
        return
    time.sleep(1)
    current_pid = os.getpid()
    parent_pid = os.getppid()
    other_pids = []
    for pid_str in os.listdir('/proc'):
        if not pid_str.isdigit():
            continue
        pid = int(pid_str)
        if pid in (current_pid, parent_pid):
            continue
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                cmdline = f.read().decode('utf-8', 'ignore').replace('\x00', ' ')
        except Exception:
            continue
        # Только реальный интерпретатор python с этим скриптом
        # (исключаем bash-обёртки, py_compile и т.п.)
        if 'tournament_bot.py' not in cmdline:
            continue
        if '-m py_compile' in cmdline or 'pgrep' in cmdline:
            continue
        exe = os.path.basename(os.readlink(f'/proc/{pid}/exe')) if os.path.exists(f'/proc/{pid}/exe') else ''
        if exe.startswith('python'):
            other_pids.append(pid)
    if other_pids:
        print(f"[ERROR] Бот уже запущен (PID: {', '.join(map(str, other_pids))}). Завершение.")
        sys.exit(1)

if '--force' not in sys.argv:
    check_single_instance()
else:
    _skip_single_instance = True
    print("[INFO] Запуск с --force, пропускаем проверку на дубли.")

# ========== НАСТРОЙКИ ==========
TELEGRAM_BOT_TOKEN = "8763865911:AAF9NsWJsV-ppuorU-HDGRnyQPzjJFDntFs"
CHAT_ID_SBER_PADEL = "-1002556296907"
CHAT_ID_PRO = "-4794823132"
ADMIN_CHAT_ID = "228493828"  # Личные сообщения администратору
ALL_CHAT_IDS = [CHAT_ID_SBER_PADEL, CHAT_ID_PRO]
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TRAINING_BOOKING_CHAT_ID = "-1002556296907"  # Группа SBER-PADEL для уведомлений о записях на открытые тренировки
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
_player_mu_cache = {}
_player_sigma_cache = {}
_players_watch = None

def listen_player_names(db):
    global _players_watch
    def on_players_snapshot(col_snapshot, changes, read_time):
        for change in changes:
            if change.type.name in ('ADDED', 'MODIFIED'):
                d = change.document.to_dict()
                _player_names_cache[change.document.id] = d.get("name", change.document.id)
                _player_ratings_cache[change.document.id] = d.get("rating", 0)
                _player_mu_cache[change.document.id] = d.get("mu", 0)
                _player_sigma_cache[change.document.id] = d.get("sigma", 0)
            elif change.type.name == 'REMOVED':
                _player_names_cache.pop(change.document.id, None)
                _player_ratings_cache.pop(change.document.id, None)
                _player_mu_cache.pop(change.document.id, None)
                _player_sigma_cache.pop(change.document.id, None)
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

def get_player_mu(pid):
    if not pid:
        return 0
    mu = _player_mu_cache.get(pid)
    if mu is not None:
        return mu
    try:
        doc = db.collection("padel_players").document(pid).get()
        if doc.exists:
            m = doc.to_dict().get("mu", 0)
            _player_mu_cache[pid] = m
            return m
    except Exception as e:
        print(f"[WARN] get_player_mu fallback failed for {pid}: {e}")
    return 0

def get_player_sigma(pid):
    if not pid:
        return 0
    sigma = _player_sigma_cache.get(pid)
    if sigma is not None:
        return sigma
    try:
        doc = db.collection("padel_players").document(pid).get()
        if doc.exists:
            s = doc.to_dict().get("sigma", 0)
            _player_sigma_cache[pid] = s
            return s
    except Exception as e:
        print(f"[WARN] get_player_sigma fallback failed for {pid}: {e}")
    return 0

def _compute_rating(mu, sigma):
    """Calculate display rating from mu and sigma (same formula as website)."""
    return max(0, round((mu - 3 * sigma) * 10))

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

# ========== BOT MODE (production / test) ==========
BOT_MODE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_mode.json")
TEST_PREFIX = "🧪 <b>[ТЕСТ-РЕЖИМ]</b>\n\n"


def get_bot_mode():
    """Текущий режим бота: 'production' (в группы) или 'test' (всё админу в ЛС)."""
    try:
        with open(BOT_MODE_FILE, "r", encoding="utf-8") as f:
            m = json.load(f).get("mode", "production")
            return m if m in ("production", "test") else "production"
    except Exception:
        return "production"


# ========== TELEGRAM LINKS (username → chat_id) ==========
# Telegram API не умеет искать chat_id по username, поэтому бот сам строит
# маппинг из входящих сообщений и хранит его в Firestore (коллекция telegram_links).
# Admin SDK обходит rules; доступ сайта к коллекции не нужен.
_tg_links_cache = None

def load_tg_links(db):
    global _tg_links_cache
    if _tg_links_cache is None:
        _tg_links_cache = {}
        try:
            for doc in db.collection("telegram_links").stream():
                chat_id = doc.to_dict().get("chat_id")
                if chat_id:
                    _tg_links_cache[doc.id] = str(chat_id)
            print(f"[INFO] telegram_links загружено: {len(_tg_links_cache)}")
        except Exception as e:
            print(f"[WARN] telegram_links load: {e}")
    return _tg_links_cache

def remember_tg_user(db, username, chat_id):
    """Запоминает соответствие @username → chat_id (кэш + Firestore)."""
    if not username or not chat_id:
        return
    key = str(username).strip().lower()
    if not key:
        return
    links = load_tg_links(db)
    if str(links.get(key, "")) == str(chat_id):
        return
    links[key] = str(chat_id)
    try:
        db.collection("telegram_links").document(key).set({
            "chat_id": str(chat_id),
            "updatedAt": firestore.SERVER_TIMESTAMP,
        })
    except Exception as e:
        print(f"[WARN] telegram_links write ({key}): {e}")

def resolve_organizer_chat_id(db, training_data):
    """chat_id организатора тренировки: createdBy → users.telegram → telegram_links."""
    email = (training_data.get("createdBy") or "").strip().lower()
    if not email:
        return None
    try:
        user_doc = db.collection("users").document(email).get()
        if not user_doc.exists:
            return None
        tg = (user_doc.to_dict().get("telegram") or "").strip().lower()
        if not tg:
            return None
        return load_tg_links(db).get(tg)
    except Exception as e:
        print(f"[WARN] resolve organizer ({email}): {e}")
        return None

def notify_training_organizer(data, text, event_tag):
    """Дублирует уведомление о тренировке организатору в ЛС (если chat_id известен).

    Не дублирует, если организатор и так получает уведомление (админ/группа).
    В тестовом режиме бота копия уходит админу с пометкой.
    """
    try:
        db = init_firebase()
        org_chat = resolve_organizer_chat_id(db, data)
        if not org_chat:
            print(f"[EVENT] {event_tag}: у организатора нет telegram/chat_id — ЛС пропущено")
            return
        org_chat = str(org_chat)
        if org_chat.startswith('-'):
            print(f"[WARN] {event_tag}: chat_id организатора {org_chat} — групповой (битый маппинг), ЛС пропущено")
            return
        if org_chat in (str(ADMIN_CHAT_ID), str(TRAINING_BOOKING_CHAT_ID)):
            return
        if get_bot_mode() == "test":
            send_telegram(f"🧪 [test → организатору {org_chat}]\n\n{text}", ADMIN_CHAT_ID)
        else:
            send_telegram(text, org_chat)
        print(f"[EVENT] {event_tag} → организатор {org_chat}")
    except Exception as e:
        print(f"[WARN] notify organizer ({event_tag}): {e}")


def set_bot_mode(mode):
    with open(BOT_MODE_FILE, "w", encoding="utf-8") as f:
        json.dump({"mode": mode}, f, ensure_ascii=False)


# ========== TELEGRAM ==========
def send_telegram(text, chat_id):
    # В режиме тестирования всё, что адресовано группам, перенаправляется админу в ЛС
    if get_bot_mode() == "test" and str(chat_id) in [str(c) for c in ALL_CHAT_IDS]:
        chat_id = ADMIN_CHAT_ID
        text = TEST_PREFIX + text
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
    """Рассылка в обе группы (турниры)."""
    if get_bot_mode() == "test":
        send_telegram(TEST_PREFIX + text, ADMIN_CHAT_ID)
        return
    for cid in ALL_CHAT_IDS:
        send_telegram(text, cid)

def send_game_result(text):
    """Результаты отдельно взятых игр — только в SBER-PADEL (без Sber Padel Pro)."""
    if get_bot_mode() == "test":
        send_telegram(TEST_PREFIX + text, ADMIN_CHAT_ID)
        return
    send_telegram(text, CHAT_ID_SBER_PADEL)

# ========== FORMAT MESSAGES ==========
def format_game(data, doc_id):
    # Support both old format (team1Player1/2, team2Player1/2, score1/2)
    # and new format (pair1[], pair2[], sets[{score1, score2}])
    
    pair1 = data.get("pair1", [])
    pair2 = data.get("pair2", [])
    sets = data.get("sets", [])
    ts_changes = data.get("tsChanges", {})
    rating_changes = data.get("ratingChanges", {})
    has_ts_changes = bool(ts_changes)
    
    if pair1 and pair2:
        # New format
        # Determine winner by counting won sets
        pair1_wins = 0
        pair2_wins = 0
        raw_set_scores = []
        for s in sets:
            s1 = s.get("score1", 0)
            s2 = s.get("score2", 0)
            raw_set_scores.append((s1, s2))
            if s1 > s2:
                pair1_wins += 1
            elif s2 > s1:
                pair2_wins += 1
        
        # Winner first — swap teams AND scores together (same as website's getGameDisplayOrder)
        if pair1_wins >= pair2_wins:
            winners, losers = pair1, pair2
            set_scores = [f"{a}:{b}" for a, b in raw_set_scores]
        else:
            winners, losers = pair2, pair1
            set_scores = [f"{b}:{a}" for a, b in raw_set_scores]
        
        # Compute pre-game rating (тот, что был ДО этой игры)
        # Приоритет: авторитетный снимок из документа (писала функция в той же транзакции),
        # fallback — математика от живого кэша (legacy-документы).
        ratings_before_map = data.get("ratingsBefore", {}) or {}
        ratings_after_map = data.get("ratingsAfter", {}) or {}
        
        def _pre_game_rating(pid):
            snap = ratings_before_map.get(pid)
            if snap is not None and snap.get("rating") is not None:
                return snap["rating"]
            ts = ts_changes.get(pid, {})
            if ts:
                mu = get_player_mu(pid)
                sigma = get_player_sigma(pid)
                mu_before = mu - ts.get("muDelta", 0)
                sigma_before = sigma - ts.get("sigmaDelta", 0)
                return _compute_rating(mu_before, sigma_before)
            # Legacy fallback: use ratingChanges
            return get_player_rating(pid) - rating_changes.get(pid, 0)
        
        def fmt_player(pid):
            # В скобках — рейтинг ДО игры
            name = get_player_name(pid)
            return f"{name} ({_pre_game_rating(pid):.0f})"
        
        w1, w2 = fmt_player(winners[0]), fmt_player(winners[1])
        l1, l2 = fmt_player(losers[0]), fmt_player(losers[1])
        
        sum_win = sum(_pre_game_rating(pid) for pid in winners)
        sum_lose = sum(_pre_game_rating(pid) for pid in losers)
        
        score_line = " ".join(set_scores)
        
        # Rating changes lines — из снимков документа (fallback — кэш-математика)
        rating_lines = []
        for pid in winners + losers:
            before_snap = ratings_before_map.get(pid)
            after_snap = ratings_after_map.get(pid)
            if before_snap is not None and before_snap.get("rating") is not None:
                rating_before = before_snap["rating"]
                if after_snap is not None and after_snap.get("rating") is not None:
                    rating_after = after_snap["rating"]
                else:
                    rating_after = rating_before + rating_changes.get(pid, 0)
                change = rating_after - rating_before
            else:
                ts = ts_changes.get(pid, {})
                if ts:
                    mu = get_player_mu(pid)
                    sigma = get_player_sigma(pid)
                    mu_before = mu - ts.get("muDelta", 0)
                    sigma_before = sigma - ts.get("sigmaDelta", 0)
                    rating_before = _compute_rating(mu_before, sigma_before)
                    rating_after = get_player_rating(pid)
                    change = rating_after - rating_before
                else:
                    # Legacy fallback
                    rating_before = get_player_rating(pid) - rating_changes.get(pid, 0)
                    rating_after = get_player_rating(pid)
                    change = rating_changes.get(pid, 0)
            arrow = "📈" if change > 0 else "📉"
            rating_lines.append(f"{rating_before:.0f} {arrow} {rating_after:.0f} ({change:+.0f})")
        
        ratings_text = "\n".join(rating_lines)
        
        return (
            f"🎾 <b>Игра завершена!</b>\n\n"
            f"{w1} и {w2} |{sum_win:.0f}| VS {l1} и {l2} |{sum_lose:.0f}|\n"
            f"{score_line}\n\n"
            f"{ratings_text}\n\n"
            f"👉 <a href='https://sber-padel-tour.ru/'>Перейти на сайт</a>\n"
            f"🔎 <a href='https://t.me/padelradarru_bot'>Найти свободный корт</a>"
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
            f"👉 <a href='https://sber-padel-tour.ru/'>Перейти на сайт</a>\n"
            f"🔎 <a href='https://t.me/padelradarru_bot'>Найти свободный корт</a>"
        )

def _format_tournament_rating_changes(data):
    """Format rating changes for tournament participants."""
    ts_changes = data.get("tsChanges", {})
    rating_changes = data.get("ratingChanges", {})
    ratings_before_map = data.get("ratingsBefore", {}) or {}
    ratings_after_map = data.get("ratingsAfter", {}) or {}
    
    # Collect all player IDs from tournament
    player_ids = set()
    for pid in data.get("playerIds", []):
        if pid:
            player_ids.add(pid)
    for slot in data.get("slots", []):
        pid = slot.get("playerId")
        if pid:
            player_ids.add(pid)
    for entry in data.get("leaderboard", []):
        pid = entry.get("playerId")
        if pid:
            player_ids.add(pid)
    for pair in data.get("pairLeaderboard", []):
        for pid in pair.get("playerIds", []):
            if pid:
                player_ids.add(pid)
    
    if not player_ids:
        return None
    
    changes = []
    for pid in player_ids:
        before_snap = ratings_before_map.get(pid)
        after_snap = ratings_after_map.get(pid)
        if before_snap is not None and before_snap.get("rating") is not None:
            rating_before = before_snap["rating"]
            if after_snap is not None and after_snap.get("rating") is not None:
                rating_after = after_snap["rating"]
            else:
                rating_after = rating_before + rating_changes.get(pid, 0)
            change = rating_after - rating_before
        else:
            ts = ts_changes.get(pid, {})
            if ts:
                mu = get_player_mu(pid)
                sigma = get_player_sigma(pid)
                mu_before = mu - ts.get("muDelta", 0)
                sigma_before = sigma - ts.get("sigmaDelta", 0)
                rating_before = _compute_rating(mu_before, sigma_before)
                rating_after = get_player_rating(pid)
                change = rating_after - rating_before
            else:
                # Legacy fallback
                rating_before = get_player_rating(pid) - rating_changes.get(pid, 0)
                rating_after = get_player_rating(pid)
                change = rating_changes.get(pid, 0)
        
        changes.append({
            'name': get_player_name(pid),
            'before': rating_before,
            'after': rating_after,
            'change': change
        })
    
    if not changes:
        return None
    
    # Sort by absolute change (descending) — most significant first
    changes.sort(key=lambda x: abs(x['change']), reverse=True)
    
    lines = []
    for c in changes[:15]:  # Top 15 most significant
        arrow = "📈" if c['change'] > 0 else "📉"
        lines.append(f"{c['before']:.0f} {arrow} {c['after']:.0f} ({c['change']:+.0f}) — {c['name']}")
    
    return "\n".join(lines)

def _get_player_rating_change_str(pid, data):
    """Get rating change string for a single player, e.g. (+6) or (-3)."""
    ts_changes = data.get("tsChanges", {})
    rating_changes = data.get("ratingChanges", {})
    ratings_before_map = data.get("ratingsBefore", {}) or {}
    ratings_after_map = data.get("ratingsAfter", {}) or {}
    
    before_snap = ratings_before_map.get(pid)
    after_snap = ratings_after_map.get(pid)
    if before_snap is not None and before_snap.get("rating") is not None:
        rating_before = before_snap["rating"]
        if after_snap is not None and after_snap.get("rating") is not None:
            rating_after = after_snap["rating"]
        else:
            rating_after = rating_before + rating_changes.get(pid, 0)
        change = rating_after - rating_before
    else:
        ts = ts_changes.get(pid, {})
        if ts:
            mu = get_player_mu(pid)
            sigma = get_player_sigma(pid)
            mu_before = mu - ts.get("muDelta", 0)
            sigma_before = sigma - ts.get("sigmaDelta", 0)
            rating_before = _compute_rating(mu_before, sigma_before)
            rating_after = get_player_rating(pid)
            change = rating_after - rating_before
        else:
            change = rating_changes.get(pid, 0)
    
    if change == 0:
        return ""
    return f" ({change:+.0f})"

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
            # Rating changes for each player in pair
            rating_changes_str = ", ".join(
                _get_player_rating_change_str(pid, data).strip() or "0" for pid in pids
            )
            results_text += f"{medals[i]} <b>{pnames}</b> — Игры {w}-{l} | Очки {gf}-{ga} | Рейтинг {rating_changes_str}\n"
        
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
            rating_changes_str = ", ".join(
                _get_player_rating_change_str(pid, data).strip() or "0" for pid in pids
            )
            lines.append(f"{idx+1}. {pnames} — Игры {w}-{l} | Очки {gf}-{ga} | Разница {diff_str} | Рейтинг {rating_changes_str}")
        full_table = "\n".join(lines)
    else:
        leaderboard = data.get("leaderboard", [])
        for i, p in enumerate(leaderboard[:3]):
            pid = p.get("playerId", "???")
            pname = get_player_name(pid)
            points = p.get('points', 0)
            change_str = _get_player_rating_change_str(pid, data)
            results_text += f"{medals[i]} <b>{pname}</b> — {points} очков{change_str}\n"
        
        # Full table (top 10)
        lines = []
        for idx, p in enumerate(leaderboard[:10]):
            pid = p.get("playerId", "???")
            pname = get_player_name(pid)
            points = p.get('points', 0)
            change_str = _get_player_rating_change_str(pid, data)
            lines.append(f"{idx+1}. {pname} — {points} очков{change_str}")
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
        f"👉 <a href='https://sber-padel-tour.ru/'>Перейти на сайт</a>\n"
        f"🔎 <a href='https://t.me/padelradarru_bot'>Найти свободный корт</a>"
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
        f"<a href='https://sber-padel-tour.ru/?training={doc_id}'>https://sber-padel-tour.ru/?training={doc_id}</a>\n"
        f"🔎 <a href='https://t.me/padelradarru_bot'>Найти свободный корт</a>"
    )

def format_new_user(data, doc_id):
    first_name = data.get("firstName", "")
    last_name = data.get("lastName", "")
    name = f"{last_name or ''} {first_name or ''}".strip() or doc_id
    
    return (
        f"✅ Новая регистрация на Sber Padel Tour!\n\n"
        f"👤 {name}\n\n"
        f"👉 <a href='https://sber-padel-tour.ru/'>Перейти на сайт</a>\n"
        f"🔎 <a href='https://t.me/padelradarru_bot'>Найти свободный корт</a>"
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
                send_game_result(msg)
                new_ids.add(doc_id)
        elif change.type.name == "ADDED":
            doc_id = change.document.id
            data = change.document.to_dict()
            if data.get("status") == "finished" and doc_id not in sent_ids:
                print(f"[EVENT] Игра завершена (add): {doc_id}")
                msg = format_game(data, doc_id)
                send_game_result(msg)
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
                # Skip publishing if organizer chose not to publish
                if data.get("skipPublish") == True:
                    print(f"[EVENT] Турнир завершён (без публикации): {doc_id}")
                    new_ids.add(doc_id)  # Mark as sent so we don't check again
                    continue
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
                    "chat_id": ADMIN_CHAT_ID,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
            resp.raise_for_status()
            print(f"[EVENT] Заявка на тренировку {training_id} → ADMIN")
        except Exception as e:
            print(f"[ERROR] Не удалось отправить заявку {training_id}: {e}")

        notify_training_organizer(data, text, f"заявка {training_id}")


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

        notify_training_organizer(data, text, f"запись {training_id}")


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

# ========== ADMIN COMMANDS ==========
SUPER_ADMIN_IDS = ["228493828"]  # Telegram ID супер-админа

def handle_mode(chat_id, from_user, text):
    """Переключение режима бота: /mode, /mode test, /mode work (только супер-админ)."""
    user_id = str(from_user.get("id", ""))
    if user_id not in SUPER_ADMIN_IDS:
        send_telegram("❌ У вас нет прав для этой команды.", chat_id)
        return
    
    parts = text.split()
    if len(parts) == 1:
        cur = get_bot_mode()
        label = "🧪 тестирование (все уведомления вам в ЛС)" if cur == "test" else "✅ рабочий (уведомления в группы)"
        send_telegram(
            f"Текущий режим: <b>{label}</b>\n\n"
            "/mode test — режим тестирования\n"
            "/mode work — рабочий режим",
            chat_id
        )
        return
    
    arg = parts[1].lower()
    if arg in ("test", "тест"):
        set_bot_mode("test")
        send_telegram("🧪 Режим <b>тестирования</b> включён. Все уведомления будут приходить сюда, в ЛС.", chat_id)
        print(f"[ADMIN] {user_id} переключил бота в тест-режим")
    elif arg in ("work", "prod", "production", "рабочий"):
        set_bot_mode("production")
        send_telegram("✅ <b>Рабочий режим</b> включён. Уведомления уходят в группы.", chat_id)
        print(f"[ADMIN] {user_id} вернул бота в рабочий режим")
    else:
        send_telegram("❌ Неизвестный режим. Используйте: /mode test или /mode work", chat_id)


def handle_setpassword(chat_id, from_user, text):
    """Установка пароля пользователю (только для супер-админа)"""
    user_id = str(from_user.get("id", ""))
    
    if user_id not in SUPER_ADMIN_IDS:
        send_telegram("❌ У вас нет прав для этой команды.", chat_id)
        return
    
    # Формат: /setpassword email@domain.com новый_пароль
    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        send_telegram(
            "❌ Неверный формат.\n\n"
            "Использование:\n"
            "/setpassword email@sberpadel.local новый_пароль",
            chat_id
        )
        return
    
    auth_email = parts[1].strip().lower()
    new_password = parts[2].strip()
    
    if len(new_password) < 6:
        send_telegram("❌ Пароль должен быть не менее 6 символов.", chat_id)
        return
    
    try:
        db = init_firebase()
        
        # Находим пользователя в Firebase Auth
        try:
            user = auth.get_user_by_email(auth_email)
        except Exception as e:
            send_telegram(f"❌ Пользователь {auth_email} не найден в системе.", chat_id)
            return
        
        # Устанавливаем новый пароль
        auth.update_user(user.uid, password=new_password)
        
        # Отправляем уведомление пользователю (если есть recoveryEmail)
        user_doc = db.collection("users").document(auth_email).get()
        if user_doc.exists:
            user_data = user_doc.to_dict()
            recovery_email = user_data.get("recoveryEmail", "")
            first_name = user_data.get("firstName", "")
            if recovery_email:
                # Можно отправить email, но пока просто уведомляем админа
                pass
        
        send_telegram(
            f"✅ Пароль успешно изменён!\n\n"
            f"👤 Пользователь: {auth_email}\n"
            f"🔑 Новый пароль: <code>{new_password}</code>\n\n"
            f"Сообщите пароль пользователю.",
            chat_id
        )
        print(f"[ADMIN] {user_id} установил пароль для {auth_email}")
        
    except Exception as e:
        send_telegram(f"❌ Ошибка: {e}", chat_id)
        print(f"[ERROR] setpassword: {e}")


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
                from_user = msg.get("from", {})

                # Запоминаем @username → chat_id для уведомлений организаторам тренировок.
                # ТОЛЬКО личные чаты: сообщения из групп не дают персонального chat_id.
                chat_type = msg.get("chat", {}).get("type", "")
                if from_user.get("username") and chat_id and chat_type == "private":
                    try:
                        remember_tg_user(init_firebase(), from_user.get("username"), chat_id)
                    except Exception as e:
                        print(f"[WARN] telegram_links upsert: {e}")
                
                
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
                        "/rating — рейтинг игроков\n"
                        "/mode — режим бота (тест/рабочий, только админ)\n\n"
                        "ℹ️ Организаторы тренировок получают уведомления о новых записях в ЛС, "
                        "если в профиле на сайте указан @username и вы хоть раз написали боту.",
                        chat_id
                    )
                elif text == "/mode" or text.startswith("/mode "):
                    handle_mode(chat_id, msg.get("from", {}), text)
                elif text.startswith("/setpassword "):
                    handle_setpassword(chat_id, msg.get("from", {}), text)
                    
        except requests.exceptions.ReadTimeout:
            continue
        except Exception as e:
            print(f"[WARN] Ошибка polling: {e}")
            time.sleep(5)

def send_trainings_list(chat_id):
    try:
        db = init_firebase()
        # Only future trainings, ordered by date
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        docs = db.collection("padel_trainings").where("date", ">=", today).order_by("date").limit(20).stream()
        
        lines = ["🎾 <b>Ближайшие тренировки:</b>\n"]
        count = 0
        
        for doc in docs:
            d = doc.to_dict()
            date = d.get("date", "")
            time_start = d.get("timeStart", "")
            time_end = d.get("timeEnd", "")
            location = d.get("location", "")
            group = d.get("group", "")
            coach = d.get("coach", "")
            training_type = d.get("trainingType", "open")
            total_slots = d.get("totalSlots", 4)
            slots = d.get("slots", []) or []
            applications = d.get("applications", []) or []
            
            # Count booked
            booked = len([s for s in slots if s.get("playerId")])
            
            # Count applications for closed trainings
            pending_apps = len([a for a in applications if a.get("playerId") and a.get("status") != "approved"])
            
            # Format date nicely
            try:
                from datetime import datetime as dt
                date_obj = dt.strptime(date, "%Y-%m-%d")
                weekdays = ['Пн','Вт','Ср','Чт','Пт','Сб','Вс']
                wd = weekdays[date_obj.weekday()]
                months = ['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря']
                date_str = f"{wd} {date_obj.day} {months[date_obj.month-1]}"
            except:
                date_str = date
            
            # Time
            time_str = ""
            if time_start and time_end:
                time_str = f"{time_start}–{time_end}"
            elif time_start:
                time_str = time_start
            
            # Type
            type_label = "🔒 Закрытая (по заявкам)" if training_type == "closed" else "🔓 Открытая"
            
            # Build line
            info = f"📅 {date_str}"
            if time_str:
                info += f" | ⏰ {time_str}"
            if location:
                info += f"\n📍 {location}"
            if group:
                info += f"\n👥 Группа: {group}"
            if coach:
                info += f"\n🎯 Тренер: {coach}"
            info += f"\n{type_label}"
            
            if training_type == "closed":
                info += f"\n👤 Мест: {total_slots} | Заявок: {len(applications)} | Подтверждено: {booked}"
                if pending_apps > 0:
                    info += f" | Ожидают: {pending_apps}"
            else:
                info += f"\n👤 Мест: {booked}/{total_slots}"
                if booked >= total_slots:
                    info += " ✅ Полностью набрана"
                else:
                    remaining = total_slots - booked
                    info += f" (осталось {remaining})"
            
            # Link
            info += f"\n👉 <a href='https://sber-padel-tour.ru/?training={doc.id}'>Записаться / Подать заявку</a>"
            
            lines.append(info + "\n")
            count += 1
        
        if count == 0:
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
    
    # Start heartbeat thread
    import threading
    def heartbeat_thread():
        db_hb = init_firebase()
        while _running:
            try:
                db_hb.collection("bot_status").document("status").set({
                    "lastHeartbeat": firestore.SERVER_TIMESTAMP,
                    "status": "running",
                    "pid": os.getpid()
                })
                print("[HEARTBEAT] OK")
            except Exception as e:
                print(f"[HEARTBEAT] Error: {e}")
            time.sleep(60)
    
    hb_thread = threading.Thread(target=heartbeat_thread, daemon=True)
    hb_thread.start()
    print("[OK] Heartbeat запущен")
    print("[OK] Polling команд запущен")
    
    # Run polling in main thread
    poll_commands()
    
    # Cleanup
    for w in _watches:
        w.unsubscribe()
    print("[BOT] Остановлен.")
