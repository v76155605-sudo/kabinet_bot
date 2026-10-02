#!/usr/bin/env python3
"""Бот уведомлений для kabinetavtora.com -> Telegram.
Запускается GitHub Actions. Пароль берётся только из переменных окружения (Secrets).
В логи ничего из текста сообщений не пишется (репозиторий публичный)."""
import html
import json
import os
import re
import sys
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

E = os.environ
TG_TOKEN = E.get("TG_TOKEN", "").strip()
TG_CHAT_ID = E.get("TG_CHAT_ID", "").strip()
LOGIN = E.get("SITE_LOGIN", "")
PASSWORD = E.get("SITE_PASSWORD", "")
SITE_URL = E.get("SITE_URL", "").strip()
LOGIN_URL = E.get("LOGIN_URL", "").strip() or SITE_URL
ORDERS_URL = E.get("ORDERS_URL", "").strip()
MESSAGES_URL = E.get("MESSAGES_URL", "").strip() or ORDERS_URL

STATE_FILE = "state.json"
ID_RE = re.compile(r"id-\d+-\d+")
DATE_RE = re.compile(r"^\d{2}/\d{2}\s+\d{2}:\d{2}$")
UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Mobile Safari/537.36")


class BotError(Exception):
    pass


# ---------- утилиты ----------
def mask(text):
    for secret in (PASSWORD, TG_TOKEN):
        if secret:
            text = text.replace(secret, "***")
    return text


def snippet(soup, n=300):
    return mask(soup.get_text(" ", strip=True)[:n])


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


def tg(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT_ID, "text": text[:3500],
                  "parse_mode": "HTML", "disable_web_page_preview": "true"},
            timeout=30,
        )
    except requests.RequestException as e:
        raise BotError(f"TELEGRAM: сеть недоступна ({type(e).__name__})")
    if r.status_code != 200:
        raise BotError(
            f"TELEGRAM: ошибка {r.status_code}: {mask(r.text[:200])}. "
            "Проверьте TG_TOKEN и TG_CHAT_ID и что вы нажали /start у своего бота.")


# ---------- сайт ----------
def get_soup(s, url):
    r = s.get(url, timeout=30)
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r, BeautifulSoup(r.text, "html.parser")


def find_login_form(soup):
    for f in soup.find_all("form"):
        if f.find("input", attrs={"type": re.compile(r"^password$", re.I)}):
            return f
    return None


def login(s):
    r, soup = get_soup(s, LOGIN_URL)
    form = find_login_form(soup)
    if not form:
        raise BotError(
            f"ВХОД: на странице {LOGIN_URL} не найдена форма с полем пароля "
            f"(HTTP {r.status_code}). Начало страницы: {snippet(soup)}")
    data, user_field, submit = {}, None, None
    for inp in form.find_all(["input", "select", "textarea"]):
        name = inp.get("name")
        if not name:
            continue
        t = (inp.get("type") or "text").lower()
        if t in ("submit", "button", "image"):
            if submit is None and t == "submit":
                submit = (name, inp.get("value", ""))
            continue
        if t in ("file", "reset"):
            continue
        if t in ("checkbox", "radio"):
            if inp.has_attr("checked"):
                data[name] = inp.get("value", "on")
            continue
        if t == "password":
            data[name] = PASSWORD
        elif t == "hidden":
            data[name] = inp.get("value", "")
        elif t in ("text", "email", "tel") and user_field is None:
            user_field = name
            data[name] = LOGIN
        else:
            data[name] = inp.get("value", "")
    if not user_field:
        raise BotError("ВХОД: в форме не найдено поле для логина "
                       f"(поля: {', '.join(data.keys())}).")
    if submit:
        data[submit[0]] = submit[1]
    action = urljoin(r.url, form.get("action") or r.url)
    method = (form.get("method") or "post").lower()
    try:
        if method == "get":
            r2 = s.get(action, params=data, timeout=30)
        else:
            r2 = s.post(action, data=data, timeout=30)
    except requests.RequestException as e:
        raise BotError(f"ВХОД: ошибка сети при отправке формы: {mask(str(e))[:200]}")
    if not r2.encoding or r2.encoding.lower() == "iso-8859-1":
        r2.encoding = r2.apparent_encoding
    soup2 = BeautifulSoup(r2.text, "html.parser")
    if find_login_form(soup2):
        raise BotError(
            f"ВХОД: после отправки формы снова показана форма входа (HTTP {r2.status_code}, "
            f"поле логина '{user_field}', метод {method.upper()}, адрес {action}). "
            f"Возможно неверный логин/пароль, капча или другая защита. Текст страницы: {snippet(soup2)}")


