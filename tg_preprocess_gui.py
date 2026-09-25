#!/usr/bin/env python3
"""Окно поверх tg_preprocess: выбрать result.json, подобрать параметры, посмотреть результат, сохранить или скопировать.

    python tg_preprocess_gui.py [result.json]

Предпросмотр обновляется при любой смене параметра; изменившиеся фрагменты на секунду подсвечиваются.
До выбора файла (и по переключателю) показывается вымышленный «пример» с типичными случаями.
"""
from __future__ import annotations

import difflib
import queue
import sys
import threading
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import tg_preprocess as core

LINK_MODES = {"Домен": "domain", "Как есть": "keep", "Убрать": "drop"}
PREVIEW_MESSAGES = 500  # сколько первых сообщений реального чата идёт в предпросмотр
PREVIEW_LIMIT = 300_000  # предохранитель по числу символов в виджете
PART_SEP = "\n" + "─" * 60 + "\n"  # граница частей при --max-tokens
DEBOUNCE_MS = 250  # пауза после последней правки, прежде чем обновить предпросмотр
FULL_DEBOUNCE_MS = 600  # то же для пересчёта всего чата (сохранение, статистика)
FLASH_COLOR = (0xFF, 0xD5, 0x4F)
FLASH_STEPS = 10
FLASH_STEP_MS = 140
DIFF_CHAR_BUDGET = 200  # для скольких изменившихся строк считать посимвольное отличие
DIFF_LINE_MAX = 800  # строки длиннее подсвечиваются целиком


# ---------------------------------------------------------------- пример

