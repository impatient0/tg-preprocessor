#!/usr/bin/env python3
"""Окно поверх tg_preprocess: выбрать result.json, подобрать параметры, посмотреть результат, сохранить или скопировать.

    python tg_preprocess_gui.py [result.json]
"""
from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import tg_preprocess as core

LINK_MODES = {"Домен": "domain", "Как есть": "keep", "Убрать": "drop"}
PREVIEW_LIMIT = 300_000  # символов; Text заметно тормозит на многомегабайтных вставках


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Telegram → LLM")
        root.minsize(760, 620)

        self.path: Path | None = None
        self.chats: list[dict] = []
        self.result: core.Result | None = None
        self.jobs: queue.Queue = queue.Queue()
        self.busy = False

        self.v_path = tk.StringVar()
        self.v_chat = tk.StringVar()
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
        self.v_status = tk.StringVar(value="Выберите файл result.json из экспорта Telegram Desktop.")

        self._build()

    # ------------------------------------------------------------ интерфейс

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Файл экспорта").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(top, textvariable=self.v_path)
        entry.grid(row=0, column=1, sticky="ew", padx=6)
        entry.bind("<Return>", lambda _e: self.load(Path(self.v_path.get().strip())))
        self.b_open = ttk.Button(top, text="Обзор…", command=self.browse)
        self.b_open.grid(row=0, column=2)
        ttk.Label(top, text="Чат").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.c_chat = ttk.Combobox(top, textvariable=self.v_chat, state="disabled")
        self.c_chat.grid(row=1, column=1, columnspan=2, sticky="ew", padx=6, pady=(6, 0))
        self.c_chat.bind("<<ComboboxSelected>>", lambda _e: self.convert())

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
            ("Ссылки", None),
            ("С даты (ГГГГ-ММ-ДД)", self.v_since),
            ("По дату (включительно)", self.v_until),
            ("Склейка сообщений, мин", self.v_merge),
            ("Цитата ответа: от, симв.", self.v_qmin),
            ("Цитата ответа: до, симв.", self.v_qmax),
            ("Токенов на часть (0 — без разбивки)", self.v_tokens),
        )
        for i, (label, var) in enumerate(rows):
            ttk.Label(fields_, text=label).grid(row=i, column=0, sticky="w", pady=2)
            if var is None:
                w = ttk.Combobox(fields_, textvariable=self.v_links, values=list(LINK_MODES), state="readonly", width=12)
            else:
                w = ttk.Entry(fields_, textvariable=var, width=12)
            w.grid(row=i, column=1, sticky="w", padx=(10, 0), pady=2)

        actions = ttk.Frame(self.root)
        actions.pack(fill="x", **pad)
        self.b_convert = ttk.Button(actions, text="Преобразовать", command=self.convert)
        self.b_convert.pack(side="left")
        self.l_part = ttk.Label(actions, text="Часть:")
        self.c_part = ttk.Combobox(actions, textvariable=self.v_part, state="readonly", width=8)
        self.c_part.bind("<<ComboboxSelected>>", lambda _e: self.show_part())
        self.b_copy = ttk.Button(actions, text="Копировать в буфер", command=self.copy)
        self.b_copy.pack(side="right")
        self.b_save = ttk.Button(actions, text="Сохранить…", command=self.save)
        self.b_save.pack(side="right", padx=6)

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, **pad)
        self.preview = tk.Text(body, wrap="word", font=("Consolas", 10), state="disabled", undo=False)
        scroll = ttk.Scrollbar(body, command=self.preview.yview)
        self.preview.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.preview.pack(side="left", fill="both", expand=True)

        ttk.Label(self.root, textvariable=self.v_status, wraplength=740, justify="left").pack(fill="x", padx=8, pady=(0, 8))
        self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.root.configure(cursor="watch" if busy else "")
        state = "disabled" if busy else "normal"
        for b in (self.b_open, self.b_convert):
            b.configure(state=state)
        has_result = self.result is not None and not busy
        for b in (self.b_save, self.b_copy):
            b.configure(state="normal" if has_result else "disabled")

    def _status(self, text: str) -> None:
        self.v_status.set(text)

    # ------------------------------------------------------------ фоновые задачи

    def _run(self, work, done) -> None:
        """Выполняет work() в потоке; done(результат) вызывается уже в потоке интерфейса."""
        if self.busy:
            return
        self._set_busy(True)

        def target():
            try:
                self.jobs.put((done, work(), None))
            except Exception as e:  # noqa: BLE001 — любую ошибку показываем пользователю
                self.jobs.put((done, None, e))

        threading.Thread(target=target, daemon=True).start()
        self.root.after(50, self._poll)

    def _poll(self) -> None:
        try:
            done, value, error = self.jobs.get_nowait()
        except queue.Empty:
            self.root.after(50, self._poll)
            return
        self._set_busy(False)
        if error is not None:
            self._status(f"Ошибка: {error}")
            messagebox.showerror("Ошибка", str(error))
        else:
            done(value)

    # ------------------------------------------------------------ загрузка и преобразование

    def browse(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Экспорт Telegram", filetypes=[("JSON", "*.json"), ("Все файлы", "*.*")])
        if chosen:
            self.load(Path(chosen))

    def load(self, path: Path) -> None:
        self.v_path.set(str(path))
        self._status("Читаю файл…")
        self._run(lambda: core.chats_of(core.load_export(path)), lambda chats: self._loaded(path, chats))

    def _loaded(self, path: Path, chats: list[dict]) -> None:
        self.path, self.chats, self.result = path, chats, None
        labels = [f"{i}. {c.get('name') or '(без названия)'} — сообщений: {len(c.get('messages', []))}"
                  for i, c in enumerate(chats, 1)]
        self.c_chat.configure(values=labels, state="readonly" if len(chats) > 1 else "disabled")
        self.c_chat.current(0)
        self.convert()

    def options(self) -> core.Options:
        def integer(var: tk.StringVar, name: str) -> int:
            try:
                value = int(var.get().strip())
            except ValueError:
                raise ValueError(f"«{name}»: нужно целое число") from None
            if value < 0:
                raise ValueError(f"«{name}»: число не может быть отрицательным")
            return value

        def day(var: tk.StringVar, name: str) -> date | None:
            text = var.get().strip()
            if not text:
                return None
            try:
                return date.fromisoformat(text)
            except ValueError:
                raise ValueError(f"«{name}»: ожидается дата в формате ГГГГ-ММ-ДД") from None

        opts = core.Options(
            reactions=self.v_reactions.get(),
            links=LINK_MODES[self.v_links.get()],
            keep_service=self.v_service.get(),
            media=self.v_media.get(),
            quote_min=integer(self.v_qmin, "Цитата: от"),
            quote_max=integer(self.v_qmax, "Цитата: до"),
            quote_adjacent=self.v_adjacent.get(),
            merge_window=integer(self.v_merge, "Склейка"),
            short_names=self.v_short.get(),
            since=day(self.v_since, "С даты"),
            until=day(self.v_until, "По дату"),
            max_tokens=integer(self.v_tokens, "Токенов на часть"),
            header=self.v_header.get(),
        )
        if opts.quote_min > opts.quote_max:
            raise ValueError("Цитата: «от» не может быть больше «до»")
        if opts.since and opts.until and opts.since > opts.until:
            raise ValueError("«С даты» позже «По дату»")
        return opts

    def convert(self) -> None:
        if not self.chats:
            self._status("Сначала выберите файл.")
            return
        try:
            opts = self.options()
        except ValueError as e:
            self._status(f"Ошибка: {e}")
            messagebox.showerror("Неверный параметр", str(e))
            return
        chat = self.chats[self.c_chat.current()]
        self._status("Преобразую…")
        self._run(lambda: core.Converter(chat, opts).convert(), self._converted)

    def _converted(self, result: core.Result) -> None:
        self.result = result
        n = len(result.parts)
        if n > 1:
            self.c_part.configure(values=[f"{i}/{n}" for i in range(1, n + 1)])
            self.c_part.current(0)
            self.l_part.pack(side="left", padx=(16, 4))
            self.c_part.pack(side="left")
        else:
            self.l_part.pack_forget()
            self.c_part.pack_forget()
        self._set_busy(False)
        self.show_part()
        stats = core.stats_lines(result, self.path.stat().st_size if self.path else 0)
        self._status("\n".join(stats + ([f"Частей: {n}"] if n > 1 else [])))

    # ------------------------------------------------------------ результат

    def current_text(self) -> str:
        if self.result is None:
            return ""
        index = self.c_part.current() if len(self.result.parts) > 1 else 0
        return self.result.parts[max(index, 0)]

    def show_part(self) -> None:
        text = self.current_text()
        if len(text) > PREVIEW_LIMIT:
            text = text[:PREVIEW_LIMIT] + "\n… (предпросмотр обрезан; полный текст — в файле и в буфере)"
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        self.preview.configure(state="disabled")

    def copy(self) -> None:
        text = self.current_text()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()  # без этого буфер может опустеть после закрытия окна
        which = f" (часть {self.v_part.get()})" if self.result and len(self.result.parts) > 1 else ""
        self._status(f"Скопировано в буфер{which}: {len(text):,} симв.".replace(",", " "))

    def save(self) -> None:
        if self.result is None or self.path is None:
            return
        suggested = core.default_output(self.path, self.chats[self.c_chat.current()])
        chosen = filedialog.asksaveasfilename(
            title="Сохранить результат", initialdir=suggested.parent, initialfile=suggested.name,
            defaultextension=".txt", filetypes=[("Текст", "*.txt"), ("Все файлы", "*.*")])
        if chosen:
            self.save_to(Path(chosen))

    def save_to(self, base: Path) -> None:
        assert self.result is not None
        paths = core.part_paths(base, len(self.result.parts))
        existing = [p for p in paths[1:] if p.exists()] if len(paths) > 1 else []
        if existing and not messagebox.askyesno(
                "Перезаписать?", "Уже существуют файлы:\n" + "\n".join(p.name for p in existing[:5])):
            return
        try:
            written = core.write_parts(base, self.result.parts)
        except OSError as e:
            self._status(f"Ошибка записи: {e}")
            messagebox.showerror("Ошибка записи", str(e))
            return
        self._status("Сохранено: " + (str(written[0]) if len(written) == 1 else f"{written[0].parent} ({len(written)} файлов)"))


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