def fetch(s, url):
    r, soup = get_soup(s, url)
    if find_login_form(soup):
        login(s)
        r, soup = get_soup(s, url)
        if find_login_form(soup):
            raise BotError(f"ВХОД: вход выполнен, но {url} снова требует авторизацию. "
                           f"Текст страницы: {snippet(soup)}")
    if r.status_code >= 400:
        raise BotError(f"САЙТ: {url} вернул HTTP {r.status_code}. {snippet(soup, 200)}")
    return r.url, soup


# ---------- разбор страниц ----------
def parse_orders(soup, base):
    orders = {}
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        cells = [td.get_text(" ", strip=True) for td in tds]
        m = ID_RE.fullmatch(cells[0])
        if not m:
            continue
        a = tr.find("a", href=True)
        orders[m.group(0)] = {
            "deadline": cells[1] if len(cells) > 1 else "",
            "subject": cells[2] if len(cells) > 2 else "",
            "kind": cells[3] if len(cells) > 3 else "",
            "topic": cells[4] if len(cells) > 4 else "",
            "link": urljoin(base, a["href"]) if a else base,
        }
    return orders


def filter_problem(soup, n_found):
    """Возвращает текст предупреждения, если таблица показывает не все заказы."""
    text = soup.get_text(" ", strip=True)
    total = None
    m = re.search(r"всі замовлення\s*\((\d+)\s*/\s*(\d+)\)", text)
    if m:
        total = int(m.group(2))
    else:
        m = re.search(r"з\s+усіх\s+спеціальностей\s*\((\d+)\)", text)
        if m:
            total = int(m.group(1))
    if total is not None and n_found < total:
        hint = ""
        el = soup.find(string=re.compile(r"з\s+усіх\s+спеціальностей"))
        if el is not None and el.parent is not None:
            node = el.parent
            for _ in range(3):
                attrs = {k: v for k, v in node.attrs.items()
                         if k in ("href", "onclick") or str(k).startswith("data-")}
                if attrs:
                    hint = f" Кнопка «з усіх спеціальностей»: {mask(str(attrs))[:200]}"
                    break
                if node.parent is None:
                    break
                node = node.parent
        return (f"⚠️ Страница ORDERS_URL показывает {n_found} из {total} заказов — "
                "включён фильтр (например «з ваших спеціальностей» или скрыты «Приховані»). "
                "Откройте нужный вид вручную и пришлите мне адрес из строки браузера." + hint)
    return None


def parse_messages(soup, base):
    links = {}
    for a in soup.find_all("a", href=True):
        m = ID_RE.search(a.get_text())
        if m:
            links.setdefault(m.group(0), urljoin(base, a["href"]))
    items = []
    # 1) строки таблицы: дата + текст
    for tr in soup.find_all("tr"):
        texts = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        di = next((i for i, t in enumerate(texts) if DATE_RE.match(t)), None)
        if di is not None and di + 1 < len(texts) and texts[di + 1]:
            items.append((texts[di], texts[di + 1]))
    # 2) просто строки текста: дата, следующая строка = текст
    if not items:
        lines = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]
        for i, l in enumerate(lines[:-1]):
            if DATE_RE.match(l):
                items.append((l, lines[i + 1]))
    # 3) запасной вариант: любые строки со словом «менеджер»
    mode = "dated"
    if not items:
        mode = "generic"
        lines = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]
        items = [("", l) for l in lines if "менеджер" in l.lower() and len(l) < 300]
    return items, links, mode


def classify(text):
    t = text.lower()
    if "звернути увагу" in t or "обратить внимание" in t:
        return "📌 Менеджер просит обратить внимание на проект"
    if "оцен" in t:
        return "💬 Сообщение менеджера в оценённом вами задании"
    if "в работу" in t or "взят" in t:
        return "💬 Сообщение менеджера в задании, которое вы выполняете"
    return "🔔 Новое сообщение"