def sample_chat() -> dict:
    """Вымышленный чат в формате экспорта Telegram, задевающий каждую опцию; обрабатывается тем же Converter."""
    people = {
        "u1": "Анна Смирнова",
        "u2": "Анна Ким",  # то же имя, что у u1: --short-names должен оставить фамилии
        "u3": "Борис Петров",
        "u4": "".join(c + "̶" for c in "Вика Сидорова"),  # ник с «зачёркиванием»
    }
    file = "(File not included. Change data exporting settings to download.)"
    msgs: list[dict] = []

    def add(day, hm, uid, text="", **extra):
        m = {"id": len(msgs) + 1, "type": "message", "date": f"2026-03-{day:02d}T{hm}:00",
             "from": people[uid], "from_id": uid, "text": text, **extra}
        msgs.append(m)
        return m["id"]

    def service(day, hm, uid, action, **extra):
        msgs.append({"id": len(msgs) + 1, "type": "service", "date": f"2026-03-{day:02d}T{hm}:00",
                     "actor": people[uid], "actor_id": uid, "action": action, "text": "", **extra})

    def react(emoji, *uids, count=None):
        recent = [{"from": people[u], "from_id": u} for u in uids]
        return {"type": "emoji", "emoji": emoji, "count": count or len(uids), "recent": recent}

    service(2, "08:50", "u4", "join_group_by_link", inviter="Group")
    m1 = add(2, "09:00", "u1", "Всем привет! Напоминаю про созвон в четверг.")
    m2 = add(2, "09:01", "u1", ["Материалы здесь: ", {"type": "link", "text":
                                "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit?usp=sharing"}])
    add(2, "09:03", "u1", ["Ещё ", {"type": "text_link", "text": "чек-лист",
                                    "href": "https://example.com/checklists/weekly"}, " для проверки"])
    m4 = add(2, "09:09", "u1", "Кто-нибудь уже смотрел?")
    add(2, "09:12", "u3", "Смотрел, всё в порядке", reply_to_message_id=m4, reactions=[react("👍", "u2")])
    add(2, "09:13", "u3", "Только в третьем пункте не хватает примера", reply_to_message_id=m2)
    add(2, "09:20", "u2", "", media_type="sticker", sticker_emoji="😀", file=file)
    add(2, "09:21", "u2", "Скриншот ошибки", photo=file, width=800, height=600)
    add(2, "09:25", "u4", "", media_type="voice_message", duration_seconds=47, file=file)
    add(2, "09:40", "u2", [{"type": "mention", "text": "@boris_p"}, " посмотришь? ",
                           {"type": "mention_name", "text": "Вика", "user_id": 4}, " тоже"])
    service(2, "09:45", "u1", "pin_message", message_id=m1)
    m12 = add(2, "10:05", "u3", "Итоги квартала — таблица во вложении. Обратите внимание на второй лист: "
              "там пересчитаны показатели по регионам с учётом новых тарифов",
              forwarded_from="Отдел продаж", file=file, file_name="Q1_итоги.xlsx",
              mime_type="application/vnd.ms-excel")
    add(2, "10:06", "u2", "Спасибо!")
    add(2, "10:07", "u1", "Посмотрю после обеда, отпишусь", reply_to_message_id=m12)
    add(2, "10:30", "u3", "Голосуем!", reactions=[react("🔥", "u1", "u2")],
        poll={"question": "Во сколько созвон в четверг?", "closed": False, "total_voters": 9,
              "answers": [{"text": "10:00", "voters": 3, "chosen": False},
                          {"text": "15:00", "voters": 5, "chosen": False},
                          {"text": "17:30", "voters": 1, "chosen": False}]})
    add(2, "10:35", "u2", ["Запустите ", {"type": "code", "text": "make build"}, " и приложите лог:\n",
                           {"type": "pre", "text": "ERROR: no rule to make target 'all'\n\n\nmake: *** [build] Error 2",
                            "language": ""}])
    service(2, "11:00", "u3", "phone_call", duration_seconds=312, discard_reason="hangup")

    m_day3 = add(3, "12:00", "u1", "Итоги созвона:\n\n- релиз в пятницу\n- тесты до среды\n- документация после релиза")
    m_agree = add(3, "12:05", "u2", "Согласна!", reply_to_message_id=9999, reactions=[
        react("🔥", "u1", "u3", "u4", count=5),
        {"type": "custom_emoji", "count": 1, "document_id": "", "recent": [{"from": people["u1"], "from_id": "u1"}]}])
    add(3, "12:10", "u3", "Тоже за", reply_to_message_id=m_agree)
    add(3, "12:30", "u4", "Вопрос по релизу: кто отвечает за выкладку?")
    add(3, "12:40", "u3", "Я могу выложить, если нужно")
    add(3, "13:00", "u1", "Выкладкой займусь сама, спасибо", reply_to_message_id=m_day3)

    add(5, "10:00", "u2", "", media_type="video_message", duration_seconds=12, file=file)
    add(5, "10:01", "u2", "Начинайте без меня, подключусь через пять минут")
    add(5, "10:02", "u1", "Созвон начинаем", reactions=[react("👍", "u3", "u4")])
    service(5, "10:50", "u1", "edit_group_title", title="Проект «Альфа»")
    add(5, "10:55", "u1", "Всем спасибо, встретимся в понедельник!")
    return {"name": "Пример: рабочая группа", "type": "private_supergroup", "id": 0, "messages": msgs}


# ---------------------------------------------------------------- вспомогательное

class OptionError(ValueError):
    def __init__(self, message: str, *fields: str):
        super().__init__(message)
        self.fields = fields


class LatestJob:
    """Фоновая задача по принципу «побеждает последний запрос».

    Пока задача выполняется, новые запросы не копятся — остаётся только самый свежий, а результат
    устаревшего отбрасывается. Обратные вызовы выполняются в потоке интерфейса.
    """

    def __init__(self, root: tk.Tk, on_done, on_error):
        self.root, self.on_done, self.on_error = root, on_done, on_error
        self.results: queue.Queue = queue.Queue()
        self.running = False
        self.pending = None

    @property
    def busy(self) -> bool:
        return self.running or self.pending is not None

    def submit(self, work, tag=None) -> None:
        self.pending = (work, tag)
        if not self.running:
            self._start()

    def _start(self) -> None:
        (work, tag), self.pending = self.pending, None
        self.running = True

        def target():
            try:
                self.results.put((work(), tag, None))
            except Exception as e:  # noqa: BLE001 — любую ошибку показываем пользователю
                self.results.put((None, tag, e))

        threading.Thread(target=target, daemon=True).start()
        self.root.after(30, self._poll)

    def _poll(self) -> None:
        try:
            value, tag, error = self.results.get_nowait()
        except queue.Empty:
            self.root.after(30, self._poll)
            return
        self.running = False
        if self.pending is not None:
            self._start()  # пока считали, пришёл более свежий запрос — этот результат уже неактуален
        elif error is not None:
            self.on_error(error)
        else:
            self.on_done(value, tag)


