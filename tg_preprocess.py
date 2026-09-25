#!/usr/bin/env python3
"""Сжимает экспорт чата Telegram Desktop (result.json) в компактный текст для LLM.

    python tg_preprocess.py result.json [-o out.txt] [--reactions] [--max-tokens N] ...

Подробности — в README.md. Ядро (Converter/Options) не зависит от CLI, так что поверх него
можно построить GUI, не трогая логику.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

CHAT_KINDS = {
    "personal_chat": "личный чат",
    "bot_chat": "чат с ботом",
    "saved_messages": "избранное",
    "private_group": "группа",
    "public_group": "группа",
    "private_supergroup": "группа",
    "public_supergroup": "группа",
    "private_channel": "канал",
    "public_channel": "канал",
}

MEDIA_LABELS = {
    "animation": "GIF",
    "video_file": "Видео",
    "video_message": "Видеосообщение",
    "voice_message": "Голосовое",
}

SERVICE_ACTIONS = {
    "join_group_by_link": "вступил(а) в группу по ссылке",
    "create_group": "создал(а) группу",
    "create_channel": "создал(а) канал",
}

# Комбинирующие знаки «перечёркивания/наложения» (U+0335..U+0338), которыми украшают ники.
_OVERLAY = dict.fromkeys(range(0x0335, 0x0339))
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")


class ExportError(Exception):
    """Ошибка входных данных, о которой стоит сообщить пользователю без трейсбека."""


@dataclass
class Options:
    reactions: bool = False
    links: str = "domain"  # domain | keep | hide
    keep_service: bool = False
    media: bool = True
    quote_min: int = 30
    quote_max: int = 60
    quote_adjacent: bool = False
    merge_window: int = 5
    short_names: bool = False
    since: date | None = None
    until: date | None = None
    max_tokens: int = 0  # 0 — без разбивки
    header: bool = True


@dataclass
class Item:
    dt: datetime
    sender_key: str
    sender: str
    text: str
    service: bool = False


@dataclass
class Block:
    dt: datetime
    last_dt: datetime
    sender_key: str
    sender: str
    lines: list[str]
    service: bool = False

    def render(self) -> str:
        head = f"{self.sender} [{self.dt:%H:%M}]:"
        if self.service:
            return f"* {head} {self.lines[0]}"
        return f"{head} " + "\n".join(self.lines)


@dataclass
class Result:
    parts: list[str]
    messages_in: int
    messages_out: int
    blocks: int


# ---------------------------------------------------------------- вспомогательное

def estimate_tokens(s: str) -> int:
    """Грубая оценка: кириллица токенизируется заметно хуже латиницы."""
    cyr = len(_CYRILLIC.findall(s))
    return int(cyr / 2.3 + (len(s) - cyr) / 3.6) + 1


def clean_name(s: str | None) -> str:
    return " ".join((s or "").translate(_OVERLAY).split())


def surname_of(name: str) -> str:
    """Последнее «словесное» слово имени после первого (только буквы); '' — если фамилии нет."""
    for token in reversed(name.split()[1:]):
        core = "".join(ch for ch in token if ch.isalpha())
        if core:
            return core
    return ""


def fmt_duration(seconds) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def truncate_words(text: str, lo: int, hi: int) -> str:
    """Режет по границе слова так, чтобы длина оказалась в [lo, hi]; если пробела нет — жёстко по hi."""
    text = " ".join(text.split())
    if len(text) <= hi:
        return text
    cut = text.rfind(" ", lo, hi + 1)
    if cut == -1:
        cut = hi
    return text[:cut].rstrip(" ,;:—–-") + "…"


def tidy(s: str) -> str:
    s = s.replace("\r", "")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def domain_of(url: str) -> str:
    url = url.strip()
    host = urlparse(url if "//" in url else "//" + url).hostname or ""
    return host.removeprefix("www.")


def format_link(text: str, href: str | None, mode: str) -> str:
    plain = not href or href == text or re.match(r"(https?://|www\.)", text) is not None
    if mode == "keep":
        return text if plain else f"{text} ({href})"
    # domain — заглушка с доменом, hide — просто заглушка; место ссылки остаётся заметным в обоих режимах
    dom = domain_of(href or text) if mode == "domain" else ""
    if plain:
        return f"[ссылка: {dom}]" if dom else "[ссылка]"
    return f"{text} [{dom or 'ссылка'}]"


# ---------------------------------------------------------------- ядро

class Converter:
    def __init__(self, chat: dict, opts: Options):
        self.chat = chat
        self.o = opts
        self.msgs = [m for m in chat.get("messages", []) if isinstance(m, dict)]
        self.by_id = {m["id"]: m for m in self.msgs if "id" in m}
        self._full_names = self._collect_names()
        self.names = self._shorten(self._full_names)
        senders = {m.get("from_id") or m.get("from") for m in self.msgs if m.get("type") != "service"}
        self.many = len(senders - {None}) > 2

    def with_options(self, opts: Options) -> Converter:
        """Копия с другими опциями; индексы по чату (дорогие на больших выгрузках) переиспользуются."""
        clone = copy.copy(self)
        clone.o = opts
        clone.names = clone._shorten(self._full_names)
        return clone

    # --- имена

    def _collect_names(self) -> dict[str, str]:
        counts: dict[str, Counter] = {}
        for m in self.msgs:
            for id_key, name_key in (("from_id", "from"), ("actor_id", "actor")):
                if m.get(id_key) and m.get(name_key):
                    counts.setdefault(m[id_key], Counter())[clean_name(m[name_key])] += 1
        return {uid: c.most_common(1)[0][0] for uid, c in counts.items()}

    def _shorten(self, names: dict[str, str]) -> dict[str, str]:
        """Имя без фамилии; у тёзок — плюс кратчайший префикс фамилии, различающий их («Анна К.»)."""
        if not self.o.short_names:
            return names
        first = {uid: n.split()[0] for uid, n in names.items() if n}
        groups: dict[str, list[str]] = {}
        for uid, f in first.items():
            groups.setdefault(f.casefold(), []).append(uid)
        short: dict[str, str] = {}
        for uids in groups.values():
            if len(uids) == 1:
                short[uids[0]] = first[uids[0]]
                continue
            surnames = {uid: surname_of(names[uid]) for uid in uids}
            for k in range(1, max(map(len, surnames.values())) + 1):
                labels = {uid: f"{first[uid]} {surnames[uid][:k]}{'.' if len(surnames[uid]) > k else ''}"
                          if surnames[uid] else first[uid] for uid in uids}
                if len({v.rstrip(".").casefold() for v in labels.values()}) == len(uids):  # точка — не различие
                    short.update(labels)
                    break
            else:  # префиксом не развести (одинаковые фамилии) — оставляем полные имена
                short.update({uid: names[uid] for uid in uids})
        return short

    def name_of(self, uid: str | None, fallback: str | None) -> str:
        if uid and uid in self.names:
            return self.names[uid]
        return clean_name(fallback) or "?"

    def sender_of(self, m: dict) -> tuple[str, str]:
        if m.get("type") == "service":
            uid, name = m.get("actor_id"), m.get("actor")
        else:
            uid, name = m.get("from_id"), m.get("from")
        return uid or clean_name(name) or "?", self.name_of(uid, name)

    # --- текст сообщения

    def flatten(self, text) -> str:
        if isinstance(text, str):
            parts = [{"type": "plain", "text": text}]
        elif isinstance(text, list):
            parts = [{"type": "plain", "text": p} if isinstance(p, str) else p for p in text if p]
        else:
            return ""
        out = []
        for p in parts:
            t, s = p.get("type"), p.get("text", "")
            if t == "link":
                out.append(format_link(s, None, self.o.links))
            elif t == "text_link":
                out.append(format_link(s, p.get("href"), self.o.links))
            elif t == "mention_name":
                out.append("@" + s)
            elif t == "code":
                out.append(f"`{s}`")
            elif t == "pre":
                out.append(f"```{p.get('language', '')}\n{s}\n```")
            else:
                out.append(s)
        return "".join(out)

    def media_label(self, m: dict) -> str:
        kind = m.get("media_type")
        dur = fmt_duration(m.get("duration_seconds")) if kind in MEDIA_LABELS else ""
        if kind == "sticker":
            return f"[Стикер {m['sticker_emoji']}]" if m.get("sticker_emoji") else "[Стикер]"
        if kind in MEDIA_LABELS:
            return f"[{MEDIA_LABELS[kind]}{' ' + dur if dur else ''}]"
        if kind == "audio_file":
            title = " - ".join(x for x in (m.get("performer"), m.get("title")) if x) or m.get("file_name")
            return f"[Аудио: {title}]" if title else "[Аудио]"
        if "photo" in m:
            return "[Фото]"
        if "file" in m:
            return f"[Файл: {m['file_name']}]" if m.get("file_name") else "[Файл]"
        if "location_information" in m:
            return "[Геолокация]"
        if "contact_information" in m:
            info = m["contact_information"] or {}
            who = " ".join(x for x in (info.get("first_name"), info.get("last_name")) if x)
            return f"[Контакт: {who}]" if who else "[Контакт]"
        if kind:
            return f"[{kind}]"
        return ""

    @staticmethod
    def poll_label(poll: dict) -> str:
        answers = poll.get("answers") or []
        with_votes = any(a.get("voters") for a in answers)
        opts = " | ".join(
            a.get("text", "") + (f" ({a.get('voters', 0)})" if with_votes else "") for a in answers
        )
        return f"[Опрос «{poll.get('question', '')}»: {opts}]"

    def body(self, m: dict, media: bool | None = None, short_poll: bool = False) -> str:
        media = self.o.media if media is None else media
        parts = []
        if m.get("forwarded_from"):
            parts.append(f"[Переслано от {clean_name(m['forwarded_from'])}]")
        if media:
            label = self.media_label(m)
            if label:
                parts.append(label)
        if isinstance(m.get("poll"), dict):
            parts.append("[Опрос]" if short_poll else self.poll_label(m["poll"]))
        text = tidy(self.flatten(m.get("text")))
        if text:
            parts.append(text)
        return " ".join(parts)

    def call_label(self, m: dict) -> str:
        dur = fmt_duration(m.get("duration_seconds")) if m.get("duration_seconds") else ""
        return f"[Звонок {dur}]" if dur else "[Звонок без ответа]"

    def service_text(self, m: dict) -> str:
        action = m.get("action", "")
        if action == "pin_message":
            target = self.by_id.get(m.get("message_id"))
            quote = self.quote_text(target) if target else ""
            return f"закрепил(а) сообщение «{quote}»" if quote else "закрепил(а) сообщение"
        if action in ("invite_members", "remove_members"):
            verb = "добавил(а)" if action == "invite_members" else "удалил(а)"
            members = ", ".join(clean_name(x) for x in m.get("members", []) if x)
            return f"{verb} участников: {members}" if members else f"{verb} участников"
        if action == "edit_group_title":
            return f"сменил(а) название на «{m.get('title', '')}»"
        if action == "topic_created":
            return f"создал(а) тему «{m.get('title', '')}»"
        return SERVICE_ACTIONS.get(action, action or "служебное сообщение")

    # --- ответы и реакции

    def quote_text(self, target: dict) -> str:
        # только текст: пометки о пересылке/файле съедали бы лимит; они нужны, лишь когда текста нет
        flat = tidy(self.flatten(target.get("text"))) or self.body(target, media=True, short_poll=True)
        return truncate_words(flat, self.o.quote_min, self.o.quote_max)

    def reply_prefix(self, m: dict, prev_id, sender_key: str) -> str:
        rid = m.get("reply_to_message_id")
        if rid is None or (rid == prev_id and not self.o.quote_adjacent):
            return ""
        target = self.by_id.get(rid)
        if target is None:
            return "[↪ сообщение вне выгрузки]"
        if target.get("action") == "topic_created":
            return ""
        quote = self.quote_text(target)
        if not quote:
            return ""
        t_key, t_name = self.sender_of(target)
        if self.many or t_key == sender_key:
            return f"[↪ {t_name}: «{quote}»]"
        return f"[↪ «{quote}»]"

    def reactions_text(self, m: dict) -> str:
        out = []
        for r in m.get("reactions") or []:
            emoji = r.get("emoji") or {"custom_emoji": "[эмодзи]", "paid": "⭐"}.get(r.get("type"), "?")
            count = r.get("count") or 0
            recent = r.get("recent") or []
            if recent and len(recent) == count <= 3:
                who = ", ".join(self.name_of(x.get("from_id"), x.get("from")) for x in recent)
                out.append(f"{emoji} {who}")
            else:
                out.append(f"{emoji}×{count}" if count > 1 else emoji)
        return f"<{' | '.join(out)}>" if out else ""

    # --- сборка

    def in_range(self, dt: datetime) -> bool:
        return (not self.o.since or dt.date() >= self.o.since) and (not self.o.until or dt.date() <= self.o.until)

    def items(self, limit: int | None = None) -> list[Item]:
        items: list[Item] = []
        prev_id = None
        prev_reply: tuple | None = None
        consumed = 0
        for m in self.msgs:
            try:
                dt = datetime.fromisoformat(m["date"]).replace(second=0, microsecond=0)
            except (KeyError, TypeError, ValueError):
                continue
            if not self.in_range(dt):
                continue
            if limit is not None and consumed >= limit:
                break
            consumed += 1
            key, sender = self.sender_of(m)
            if m.get("type") == "service":
                if m.get("action") == "phone_call":
                    items.append(Item(dt, key, sender, self.call_label(m)))
                elif self.o.keep_service:
                    items.append(Item(dt, key, sender, self.service_text(m), service=True))
                else:
                    continue
                prev_id = m.get("id")
                continue
            body = self.body(m)
            if not body:
                continue
            rid = m.get("reply_to_message_id")
            repeat = rid is not None and (rid, key) == prev_reply  # тот же автор снова отвечает на то же сообщение
            prefix = "" if repeat else self.reply_prefix(m, prev_id, key)
            prev_reply = (rid, key)
            text = " ".join(x for x in (prefix, body) if x)
            if self.o.reactions:
                reactions = self.reactions_text(m)
                if reactions:
                    text += " " + reactions
            items.append(Item(dt, key, sender, text))
            prev_id = m.get("id")
        return items

    def blocks(self, items: list[Item]) -> list[Block]:
        window = timedelta(minutes=self.o.merge_window)
        blocks: list[Block] = []
        for it in items:
            last = blocks[-1] if blocks else None
            if (
                last and not last.service and not it.service
                and last.sender_key == it.sender_key
                and last.last_dt.date() == it.dt.date()
                and it.dt - last.last_dt <= window
            ):
                last.lines.append(it.text)
                last.last_dt = it.dt
            else:
                blocks.append(Block(it.dt, it.dt, it.sender_key, it.sender, [it.text], it.service))
        return blocks

    def header(self, blocks: list[Block], n_messages: int, part: tuple[int, int] | None) -> str:
        title = clean_name(self.chat.get("name")) or "без названия"
        kind = CHAT_KINDS.get(self.chat.get("type"), "чат")
        people = Counter()
        keys: dict[str, str] = {}
        for b in blocks:
            if not b.service:
                people[b.sender] += len(b.lines)
                keys.setdefault(b.sender, b.sender_key)

        def legend(label: str) -> str:  # при сокращённых именах — «Полное имя (как в тексте)»
            full = self._full_names.get(keys[label], label)
            return label if full == label else f"{full} ({label})"

        listed = [legend(n) for n, _ in people.most_common(30)]
        extra = f" и ещё {len(people) - 30}" if len(people) > 30 else ""
        first = f"Чат «{title}» — {kind}"
        if part:
            first += f" (часть {part[0]}/{part[1]})"
        elif self.many:
            first += f", участников: {len(people)}"
        span = f"{blocks[0].dt:%Y-%m-%d} — {blocks[-1].dt:%Y-%m-%d}" if blocks else "—"
        return "\n".join((
            first,
            f"Период: {span}, сообщений: {n_messages}",
            f"Участники: {', '.join(listed)}{extra}",
        ))

    @staticmethod
    def day_header(d: date, continued: bool = False) -> str:
        return f"=== {d:%Y-%m-%d} ({WEEKDAYS[d.weekday()]}){', продолжение' if continued else ''} ==="

    def chunk(self, blocks: list[Block]) -> list[list[Block]]:
        """Делит блоки на части по бюджету токенов, предпочитая границы дней."""
        limit = self.o.max_tokens
        if not limit:
            return [blocks]
        budget = limit * 0.9 - (estimate_tokens(self.header(blocks, 99999, (99, 99))) if self.o.header else 0)
        cost = [estimate_tokens(b.render()) + 1 for b in blocks]
        day_cost = Counter()
        for b, c in zip(blocks, cost):
            day_cost[b.dt.date()] += c
        parts: list[list[Block]] = []
        cur: list[Block] = []
        used = 0
        for b, c in zip(blocks, cost):
            d = b.dt.date()
            new_day = not cur or cur[-1].dt.date() != d
            need = c + (estimate_tokens(self.day_header(d, True)) + 1 if new_day else 0)
            if cur and new_day and used + day_cost[d] > budget and day_cost[d] <= budget:
                need = budget + 1  # день целиком не влезает в остаток — начинаем новую часть
            if cur and used + need > budget:
                parts.append(cur)
                cur, used = [], 0
                new_day = True
                need = c + estimate_tokens(self.day_header(d, True)) + 1
            cur.append(b)
            used += need
        if cur:
            parts.append(cur)
        return parts

    def render_part(self, blocks: list[Block], prev_last: Block | None, n_msgs: int,
                    part: tuple[int, int] | None) -> str:
        lines = []
        if self.o.header:
            lines += [self.header(blocks, n_msgs, part), ""]
        last_day = None
        for b in blocks:
            d = b.dt.date()
            if d != last_day:
                continued = last_day is None and prev_last is not None and prev_last.dt.date() == d
                lines.append(self.day_header(d, continued))
                last_day = d
            lines.append(b.render())
        return "\n".join(lines) + "\n"

    def convert(self, limit: int | None = None) -> Result:
        """limit — обработать только первые N сообщений (после фильтра по датам); для быстрого предпросмотра."""
        items = self.items(limit)
        blocks = self.blocks(items)
        if not blocks:
            return Result([self.render_part([], None, 0, None)], len(self.msgs), 0, 0)
        chunks = self.chunk(blocks)
        n = len(chunks)
        parts = []
        prev_last = None
        for i, part_blocks in enumerate(chunks, 1):
            n_msgs = sum(len(b.lines) for b in part_blocks if not b.service)
            parts.append(self.render_part(part_blocks, prev_last, n_msgs, (i, n) if n > 1 else None))
            prev_last = part_blocks[-1]
        return Result(parts, len(self.msgs), len(items), len(blocks))


# ---------------------------------------------------------------- ввод/вывод

def load_export(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except OSError as e:
        raise ExportError(f"не удалось открыть {path}: {e.strerror or e}") from e
    except json.JSONDecodeError as e:
        raise ExportError(f"{path} — не JSON: {e}") from e


def chats_of(data: dict) -> list[dict]:
    if isinstance(data.get("messages"), list):
        return [data]
    chats = (data.get("chats") or {}).get("list")
    if isinstance(chats, list):
        return chats
    raise ExportError("не похоже на экспорт Telegram: нет ни 'messages', ни 'chats.list'")


def describe_chats(chats: list[dict]) -> str:
    return "\n".join(
        f"{i}. {c.get('name') or '(без названия)'}  [{c.get('type', '?')}, id {c.get('id', '?')}, "
        f"сообщений: {len(c.get('messages', []))}]"
        for i, c in enumerate(chats, 1)
    )


def pick_chat(chats: list[dict], selector: str | None) -> dict:
    if selector is None:
        if len(chats) == 1:
            return chats[0]
        raise ExportError("в файле несколько чатов, выберите один через --chat:\n" + describe_chats(chats))
    if selector.isdigit() and 1 <= int(selector) <= len(chats):
        return chats[int(selector) - 1]
    found = [c for c in chats if str(c.get("id")) == selector]
    found = found or [c for c in chats if selector.lower() in (c.get("name") or "").lower()]
    if len(found) == 1:
        return found[0]
    problem = "не найден" if not found else "подходит несколько"
    raise ExportError(f"чат «{selector}» {problem}:\n" + describe_chats(found or chats))


def default_output(src: Path, chat: dict) -> Path:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", clean_name(chat.get("name"))).strip(" .")
    return src.with_name((name or src.stem) + ".txt")


def part_paths(base: Path, n: int) -> list[Path]:
    if n == 1:
        return [base]
    width = max(3, len(str(n)))
    return [base.with_name(f"{base.stem}_{i:0{width}d}{base.suffix}") for i in range(1, n + 1)]


def write_parts(base: Path, parts: list[str]) -> list[Path]:
    paths = part_paths(base, len(parts))
    for path, text in zip(paths, parts):
        path.write_text(text, encoding="utf-8", newline="\n")
    return paths


def stats_lines(result: Result, size_in: int) -> list[str]:
    size_out = sum(len(t) for t in result.parts)
    tokens = sum(estimate_tokens(t) for t in result.parts)
    return [
        f"Сообщений в чате: {result.messages_in}, в результате: {result.messages_out} (блоков: {result.blocks})",
        f"Размер: {size_in:,} байт JSON → {size_out:,} симв. (≈{tokens:,} токенов)".replace(",", " "),
    ]


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Сжимает экспорт чата Telegram (result.json) в компактный текст для LLM.")
    p.add_argument("input", type=Path, help="result.json из экспорта Telegram Desktop")
    p.add_argument("-o", "--output", help="файл результата ('-' — stdout); по умолчанию рядом с входным")
    p.add_argument("--chat", help="для экспорта нескольких чатов: номер, id или часть названия")
    p.add_argument("--list-chats", action="store_true", help="показать чаты из файла и выйти")
    p.add_argument("--reactions", action="store_true", help="добавлять реакции")
    p.add_argument("--links", choices=("domain", "keep", "hide"), default="domain",
                   help="ссылки: [ссылка: домен] (по умолчанию) / как есть / просто [ссылка]")
    p.add_argument("--keep-service", action="store_true", help="оставлять служебные сообщения")
    p.add_argument("--no-media", dest="media", action="store_false", help="не выводить пометки о медиа")
    p.add_argument("--merge-window", type=int, default=5, metavar="МИН",
                   help="склеивать сообщения автора в пределах N минут (по умолчанию 5)")
    p.add_argument("--quote-min", type=int, default=30, help="мин. длина цитаты ответа")
    p.add_argument("--quote-max", type=int, default=60, help="макс. длина цитаты ответа")
    p.add_argument("--quote-adjacent", action="store_true", help="цитировать и ответ на предыдущую строку")
    p.add_argument("--short-names", action="store_true", help="только имя без фамилии, если однозначно")
    p.add_argument("--since", type=date.fromisoformat, metavar="ДАТА", help="с даты (ГГГГ-ММ-ДД)")
    p.add_argument("--until", type=date.fromisoformat, metavar="ДАТА", help="по дату включительно")
    p.add_argument("--max-tokens", type=int, default=0, metavar="N", help="разбить на части ≈ по N токенов")
    p.add_argument("--no-header", dest="header", action="store_false", help="без шапки")
    args = p.parse_args(argv)
    if args.quote_min > args.quote_max:
        p.error("--quote-min не может быть больше --quote-max")
    if args.merge_window < 0 or args.max_tokens < 0:
        p.error("--merge-window и --max-tokens не могут быть отрицательными")
    if args.output == "-" and args.max_tokens:
        p.error("вывод в stdout несовместим с --max-tokens (нужно несколько файлов)")
    return args


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    try:
        data = load_export(args.input)
        chats = chats_of(data)
        if args.list_chats:
            print(describe_chats(chats))
            return 0
        chat = pick_chat(chats, args.chat)
    except ExportError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2

    opts = Options(**{f.name: getattr(args, f.name) for f in fields(Options)})
    result = Converter(chat, opts).convert()

    if args.output == "-":
        sys.stdout.write(result.parts[0])
        paths: list[Path] = []
    else:
        base = Path(args.output) if args.output else default_output(args.input, chat)
        paths = write_parts(base, result.parts)

    for line in stats_lines(result, args.input.stat().st_size):
        print(line, file=sys.stderr)
    for path in paths:
        print(f"Записано: {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