# ---------- основной код ----------
def main():
    pairs = [("TG_TOKEN", TG_TOKEN), ("TG_CHAT_ID", TG_CHAT_ID),
             ("SITE_LOGIN", LOGIN), ("SITE_PASSWORD", PASSWORD),
             ("LOGIN_URL", LOGIN_URL), ("ORDERS_URL", ORDERS_URL)]
    missing = [n for n, v in pairs if not v]
    if missing:
        print("Не заданы секреты: " + ", ".join(missing))
        sys.exit(1)

    st = load_state()
    first = not st.get("init")
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "uk,ru;q=0.8,en;q=0.5"})

    try:
        try:
            o_url, o_soup = fetch(s, ORDERS_URL)
            if MESSAGES_URL == ORDERS_URL:
                m_url, m_soup = o_url, o_soup
            else:
                m_url, m_soup = fetch(s, MESSAGES_URL)
        except requests.RequestException as e:
            st["fails"] = st.get("fails", 0) + 1
            print(f"Сеть: {mask(str(e))[:200]} (подряд ошибок: {st['fails']})")
            if st["fails"] == 3:
                tg("⚠️ Сайт кабинета недоступен 3 проверки подряд.")
            return

        st["fails"] = 0
        st["last_error"] = None

        orders = parse_orders(o_soup, o_url)
        items, links, mode = parse_messages(m_soup, m_url)
        print(f"Найдено заданий: {len(orders)}; сообщений: {len(items)} (режим: {mode})")

        # предупреждения (один раз на каждый текст)
        warns = []
        fp = filter_problem(o_soup, len(orders))
        if fp:
            warns.append(fp)
        if not orders:
            warns.append("⚠️ В таблице заданий не найдено ни одного ID. Проверьте ORDERS_URL.")
        if not items:
            warns.append("⚠️ Сообщения Центра звернень не найдены на MESSAGES_URL. "
                         "Скорее всего это всплывающее окно с отдельным адресом — пришлите мне адрес.")
        if mode == "generic" and items:
            warns.append("ℹ️ Не нашёл дат у сообщений, работаю по тексту — возможны лишние уведомления.")
        sent_w = st.setdefault("warned", [])
        for w in warns:
            if w not in sent_w:
                tg(w)
                sent_w.append(w)
        st["warned"] = [w for w in sent_w if w in warns]  # сбросить, если проблема ушла

        seen_o = set(st.get("orders", []))
        seen_m = set(st.get("msgs", []))
        keys = [f"{d}|{t}" for d, t in items]

        if first:
            tg(f"✅ Бот запущен. Запомнил заданий: {len(orders)}, сообщений: {len(items)}. "
               "Дальше буду писать только о новом.")
            st["orders"] = list(orders)
            st["msgs"] = keys
            st["init"] = True
            return

        # новые задания
        for oid, o in orders.items():
            if oid in seen_o:
                continue
            body = (f"🆕 <b>Новое задание {html.escape(oid)}</b>\n"
                    f"Срок: {html.escape(o['deadline'])}\n"
                    f"Предмет: {html.escape(o['subject'])}\n"
                    f"Вид: {html.escape(o['kind'])}\n"
                    f"Тема: {html.escape(o['topic'])}\n"
                    f"{html.escape(o['link'])}")
            tg(body)
            seen_o.add(oid)
            st["orders"] = list(seen_o)

        # новые сообщения (от старых к новым: на сайте новые сверху)
        new = [(k, d, t) for k, (d, t) in zip(keys, items) if k not in seen_m]
        for k, d, t in reversed(new):
            m = ID_RE.search(t)
            link = links.get(m.group(0), m_url) if m else m_url
            body = (f"<b>{classify(t)}</b>\n{html.escape(d)}\n"
                    f"{html.escape(t)}\n{html.escape(link)}")
            tg(body)
            seen_m.add(k)
            st["msgs"] = list(seen_m)[-1000:]
        print(f"Новых заданий/сообщений отправлено: {len(new)}")

    except BotError as e:
        msg = str(e)
        print(msg)
        if st.get("last_error") != msg and not msg.startswith("TELEGRAM"):
            try:
                tg("❌ Ошибка бота:\n" + html.escape(msg))
            except BotError:
                pass
        st["last_error"] = msg
        save_state(st)
        sys.exit(1)
    finally:
        save_state(st)


if __name__ == "__main__":
    main()