def _char_ranges(old: str, new: str, lineno: int) -> list[tuple[int, int, int]]:
    if len(old) > DIFF_LINE_MAX or len(new) > DIFF_LINE_MAX:
        return [(lineno, 0, len(new))] if new else []
    out = []
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            out.append((lineno, j1, j2))
        elif tag == "delete":  # в новом тексте на месте удалённого ничего нет — отмечаем соседний символ
            if j1 < len(new):
                out.append((lineno, j1, j1 + 1))
            elif j1 > 0:
                out.append((lineno, j1 - 1, j1))
    return out


def diff_ranges(old: str, new: str) -> list[tuple[int, int, int]]:
    """Диапазоны (строка с 1, колонка с, колонка по) в new, которые отличаются от old.

    Полностью удалённые строки показать нечем и в результат не попадают.
    """
    a, b = old.split("\n"), new.split("\n")
    out: list[tuple[int, int, int]] = []
    budget = DIFF_CHAR_BUDGET
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("equal", "delete"):
            continue
        if tag == "replace" and i2 - i1 == j2 - j1 and budget > 0:
            for k in range(j2 - j1):
                budget -= 1
                out += _char_ranges(a[i1 + k], b[j1 + k], j1 + k + 1)
        else:
            out += [(j + 1, 0, len(b[j])) for j in range(j1, j2) if b[j]]
    return out


def tk_col(line: str, col: int) -> int:
    """Tk 8.6 считает символы вне BMP (эмодзи) за два — сдвигаем колонку на их число."""
    return col + sum(1 for ch in line[:col] if ord(ch) > 0xFFFF)


# ---------------------------------------------------------------- окно

class App:
    ENTRY_KEYS = ("since", "until", "merge", "qmin", "qmax", "tokens")

    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Telegram → LLM")
        root.minsize(760, 660)

        self.path: Path | None = None
        self.chats: list[dict] = []
        self.convs: dict[int, core.Converter] = {}
        self.sample = core.Converter(sample_chat(), core.Options())
        self.full: core.Result | None = None
        self.full_fresh = False
        self.version = 0  # растёт при каждой правке; пересчёт «свежий», только если версия не изменилась
        self.shown = ""
        self.shown_source = ""
        self.pending_cause: str | None = None
        self.timers: dict[str, str | None] = {"refresh": None, "full": None, "fade": None}
        self.preview_note = ""
        self.full_note = ""

        self.loader = LatestJob(root, self._loaded, self._load_failed)
        self.previewer = LatestJob(root, self._preview_ready, self._job_failed)
        self.fuller = LatestJob(root, self._full_ready, self._job_failed)

        self.v_path = tk.StringVar()
        self.v_chat = tk.StringVar()
        self.v_source = tk.StringVar(value="sample")
        self.v_reactions = tk.BooleanVar(value=False)
        self.v_service = tk.BooleanVar(value=False)
        self.v_media = tk.BooleanVar(value=True)
        self.v_short = tk.BooleanVar(value=False)
        self.v_header = tk.BooleanVar(value=True)
        self.v_adjacent = tk.BooleanVar(value=False)
        self.v_links = tk.StringVar(value="Домен")
        self.v_since = tk.StringVar()
        self.v_until = tk.StringVar()
        self.v_merge = tk.StringVar(value="5")
        self.v_qmin = tk.StringVar(value="30")
        self.v_qmax = tk.StringVar(value="60")
        self.v_tokens = tk.StringVar(value="0")
        self.v_part = tk.StringVar()
        self.v_status = tk.StringVar()
        self.v_error = tk.StringVar()
        self.entries: dict[str, ttk.Entry] = {}

        self._build()
        for var in (self.v_reactions, self.v_service, self.v_media, self.v_short, self.v_header, self.v_adjacent,
                    self.v_links, self.v_since, self.v_until, self.v_merge, self.v_qmin, self.v_qmax, self.v_tokens):
            var.trace_add("write", lambda *_: self.request("option"))
        self.request("reset")

    # ------------------------------------------------------------ интерфейс

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        ttk.Style().configure("Bad.TEntry", foreground="#c00000")

        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Файл экспорта").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(top, textvariable=self.v_path)
        entry.grid(row=0, column=1, sticky="ew", padx=6)
        entry.bind("<Return>", lambda _e: self.load(Path(self.v_path.get().strip())))
        ttk.Button(top, text="Обзор…", command=self.browse).grid(row=0, column=2)
        ttk.Label(top, text="Чат").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.c_chat = ttk.Combobox(top, textvariable=self.v_chat, state="disabled")
        self.c_chat.grid(row=1, column=1, columnspan=2, sticky="ew", padx=6, pady=(6, 0))
        self.c_chat.bind("<<ComboboxSelected>>", lambda _e: self._chat_changed())

        opts = ttk.LabelFrame(self.root, text="Параметры")
        opts.pack(fill="x", **pad)
        checks = ttk.Frame(opts)
        checks.grid(row=0, column=0, sticky="nw", padx=8, pady=6)
        for text, var in (
            ("Реакции", self.v_reactions),
            ("Служебные сообщения (вступления, закрепы…)", self.v_service),
            ("Пометки о медиа ([Фото], [Файл: …])", self.v_media),
            ("Короткие имена (без фамилии)", self.v_short),
            ("Шапка с названием и участниками", self.v_header),
            ("Цитировать ответ на предыдущую строку", self.v_adjacent),
        ):
            ttk.Checkbutton(checks, text=text, variable=var).pack(anchor="w", pady=1)

        fields_ = ttk.Frame(opts)
        fields_.grid(row=0, column=1, sticky="nw", padx=8, pady=6)
        opts.columnconfigure(1, weight=1)
        rows = (
            ("Ссылки", None, None),
            ("С даты (ГГГГ-ММ-ДД)", "since", self.v_since),
            ("По дату (включительно)", "until", self.v_until),
            ("Склейка сообщений, мин", "merge", self.v_merge),
            ("Цитата ответа: от, симв.", "qmin", self.v_qmin),
            ("Цитата ответа: до, симв.", "qmax", self.v_qmax),
            ("Токенов на часть (0 — без разбивки)", "tokens", self.v_tokens),
        )
        for i, (label, key, var) in enumerate(rows):
            ttk.Label(fields_, text=label).grid(row=i, column=0, sticky="w", pady=2)
            if key is None:
                w = ttk.Combobox(fields_, textvariable=self.v_links, values=list(LINK_MODES), state="readonly", width=12)
            else:
                w = self.entries[key] = ttk.Entry(fields_, textvariable=var, width=12)
            w.grid(row=i, column=1, sticky="w", padx=(10, 0), pady=2)

        self.l_error = ttk.Label(self.root, textvariable=self.v_error, foreground="#c00000", wraplength=740)
        self.bar = bar = ttk.Frame(self.root)
        bar.pack(fill="x", **pad)
        ttk.Label(bar, text="Предпросмотр:").pack(side="left")
        ttk.Radiobutton(bar, text="Пример", value="sample", variable=self.v_source,
                        command=lambda: self.request("reset")).pack(side="left", padx=(8, 0))
        self.rb_real = ttk.Radiobutton(bar, text="Ваш чат", value="real", variable=self.v_source,
                                       command=lambda: self.request("reset"), state="disabled")
        self.rb_real.pack(side="left", padx=(8, 0))
        self.b_copy = ttk.Button(bar, text="Копировать в буфер", command=self.copy)
        self.b_copy.pack(side="right")
        self.b_save = ttk.Button(bar, text="Сохранить…", command=self.save)
        self.b_save.pack(side="right", padx=6)
        self.c_part = ttk.Combobox(bar, textvariable=self.v_part, state="readonly", width=7)
        self.l_part = ttk.Label(bar, text="Копировать часть:")

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, **pad)
        self.preview = tk.Text(body, wrap="word", font=("Consolas", 10), state="disabled", undo=False)
        scroll = ttk.Scrollbar(body, command=self.preview.yview)
        self.preview.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.preview.pack(side="left", fill="both", expand=True)

        ttk.Label(self.root, textvariable=self.v_status, wraplength=740, justify="left").pack(
            fill="x", padx=8, pady=(0, 8))
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        ready = self.full is not None and self.full_fresh
        for b in (self.b_save, self.b_copy):
            b.configure(state="normal" if ready else "disabled")
        many = ready and len(self.full.parts) > 1
        if many:
            n = len(self.full.parts)
            values = [f"{i}/{n}" for i in range(1, n + 1)]
            if list(self.c_part.cget("values")) != values:
                self.c_part.configure(values=values)
                self.c_part.current(0)
            self.c_part.pack(side="right", padx=(0, 6))
            self.l_part.pack(side="right")
        else:
            self.c_part.pack_forget()
            self.l_part.pack_forget()

    def _status(self) -> None:
        self.v_status.set("\n".join(x for x in (self.preview_note, self.full_note) if x))

    def _cancel(self, name: str) -> None:
        if self.timers[name] is not None:
            self.root.after_cancel(self.timers[name])
            self.timers[name] = None

    def is_idle(self) -> bool:
        """Нет ни отложенных обновлений, ни фоновых задач (подсветка не считается). Нужно тестам."""
        return (not any(v for k, v in self.timers.items() if k != "fade")
                and not (self.loader.busy or self.previewer.busy or self.fuller.busy))

    # ------------------------------------------------------------ загрузка файла

    def browse(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Экспорт Telegram", filetypes=[("JSON", "*.json"), ("Все файлы", "*.*")])
        if chosen:
            self.load(Path(chosen))

    def load(self, path: Path) -> None:
        self.v_path.set(str(path))
        self.preview_note = "Читаю файл…"
        self._status()
        self.loader.submit(lambda: core.chats_of(core.load_export(path)), tag=path)

    def _loaded(self, chats: list[dict], path: Path) -> None:
        self.path, self.chats, self.convs, self.full = path, chats, {}, None
        labels = [f"{i}. {c.get('name') or '(без названия)'} — сообщений: {len(c.get('messages', []))}"
                  for i, c in enumerate(chats, 1)]
        self.c_chat.configure(values=labels, state="readonly" if len(chats) > 1 else "disabled")
        self.c_chat.current(0)
        self.rb_real.configure(state="normal")
        self.v_source.set("real")
        self.request("reset")

    def _load_failed(self, error: Exception) -> None:
        self.preview_note = f"Ошибка: {error}"
        self._status()
        messagebox.showerror("Не удалось открыть файл", str(error))

    def _chat_changed(self) -> None:
        self.full = None
        self.request("reset")

    # ------------------------------------------------------------ параметры

    def options(self) -> core.Options:
        def integer(var: tk.StringVar, key: str, name: str) -> int:
            try:
                value = int(var.get().strip())
            except ValueError:
                raise OptionError(f"«{name}»: нужно целое число", key) from None
            if value < 0:
                raise OptionError(f"«{name}»: число не может быть отрицательным", key)
            return value

        def day(var: tk.StringVar, key: str, name: str) -> date | None:
            text = var.get().strip()
            if not text:
                return None
            try:
                return date.fromisoformat(text)
            except ValueError:
                raise OptionError(f"«{name}»: ожидается дата в формате ГГГГ-ММ-ДД", key) from None

        opts = core.Options(
            reactions=self.v_reactions.get(),
            links=LINK_MODES[self.v_links.get()],
            keep_service=self.v_service.get(),
            media=self.v_media.get(),
            quote_min=integer(self.v_qmin, "qmin", "Цитата: от"),
            quote_max=integer(self.v_qmax, "qmax", "Цитата: до"),
            quote_adjacent=self.v_adjacent.get(),
            merge_window=integer(self.v_merge, "merge", "Склейка"),
            short_names=self.v_short.get(),
            since=day(self.v_since, "since", "С даты"),
            until=day(self.v_until, "until", "По дату"),
            max_tokens=integer(self.v_tokens, "tokens", "Токенов на часть"),
            header=self.v_header.get(),
        )
        if opts.quote_min > opts.quote_max:
            raise OptionError("Цитата: «от» не может быть больше «до»", "qmin", "qmax")
        if opts.since and opts.until and opts.since > opts.until:
            raise OptionError("«С даты» позже «По дату»", "since", "until")
        return opts

    def _mark_errors(self, error: OptionError | None) -> None:
        for key, entry in self.entries.items():
            entry.configure(style="Bad.TEntry" if error and key in error.fields else "TEntry")
        self.v_error.set(str(error) if error else "")
        if error:
            self.l_error.pack(fill="x", padx=8, before=self.bar)
        else:
            self.l_error.pack_forget()

    # ------------------------------------------------------------ обновление предпросмотра

    def request(self, cause: str) -> None:
        """cause: 'option' — правка параметра (результат подсвечивается), 'reset' — смена источника (нет)."""
        self.version += 1
        self.full_fresh = False
        if cause == "reset" or self.pending_cause is None:
            self.pending_cause = cause
        self._cancel("refresh")
        self.timers["refresh"] = self.root.after(0 if cause == "reset" else DEBOUNCE_MS, self._refresh)
        self._sync_buttons()

    def _converter_for(self, index: int) -> core.Converter:
        conv = self.convs.get(index)
        if conv is None:  # индексация всего чата — один раз, в фоновом потоке
            conv = self.convs[index] = core.Converter(self.chats[index], core.Options())
        return conv

    def _refresh(self) -> None:
        self.timers["refresh"] = None
        cause, self.pending_cause = self.pending_cause or "option", None
        try:
            opts = self.options()
        except OptionError as e:
            self._mark_errors(e)
            return
        self._mark_errors(None)

        has_chat = bool(self.chats)
        real = self.v_source.get() == "real" and has_chat
        index = self.c_chat.current() if has_chat else -1
        if real:
            total = len(self.chats[index].get("messages", []))
            note = (f"Предпросмотр: первые {PREVIEW_MESSAGES} из {total} сообщений чата."
                    if total > PREVIEW_MESSAGES else "Предпросмотр: весь чат.")
            work = lambda: self._converter_for(index).with_options(opts).convert(limit=PREVIEW_MESSAGES)  # noqa: E731
        else:
            note = "Пример: вымышленный чат. Параметры действуют так же, как на ваших данных."
            work = lambda: self.sample.with_options(opts).convert()  # noqa: E731
        self.previewer.submit(work, tag=(cause, "real" if real else "sample", note))

        self._cancel("full")  # полный результат нужен для сохранения независимо от того, что показано в предпросмотре
        if has_chat:
            self.full_note = "Считаю полный результат…"
            version = self.version
            self.timers["full"] = self.root.after(FULL_DEBOUNCE_MS, lambda: self._start_full(opts, index, version))
        else:
            self.full_note = "Выберите файл, чтобы сохранить или скопировать результат."
        self._status()

    def _start_full(self, opts: core.Options, index: int, version: int) -> None:
        self.timers["full"] = None
        self.fuller.submit(lambda: self._converter_for(index).with_options(opts).convert(), tag=version)

    def _full_ready(self, result: core.Result, version: int) -> None:
        if version != self.version:
            return  # параметры успели измениться — дождёмся свежего пересчёта
        self.full, self.full_fresh = result, True
        size = self.path.stat().st_size if self.path else 0
        parts = [f"Частей: {len(result.parts)}"] if len(result.parts) > 1 else []
        self.full_note = "\n".join(core.stats_lines(result, size) + parts)
        self._status()
        self._sync_buttons()

    def _job_failed(self, error: Exception) -> None:
        self.preview_note = f"Ошибка обработки: {error}"
        self._status()

    def _preview_ready(self, result: core.Result, tag: tuple) -> None:
        cause, source, note = tag
        self.preview_note = note
        self._status()
        text = PART_SEP.join(result.parts)[:PREVIEW_LIMIT]
        self._set_preview(text, flash=(cause == "option" and source == self.shown_source))
        self.shown_source = source

    # ------------------------------------------------------------ предпросмотр и подсветка

    def _set_preview(self, text: str, flash: bool) -> None:
        w = self.preview
        old, self.shown = self.shown, text
        top = w.index("@0,0")
        w.configure(state="normal")
        w.delete("1.0", "end")
        w.insert("1.0", text)
        w.configure(state="disabled")
        self._cancel("fade")
        w.tag_remove("flash", "1.0", "end")
        if not (flash and old):
            w.yview_moveto(0)
            return
        w.yview(top)
        lines = text.split("\n")
        ranges = diff_ranges(old, text)
        for lineno, c1, c2 in ranges:
            line = lines[lineno - 1]
            w.tag_add("flash", f"{lineno}.{tk_col(line, c1)}", f"{lineno}.{tk_col(line, c2)}")
        if not ranges:
            return
        w.update_idletasks()
        if not any(w.bbox(f"{lineno}.{tk_col(lines[lineno - 1], c1)}") for lineno, c1, _c2 in ranges):
            w.yview(f"{max(ranges[0][0] - 3, 1)}.0")  # изменения за пределами видимой области — показываем
        self._fade()

    def _fade(self) -> None:
        w = self.preview
        bg = tuple(c >> 8 for c in w.winfo_rgb(w.cget("background")))

        def step(i: int) -> None:
            if i > FLASH_STEPS:
                w.tag_remove("flash", "1.0", "end")
                self.timers["fade"] = None
                return
            t = i / FLASH_STEPS
            w.tag_configure("flash", background="#%02x%02x%02x" % tuple(round(a + (b - a) * t)
                                                                        for a, b in zip(FLASH_COLOR, bg)))
            self.timers["fade"] = self.root.after(FLASH_STEP_MS, lambda: step(i + 1))

        step(0)

    # ------------------------------------------------------------ сохранение и буфер

    def copy(self) -> None:
        if not (self.full and self.full_fresh):
            return
        index = self.c_part.current() if len(self.full.parts) > 1 else 0
        text = self.full.parts[max(index, 0)]
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()  # без этого буфер может опустеть после закрытия окна
        which = f" (часть {index + 1}/{len(self.full.parts)})" if len(self.full.parts) > 1 else ""
        self.full_note = f"Скопировано в буфер{which}: {len(text):,} симв.".replace(",", " ")
        self._status()

    def save(self) -> None:
        if not (self.full and self.full_fresh and self.path):
            return
        suggested = core.default_output(self.path, self.chats[self.c_chat.current()])
        chosen = filedialog.asksaveasfilename(
            title="Сохранить результат", initialdir=suggested.parent, initialfile=suggested.name,
            defaultextension=".txt", filetypes=[("Текст", "*.txt"), ("Все файлы", "*.*")])
        if chosen:
            self.save_to(Path(chosen))

    def save_to(self, base: Path) -> None:
        assert self.full is not None
        paths = core.part_paths(base, len(self.full.parts))
        existing = [p for p in paths[1:] if p.exists()] if len(paths) > 1 else []
        if existing and not messagebox.askyesno(
                "Перезаписать?", "Уже существуют файлы:\n" + "\n".join(p.name for p in existing[:5])):
            return
        try:
            written = core.write_parts(base, self.full.parts)
        except OSError as e:
            self.full_note = f"Ошибка записи: {e}"
            self._status()
            messagebox.showerror("Ошибка записи", str(e))
            return
        where = str(written[0]) if len(written) == 1 else f"{written[0].parent} ({len(written)} файлов)"
        self.full_note = f"Сохранено: {where}"
        self._status()


def main() -> int:
    try:  # чёткий текст на HiDPI-экранах Windows
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except (ImportError, AttributeError, OSError):
        pass
    root = tk.Tk()
    app = App(root)
    if len(sys.argv) > 1:
        root.after(100, lambda: app.load(Path(sys.argv[1])))
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
